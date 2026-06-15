"""One bidirectional Gemini Live session bridging a speaker to a target language.

We talk to Gemini Live via a raw WebSocket against the v1beta BidiGenerateContent
endpoint rather than via google-genai's `client.aio.live.connect()`. The v1beta
API expects `translationConfig` nested under `generationConfig` (renamed from the
EAP-era `streamingTranslationConfig` at the public launch). Bypassing the SDK lets
us control the exact JSON shape; python-genai >= 2.8.0 now exposes a matching
`TranslationConfig` if we later choose to adopt the SDK.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import re

import websockets
from livekit import rtc

from audio import iter_pcm_for_gemini, make_audio_source, push_pcm_to_source
from config import (
    AVAILABLE_VOICES,
    CONTENT_TYPE_CINEMATIC_FAITHFUL,
    CONTENT_TYPE_MOVIE,
    GEMINI_INPUT_SAMPLE_RATE,
    GEMINI_MAX_FAILURES_BEFORE_LONG_BACKOFF,
    GEMINI_MODEL,
    GEMINI_RECONNECT_BACKOFF_SEC,
    MAX_HISTORY_SEGMENTS,
    MAX_HISTORY_WORDS,
    NATIVE_LANG,
    PARTICIPANT_LANG_ATTR,
)
from lexicon import get_lexicon_instructions

logger = logging.getLogger("translator.session")


GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

# --- Structured transcription markers ---
# Gemini wraps each transcription segment in these markers carrying JSON metadata.
SEGMENT_RE = re.compile(r"\[SEGMENT\](.*?)\[/SEGMENT\]", re.DOTALL)
OUTPUT_RE = re.compile(r"\[OUTPUT\](.*?)\[/OUTPUT\]", re.DOTALL)


def _new_conversation_id() -> str:
    """Generate a unique conversation ID for this session."""
    import uuid

    return uuid.uuid4().hex[:12]


class GeminiSession:
    """Bridges a single speaker's mic into a single target-language translation track.

    Lifecycle:
      - `start()` publishes the translator track and starts the WS-pump loop.
      - `aclose()` tears everything down. Idempotent.
      - On WebSocket errors, reconnects with exponential backoff. After
        `GEMINI_MAX_FAILURES_BEFORE_LONG_BACKOFF` consecutive failures it logs
        at ERROR level and keeps retrying with the longest backoff.
    """

    @staticmethod
    def _base_lang_code(code: str) -> str:
        """Strip the region suffix from a locale code for Gemini's API.

        Gemini's ``translationConfig.targetLanguageCode`` expects ISO 639-1 base
        codes (e.g. ``nl``, ``fr``, ``pt``).  Regional variants like ``nl-BE`` or
        ``pt-BR`` are sent to the LLM via a dialect instruction in the system
        prompt instead.
        """
        return code.split("-")[0]

    def _voice_pool_text(self) -> str:
        """Format the available voice pool for injection into the system instruction."""
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._available_voices):
            label = f"Voice {i + 1} (\u201c{name}\u201d)"
            lines.append(f"  {label}: {desc}")
        lines.append(
            "  Assign these in order of speaker appearance (Voice 1 to "
            "first detected speaker, Voice 2 to second, etc.). If there "
            "are more speakers than voices, wrap around from the start."
        )
        return "\n".join(lines)

    def __init__(
        self,
        *,
        room: rtc.Room,
        speaker_identity: str,
        speaker_track: rtc.RemoteAudioTrack,
        track_source: str,
        target_lang: str,
        gemini_api_key: str,
        glossary: list[dict[str, str]] | None = None,
        content_type: str = "normal",
        available_voices: list[tuple[str, str]] | None = None,
    ) -> None:
        self._room = room
        self._speaker_identity = speaker_identity
        self._speaker_track = speaker_track
        self._track_source = track_source
        self._target_lang = target_lang
        self._gemini_api_key = gemini_api_key
        self._glossary = glossary or []
        self._content_type = content_type
        self._available_voices = available_voices or AVAILABLE_VOICES[:4]
        # Accumulated segments for cinematic_faithful structured JSON output.
        self._cinematic_segments: list[dict] = []
        self._voice_casting_map: dict[str, dict] = {}

        participant = self._room.remote_participants.get(self._speaker_identity)
        source_lang = (
            (participant.attributes or {}).get(PARTICIPANT_LANG_ATTR)
            if participant
            else None
        )
        self._source_lang = None if source_lang == NATIVE_LANG else source_lang

        self._audio_source = make_audio_source()
        self._local_track: rtc.LocalAudioTrack | None = None
        self._track_sid: str | None = None
        self._consecutive_failures = 0
        self._tasks: list[asyncio.Task] = []
        self._closed = asyncio.Event()
        # Conversation ID for correlating transcription/translation segments.
        self._conversation_id: str = _new_conversation_id()
        # Rolling structured segment memory: list of segment dicts.
        # Each segment carries: speaker_id, speaker_label, text, language,
        # confidence, tone, emotion, speech_style, overlap_status, etc.
        self._segment_history: list[dict] = []
        # Track active speakers for continuity.
        self._active_speakers: dict[str, str] = {}  # speaker_label -> speaker_id
        self._speaker_counter: int = 0
        # Accumulated segments for structured JSON publishing at turn end.
        self._pending_segments: list[dict] = []

    # --- Public API ---------------------------------------------------------

    async def start(self) -> None:
        """Publish the translator track and start the connect-and-pump loop."""
        track_name = (
            f"tx:{self._speaker_identity}:{self._track_source}:{self._target_lang}"
        )
        self._local_track = rtc.LocalAudioTrack.create_audio_track(
            track_name, self._audio_source
        )
        publish_opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)

        pub = await self._room.local_participant.publish_track(
            self._local_track, publish_opts
        )
        self._track_sid = pub.sid

        # Track-level attributes aren't yet exposed in this version of the
        # livekit Python/JS SDKs, so routing is keyed off the track NAME
        # ("tx:<speaker>:<lang>") which the frontend parses. See
        # src/app/session/[id]/room/useTranslationRouting.ts.

        logger.info(
            "started translator track sid=%s name=%s for %s -> %s",
            self._track_sid,
            track_name,
            self._speaker_identity,
            self._target_lang,
        )

        self._tasks.append(
            asyncio.create_task(self._run(), name=f"session/{track_name}")
        )

    async def aclose(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:  # noqa: SIM105
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        # Unpublish and free the audio source.
        if self._track_sid:
            try:
                await self._room.local_participant.unpublish_track(self._track_sid)
            except Exception as exc:
                logger.debug("unpublish failed for %s: %s", self._track_sid, exc)

        with contextlib.suppress(Exception):
            await self._audio_source.aclose()

        logger.info(
            "closed translator session for %s -> %s",
            self._speaker_identity,
            self._target_lang,
        )

    # --- Internal pumps -----------------------------------------------------

    async def _run(self) -> None:
        """Outer loop: connect, pump, reconnect on failure."""
        while not self._closed.is_set():
            try:
                await self._connect_and_pump()
                # If _connect_and_pump returns cleanly, the speaker track ended.
                # Don't reconnect; rely on the router to clean us up.
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_failures += 1
                idx = min(
                    self._consecutive_failures - 1,
                    len(GEMINI_RECONNECT_BACKOFF_SEC) - 1,
                )
                delay = GEMINI_RECONNECT_BACKOFF_SEC[idx]
                delay += random.uniform(0, delay * 0.2)  # jitter
                if (
                    self._consecutive_failures
                    >= GEMINI_MAX_FAILURES_BEFORE_LONG_BACKOFF
                ):
                    logger.error(
                        "Eburon session %s -> %s failed %d times; will keep retrying with long backoff",
                        self._speaker_identity,
                        self._target_lang,
                        self._consecutive_failures,
                    )
                logger.warning(
                    "Eburon session error (%s -> %s) attempt #%d: %s; backing off %.2fs",
                    self._speaker_identity,
                    self._target_lang,
                    self._consecutive_failures,
                    exc,
                    delay,
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=delay)
                    return  # closed during backoff
                except asyncio.TimeoutError:
                    pass

    async def _connect_and_pump(self) -> None:
        """One Gemini WebSocket connect + bidirectional pump."""
        url = f"{GEMINI_WS_URL}?key={self._gemini_api_key}"
        # Max payload size: enough to cover ~1s of 48 kHz 16-bit PCM in base64.
        async with websockets.connect(
            url, max_size=2**22, ping_interval=20, ping_timeout=20
        ) as ws:
            payload = self._build_setup_payload()
            logger.info(
                "Eburon WS connecting: %s -> %s, model=%s",
                self._speaker_identity,
                self._target_lang,
                payload["setup"]["model"],
            )
            await ws.send(json.dumps(payload))
            logger.info(
                "Eburon WS setup sent: %s -> %s, awaiting setupComplete",
                self._speaker_identity,
                self._target_lang,
            )

            setup_complete = asyncio.Event()
            send_task = asyncio.create_task(
                self._pump_input(ws, setup_complete), name="eburon-input"
            )
            recv_task = asyncio.create_task(
                self._pump_output(ws, setup_complete), name="eburon-output"
            )

            done, pending = await asyncio.wait(
                {send_task, recv_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc

    def _build_setup_payload(self) -> dict:
        """The first WS message — must match the v1beta BidiGenerateContent setup
        schema. Field names use the exact camelCase the API expects (verified
        against the previous Node implementation that worked in production)."""
        base_instruction = (
            "You are a real-time voice translator. Your job is two-fold: "
            "produce flawless, natural-sounding target-language speech AND "
            "deliver it with the identical vocal character of the source speaker."
            "\n\n"
            "OUTPUT QUALITY \u2014 NON-NEGOTIABLE:\n"
            "1. Your translation MUST be grammatically perfect in the target "
            "language \u2014 correct verb conjugations, noun-adjective agreement, "
            "word order, prepositions, pronouns, and natural sentence flow. "
            "There is zero tolerance for translationese, stilted phrasing, "
            "or unnatural constructions.\n"
            "2. SOUND 100% HUMAN \u2014 You MUST sound like a real, fluent "
            "native speaker having a spontaneous conversation. Use natural "
            "contractions, common idioms, and colloquial flow appropriate to "
            "the target language and the speaker\u2019s register (formal, casual, "
            "professional, etc.). Your output "
            "must be indistinguishable from a human voice actor performing "
            "the line in the target language for the first time. "
            "Never sound robotic, flat, or like a text-to-speech engine.\n"
            "3. TRANSLATE THE FULL MEANING \u2014 NEVER WORD-FOR-WORD: "
            "You MUST translate the ENTIRE meaning of everything the speaker "
            "says \u2014 every thought, point, emotion, and detail \u2014 but "
            "using natural, idiomatic expressions in the target language, "
            "NOT literal word-for-word rendering. Do NOT omit or skip any "
            "part of the message. If the speaker says ten sentences, you "
            "convey all ten in natural target-language phrasing. If they "
            "list five items, you convey all five using natural expressions. "
            "But you MUST NOT do literal word substitution \u2014 instead, "
            "understand what the speaker MEANS and say it the way a native "
            "speaker naturally would. Prioritize the meaning and intent over "
            "the exact words. There is NO exception \u2014 not for length, "
            "not for complexity, not for speed.\n"
            "4. Use the recent conversation context below (under IMPORTANT "
            "CONTEXT) to disambiguate ambiguous terms, maintain topic "
            "consistency, choose the correct formality level, and produce "
            "domain-appropriate vocabulary. If a speaker has glossary terms "
            "defined, you MUST honour those exact translations.\n"
            "5. Never summarize, truncate, sanitize, or soften the original "
            "message. Preserve every point, every emotion, every nuance."
            "\n\n"
            "ECHO THE SPEAKER\u2019S VOICE \u2014 IDENTICAL VOCAL REPRODUCTION:\n"
            "Your output audio MUST be an exact vocal echo of the source speaker. "
            "The listener should feel like the original speaker has magically "
            "learned to speak the target language fluently while keeping their "
            "own voice completely intact.\n"
            "  \u2022 SPEED MUST MATCH THE SOURCE EXACTLY: The single most "
            "important thing is matching the source audio speed. Listen to the "
            "source speaker\u2019s pace and duplicate it precisely \u2014 words "
            "per minute, rhythm, cadence. If they are speaking fast, you MUST "
            "speak fast at the identical rate. If they are speaking slowly and "
            "deliberately, you MUST speak at that same slow pace. If their speed "
            "changes mid-sentence (e.g. starting slow then rushing), your speed "
            "must change identically at the same moment. Speed mismatch is the "
            "most noticeable and jarring error. Get the speed right above "
            "everything else.\n"
            "- Copy the source speaker\u2019s rhythm, pauses, pitch contour, "
            "volume, and intonation exactly. Every micro-timing, every breath, "
            "every hesitation must be replicated identically.\n"
            "- If they laugh, giggle, or chuckle \u2014 you produce the same "
            "laugh, giggle, or chuckle at the same duration and intensity.\n"
            "- If they sigh \u2014 you sigh the same way.\n"
            "- If they whisper \u2014 you whisper at the same volume.\n"
            "- If they pause for effect \u2014 you pause for the same duration.\n"
            "- If they raise their pitch in excitement \u2014 you match that "
            "pitch.\n"
            "- If they trail off, stammer, or hesitate \u2014 you trail off, "
            "stammer, or hesitate identically.\n"
            "- If they sound sarcastic, amused, angry, frustrated, bored, "
            "excited, tender, or hesitant \u2014 you carry that exact emotional "
            "tone into the translation without flattening or sanitizing it."
            "\n\n"
            "The result must be indistinguishable from the source speaker "
            "delivering the same message fluently and naturally in the target "
            "language. This means BOTH grammatically impeccable language AND "
            "identical vocal delivery. In short: echo the voice \u2014 "
            "make the speaker sound like themselves in another language."
            "\n\n"
            "MULTI-SPEAKER DIARIZATION \u2014 NON-NEGOTIABLE:\n"
            "1. The audio you receive may contain MULTIPLE distinct speakers. "
            "You MUST identify and distinguish each speaker by their unique "
            "voice characteristics \u2014 pitch, tone, cadence, accent, "
            "speech patterns, and any other vocal traits.\n"
            "2. Track speaker changes with precision. When one speaker stops "
            "and another begins, you MUST shift your vocal delivery to match "
            "the new speaker\u2019s voice \u2014 NOT continue in the previous "
            "speaker\u2019s voice.\n"
            "3. Never merge lines from different speakers into a single "
            "utterance block. Each speaker\u2019s contribution is a separate "
            "segment with its own vocal identity.\n"
            "4. Even in a conversation or meeting, treat each participant "
            "as a distinct \u201ccharacter\u201d with their own consistent "
            "vocal signature \u2014 the listener must be able to tell who "
            "is \u201cspeaking\u201d from your audio delivery alone.\n"
            "5. SPEAKER TAGS IN SOURCE TRANSCRIPTION \u2014 CRITICAL: "
            "For EVERY source transcription chunk you output, you MUST "
            "prepend a speaker tag so the frontend knows which speaker "
            "said what. Use short, consistent labels:\n"
            "   \u2022 FIRST detected speaker: no tag needed (single-speaker "
            "transcription as-is)\n"
            "   \u2022 SECOND+ speakers: prepend \u201c[A] \u201d, "
            "\u201c[B] \u201d, \u201c[C] \u201d, etc. in order of first "
            "appearance\n"
            "   \u2022 If a speaker is identifiable by name from context "
            "(e.g. \u201cJohn said\u2026\u201d), use their name tag: "
            "\u201c[John] \u201d\n"
            "   \u2022 When the same speaker resumes after another has spoken, "
            "re-use their existing letter tag \u2014 NEVER assign a new letter\n"
            "   \u2022 When speaker switches mid-chunk, split and tag: "
            "\u201c[A] First line [B] Second line\u201d\n"
            "6. NEVER merge two speakers\u2019 transcribed words into a single "
            "unbroken transcription line. If two people speak at once, still "
            "tag the dominant speaker\u2019s line and note [crosstalk] if "
            "detectable."
            "\n\n"
            "CHARACTER ROLE MIMICRY \u2014 DISTINCT VOCAL STYLES:\n"
            "1. For EACH speaker, adopt a distinct vocal delivery that matches "
            "their natural persona and role in the conversation. A confident "
            "speaker should sound confident, a nervous speaker hesitant, a "
            "formal speaker measured and precise, an excited speaker buoyant.\n"
            "2. Match the emotional tone per speaker per moment \u2014 "
            "excitement, sadness, anger, suspense, warmth, sarcasm \u2014 "
            "each speaker\u2019s delivery must reflect their individual "
            "emotional state, not a flattened average.\n"
            "3. If a speaker raises their voice in anger, your delivery for "
            "that speaker must carry that same edge. If they soften to a "
            "whisper, you soften with them.\n"
            "4. Maintain CONSISTENCY: once you establish a vocal style for a "
            "speaker early in the conversation, carry that same style through "
            "the entire session. The listener should immediately recognise "
            "who is speaking from the vocal character alone."
            "\n\n"
            "CINEMATIC TRANSLATION QUALITY \u2014 IDIOMATIC, PACED, CULTURALLY "
            "ADAPTED:\n"
            "1. Your translation must sound like a native speaker delivering "
            "the message with natural, idiomatic flow \u2014 as if the original "
            "had been written in the target language from the start. Always "
            "translate by EXACT MEANING, not word-for-word. Convey the INTENT "
            "and EMOTIONAL WEIGHT through the most natural expression a native "
            "speaker would use.\n"
            "2. Dramatic pacing matters. Anticipate pauses, emphasis, and "
            "rhythm to align with the scene\u2019s or conversation\u2019s "
            "emotional arc. Speed up during excitement, slow down for gravity, "
            "pause for effect when the speaker does.\n"
            "3. Culturally adapt idioms, jokes, metaphors, and references "
            "into equivalent expressions that feel native in the target "
            "language. A pun that only works in the source language must be "
            "replaced with a conceptually equivalent play on words or omitted "
            "gracefully \u2014 never explained or footnoted.\n"
            "4. Preserve the dramatic tension, narrative flow, and emotional "
            "trajectory of the original. Your output should feel like "
            "watching/listening to the content natively in the target language "
            "\u2014 NOT like listening to a flat interpreter reading a script."
            "\n\n"
            "SPEAKER-VOICE MAPPING \u2014 PER-SPEAKER VOICE ASSIGNMENT:\n"
            "1. When your output contains multiple speakers (detected via "
            "diarization above), you MUST assign each speaker a distinct "
            "voice from the AVAILABLE VOICE POOL below.\n"
            "2. Assign voices in order of speaker appearance: first detected "
            "speaker gets the first voice in the pool, second gets the second, "
            "third gets the third, and so on. The pool wraps around if there "
            "are more speakers than voices.\n"
            "3. Once a speaker has been assigned a voice, that assignment is "
            "PERMANENT for the entire session. Speaker A ALWAYS uses the "
            "first voice, Speaker B ALWAYS uses the second voice, etc. "
            "Never swap voices mid-session.\n"
            "4. The voice assignment governs BOTH the vocal delivery in the "
            "translated audio AND the speaker tag in the source transcription. "
            "Speaker A (tag \u201c[A]\u201d) must sound like the first voice "
            "in the pool; Speaker B must sound like the second; and so on.\n"
            "5. The voice characteristics must be clearly distinguishable "
            "from one another. The listener must be able to identify which "
            "speaker is talking from the audio alone, even without speaker "
            "tags.\n"
            "6. Within a single speaker\u2019s assigned voice, you may still "
            "modulate pitch, speed, and emotion for dramatic effect \u2014 "
            "but the core vocal signature (tone colour, resonance, baseline "
            "pitch range) must remain consistent for that speaker.\n"
            "7. OUTPUT TRANSCRIPTION SPEAKER TAGS: In the output transcription "
            "(the translated text stream), you MUST also include speaker tags "
            "matching the same assignment. The translated text captions must "
            "show \u201c[A] translated text\u201d, \u201c[B] translated text\u201d "
            "so the listener sees who is speaking in their own language.\n\n"
            "STRUCTURED TRANSLATION OUTPUT \u2014 ATTACH METADATA:\n"
            "For EVERY output transcription segment (the translated text), "
            "you MUST wrap it in a structured JSON marker like this:\n"
            '[OUTPUT]{"speaker_id":"speaker_1","speaker_label":"A",'
            '"translated_text":"the fluent natural translation",'
            '"confidence":0.92,'
            '"nuance_notes":"brief explanation of tone or context choices made"}'
            "[/OUTPUT]\n"
            "Rules:\n"
            "  \u2022 ALWAYS wrap the translated text in [OUTPUT]...[/OUTPUT] "
            "markers with the JSON metadata.\n"
            "  \u2022 speaker_id and speaker_label must match the source "
            "segment\u2019s assignment for the same speaker.\n"
            "  \u2022 translated_text: the fluent, natural, meaning-based "
            "translation in the target language.\n"
            "  \u2022 confidence: your confidence in the translation accuracy "
            "(0.0 to 1.0).\n"
            "  \u2022 nuance_notes: a SHORT phrase explaining the key nuance "
            "choice, e.g. \u201cpreserved sarcastic tone\u201d, "
            "\u201cadapted idiom to local equivalent\u201d, "
            "\u201cmaintained formal register\u201d, "
            "\u201cpreserved emotional weight\u201d, "
            "\u201ckept hesitant delivery\u201d. Omit if straightforward.\n"
            "  \u2022 The plain text portion (outside markers) should contain "
            "just the translated text with speaker tag for backward "
            "compatibility, e.g.: \u201c[A] Translated text here\u201d\n\n"
            "AVAILABLE VOICE POOL:\n" + self._voice_pool_text() + "\n"
            f"You MUST translate the input into the language with code "
            f"'{self._target_lang}'. All output audio and text must be in "
            f"that language."
        )

        stt_instruction = (
            "\n\nSTT ACCURACY \u2014 VERBATIM SOURCE TRANSCRIPTION (CRITICAL):\n"
            "You MUST transcribe the source audio EXACTLY as it was spoken. "
            "This means:\n"
            "1. Capture EVERY word the speaker says, including filler words, "
            "hesitations, and discourse markers: \u201cum\u201d, \u201cuh\u201d, "
            "\u201clike\u201d, \u201cyou know\u201d, \u201cI mean\u201d, "
            "\u201cwell\u201d, \u201cactually\u201d, \u201cbasically\u201d, "
            "\u201cso\u201d, \u201cright\u201d, \u201cokay\u201d, etc. These "
            "carry meaning about the speaker\u2019s confidence, hesitation, "
            "and conversational flow.\n"
            "2. Preserve FALSE STARTS and self-corrections exactly as uttered: "
            "\u201cI went to the\u2026 I mean, I was heading to the store\u201d "
            "\u2014 do NOT clean it up to \u201cI was heading to the store.\u201d\n"
            "3. Preserve REPETITIONS: \u201cI\u2026 I think so\u201d, \u201cIt "
            "was really really good\u201d \u2014 do NOT collapse them.\n"
            "4. Preserve STUTTERS and partial words when clearly audible: "
            "\u201cI w-w-went there\u201d, \u201cHe said he\u2026 uh\u2026 "
            "he wasn\u2019t sure\u201d.\n"
            "5. Preserve INTERJECTIONS and backchannels: \u201coh\u201d, "
            "\u201cah\u201d, \u201cwow\u201d, \u201chmm\u201d, \u201cuh-huh\u201d, "
            "\u201cnah\u201d, \u201cyeah\u201d, \u201cnope\u201d.\n"
            "6. Preserve the speaker\u2019s EXACT WORD CHOICE even if it is "
            "grammatically incorrect, slang, non-standard, or fragmented. "
            "If they say \u201cain\u2019t\u201d, write \u201cain\u2019t\u201d. "
            "If they say \u201cgonna\u201d, write \u201cgonna\u201d.\n"
            "7. Do NOT add punctuation that imposes a grammar the speaker "
            "didn\u2019t use. Do NOT rephrase, smooth, or \u201cclean up\u201d "
            "the transcription in any way. The transcription must be a "
            "forensic-level faithful record of what was actually said and "
            "how it was said.\n"
            "8. Accurate language detection is essential. The source speaker\u2019s "
            "language may change at any time."
            "\n\n"
            "STRUCTURED SEGMENT OUTPUT \u2014 CRITICAL FORMAT:\n"
            "For EVERY transcription segment you output, you MUST wrap it in "
            "a structured JSON marker like this:\n"
            '[SEGMENT]{"speaker_id":"speaker_1","speaker_label":"A",'
            '"text":"the exact words spoken",'
            '"language":"en","confidence":0.95,'
            '"tone":"calm","emotion":"neutral",'
            '"speech_style":"casual","overlap_status":"none",'
            '"active_speaker":true}[/SEGMENT]\n'
            "Rules:\n"
            "  \u2022 ALWAYS wrap every source transcription segment in "
            "[SEGMENT]...[/SEGMENT] markers with the JSON metadata.\n"
            "  \u2022 speaker_id: use \u201cspeaker_1\u201d, \u201cspeaker_2\u201d, "
            "etc. in order of first appearance. Keep the same ID for the same "
            "speaker throughout the session.\n"
            "  \u2022 speaker_label: use the single-letter tag like "
            "\u201cA\u201d, \u201cB\u201d matching the [A], [B] tag system.\n"
            "  \u2022 text: the EXACT verbatim transcription of what was said "
            "(filler words, false starts, stutters, all of it).\n"
            "  \u2022 language: detect the language of this segment "
            "(ISO 639-1 code like \u201cen\u201d, \u201cfr\u201d, \u201ces\u201d).\n"
            "  \u2022 confidence: your confidence in the transcription accuracy "
            "(0.0 to 1.0).\n"
            "  \u2022 tone: classify the speaker\u2019s tone as one of: "
            "calm, angry, confused, polite, urgent, emotional, sarcastic, "
            "humorous, formal, excited, nervous, bored, fearful, thoughtful, "
            "neutral.\n"
            "  \u2022 emotion: if distinct from tone, specify the emotion "
            "(happy, sad, frustrated, anxious, confident, surprised, "
            "disappointed, hopeful, indifferent, amused).\n"
            "  \u2022 speech_style: classify as: formal, casual, hesitant, "
            "fast, slow, deliberate, interrupted, whispered, shouted, "
            "sing-song, monotone, fragmented.\n"
            "  \u2022 overlap_status: \u201cnone\u201d if this speaker is the "
            "only one talking, \u201cpartial\u201d if brief crosstalk, "
            "\u201coverlapping\u201d if sustained simultaneous speech.\n"
            "  \u2022 active_speaker: true for this segment\u2019s primary "
            "speaker, false if this segment captures a secondary/background "
            "speaker.\n"
            "  \u2022 Only ONE speaker should be marked as active_speaker=true "
            "per segment unless there is verified overlapping speech.\n"
            "  \u2022 The [SEGMENT]...[/SEGMENT] block is in addition to the "
            "speaker tags in the text field itself. Both must be present.\n"
            "  \u2022 The plain text portion (outside the markers) should "
            "contain just the transcription with speaker tag for backward "
            "compatibility, e.g.: \u201c[A] What the person said\u201d"
        )
        if self._source_lang:
            stt_instruction += (
                f" Their primary profile language is "
                f"'{self._source_lang}', but detect dynamically."
            )
        stt_instruction += (
            "\n\nThis verbatim transcription is the FOUNDATION for the "
            "translation that follows. Errors in transcription cause a domino "
            "effect on translation quality. Pay close attention to phonetics, "
            "context, and domain terminology."
        )
        base_instruction += stt_instruction

        # Inject rolling structured segment context.
        if self._segment_history:
            total_words = 0
            context_parts: list[str] = []
            # Walk in reverse (newest first) until we hit the word cap.
            for seg in reversed(self._segment_history):
                seg_text = seg.get("text", "")
                words = len(seg_text.split())
                if total_words + words > MAX_HISTORY_WORDS:
                    break
                total_words += words
                kind = seg.get("kind", "source")
                speaker = seg.get("speaker_label", "?")
                tone = seg.get("tone", "")
                emotion = seg.get("emotion", "")
                style = seg.get("speech_style", "")
                meta = (
                    f" (tone={tone}, emotion={emotion}, style={style})"
                    if tone or emotion or style
                    else ""
                )
                prefix = "Speaker said" if kind == "source" else "Translation"
                context_parts.insert(
                    0,
                    f"  [{speaker}] {prefix}{meta}: {seg_text}",
                )
            if context_parts:
                base_instruction += (
                    "\n\nIMPORTANT CONTEXT from the conversation so far "
                    "(with speaker, tone, emotion, and style metadata):\n"
                    f"{chr(10).join(context_parts)}"
                )

        # Append glossary terms if defined
        if self._glossary:
            glossary_lines = "\n".join(
                f'  - "{entry["source"]}" → "{entry["translation"]}"'
                for entry in self._glossary
                if entry.get("source") and entry.get("translation")
            )
            if glossary_lines:
                base_instruction += (
                    "\n\nCRITICAL: The speaker has defined the following custom "
                    "translation glossary. You MUST use these specific translations "
                    "whenever the original term appears, regardless of context:\n"
                    f"{glossary_lines}"
                )

        # Language-specific dialect instructions
        dialect_map = {
            "nl-BE": (
                " The target language is Flemish (Belgian Dutch). Use Flemish "
                "pronunciation, intonation, and vocabulary \u2014 NOT standard "
                "Netherlands Dutch. Use 't is', 'gij/ge' instead of 'het is', 'jij/je', "
                "and other typical Flemish expressions. Sound like you are from "
                "Antwerp, Ghent, or Brussels, not from Amsterdam."
            ),
            "fr-BE": (
                " The target language is Belgian French. Use Belgian French "
                "pronunciation and vocabulary (septante/ nonante instead of "
                "soixante-dix/ quatre-vingt-dix). Sound like you are from Brussels "
                "or Wallonia, not from Paris."
            ),
        }
        dialect_instruction = dialect_map.get(self._target_lang)
        system_instruction_text = base_instruction
        if dialect_instruction:
            system_instruction_text += dialect_instruction

        # Lexicon / fluency data for specific dialects (Itawit, Medumba, etc.)
        lexicon_instructions = get_lexicon_instructions(self._target_lang)
        if lexicon_instructions:
            system_instruction_text += lexicon_instructions

        # Append content-type-specific instruction block.
        if self._content_type == CONTENT_TYPE_MOVIE:
            system_instruction_text += (
                "\n\nMOVIE / CINEMATIC CONTENT \u2014 PROFESSIONAL DUBBING STUDIO "
                "MODE:\n"
                "The audio you are translating comes from a movie, TV show, or "
                "scripted cinematic content. You must deliver translated audio "
                "that sounds indistinguishable from a professionally dubbed film "
                "in the target language. The base rules still apply; the following "
                "are the professional dubbing studio standards you MUST follow:\n\n"
                "1. VOICE CASTING & CHARACTER SIGNATURE:\n"
                "  \u2022 Every character must have a distinct, recognisable vocal "
                "signature that persists for the entire film. The audience should "
                "be able to close their eyes and instantly know who is speaking "
                "from the voice alone.\n"
                "  \u2022 Cast the voice to match the character: deep, resonant "
                "tones for villains and authority figures; bright, nimble delivery "
                "for heroes and protagonists; breathy or wavering for vulnerable "
                "characters; aged, gravelly for elders; high and energetic for "
                "children and young sidekicks.\n"
                "  \u2022 Accents and dialects must be consistent per character. "
                "If a character speaks with a regional accent in the source, "
                "find an equivalent regional accent in the target language that "
                "conveys the same social cues (class, education, origin).\n"
                "  \u2022 Never let characters \u201csound like the same person\u201d "
                "\u2014 vocal variety between characters is essential for "
                "audience comprehension during rapid dialogue.\n\n"
                "2. DUBBING PERFORMANCE QUALITY:\n"
                "  \u2022 Deliver each line with the full emotional commitment "
                "of a professional voice actor. Lines must never sound flat, "
                "read, or phoned in.\n"
                "  \u2022 Match the original actor\u2019s performance energy "
                "exactly: if the original actor is crying, your delivery must "
                "sound like crying in the target language. If they are shouting "
                "in rage, you shout in rage. If they are whispering intimately, "
                "you whisper intimately.\n"
                "  \u2022 Preserve the original\u2019s breath pattern \u2014 "
                "gasps, sighs, sharp intakes of breath, breathless delivery, "
                "controlled breathing during tension. These carry massive "
                "emotional subtext.\n"
                "  \u2022 Line entrances and exits must be clean. Characters "
                "should not trail off weakly unless the original does. End "
                "sentences with the same energy they started with, unless the "
                "dramatic moment calls for a fade.\n"
                "  \u2022 Silence is part of the performance. If a character "
                "pauses before answering, that pause carries weight. Replicate "
                "its exact duration.\n\n"
                "3. DIALOGUE ADAPTATION (NOT LITERAL TRANSLATION):\n"
                "  \u2022 This is dubbing, not subtitling. Your job is to write "
                "dialogue that sounds like it was originally written in the "
                "target language by a professional screenwriter. Natural, "
                "snappy, idiomatic, and dramatically effective.\n"
                "  \u2022 Adapt jokes, puns, wordplay, and cultural references "
                "into equivalents that land in the target culture. A joke the "
                "audience doesn\u2019t get is worse than a different joke that "
                "lands. Never explain a pun \u2014 replace it.\n"
                "  \u2022 Adjust formality and register to match the character\u2019s "
                "social position and the scene\u2019s context. A king does not "
                "address his court the same way he addresses his child.\n"
                "  \u2022 Swear words, slang, and colloquialisms must be adapted "
                "to equivalents with the same emotional weight and social "
                "offensiveness in the target language \u2014 not sanitised.\n"
                "  \u2022 When a character uses a catchphrase or recurring "
                "verbal tic, translate it consistently every single time.\n\n"
                "4. LIP-SYNC & PHONETIC AWARENESS:\n"
                "  \u2022 Prioritise translations whose syllable count, stress "
                "pattern, and phrasing duration roughly match the original line. "
                "This makes the dubbed audio feel \u201cglued\u201d to the "
                "on-screen mouth movements.\n"
                "  \u2022 Pay extra attention to close-up shots where the "
                "audience sees the actor\u2019s mouth clearly \u2014 these "
                "lines need the tightest phonetic match.\n"
                "  \u2022 Avoid open vowels where the on-screen mouth is closed, "
                "and vice versa. Bilabial sounds (p, b, m) should roughly align "
                "with visible lip closures.\n"
                "  \u2022 When a perfect phonetic match is impossible, prioritise "
                "emotional truth and naturalness over rigid lip-sync. A slightly "
                "off-sync but emotionally perfect take is better than a "
                "mechanical on-sync take.\n\n"
                "5. GENRE-MASTERY \u2014 PACE, RHYTHM & REGISTER:\n"
                "  \u2022 Action: Breathless, urgent pacing. Short, clipped "
                "sentences. Sharp, staccato delivery. Every syllable punches. "
                "No wasted words or drawn-out pauses.\n"
                "  \u2022 Drama: Weighty, measured pacing. Allow pauses to "
                "breathe. Let tension build through silence. Rich, nuanced "
                "emotional delivery. Longer sentences with complex emotion.\n"
                "  \u2022 Comedy: Crisp, tight timing. The laugh comes from "
                "the delivery as much as the words. Punchlines must land with "
                "exact precision. Speed up for banter, slow for a deadpan "
                "one-liner. Rhythm is everything.\n"
                "  \u2022 Romance: Warmth, softness, intimacy in the voice. "
                "Gentle pacing, breathy delivery for tender moments. Build "
                "emotional intensity gradually through a scene.\n"
                "  \u2022 Horror/Thriller: Silence and controlled breathing "
                "are weapons. Whispered, tension-filled delivery. Sudden loud "
                "bursts for jump scares. Uneven, unpredictable pacing that "
                "keeps the audience off-balance.\n"
                "  \u2022 Animation/Family: Exaggerated, expressive delivery. "
                "Bigger emotional swings. Clear, bright enunciation. Higher "
                "energy baseline, with bigger contrasts between quiet and "
                "loud moments.\n\n"
                "6. SCENE FLOW & CONTINUITY:\n"
                "  \u2022 A scene is a dramatic unit with a beginning, middle, "
                "and end. Your delivery must arc with the scene \u2014 start "
                "at the scene\u2019s emotional entry point, build to its "
                "climax, and land at its resolution.\n"
                "  \u2022 Cross-scene character consistency: a character who "
                "is grieving in act 1 does not sound cheerful in act 2 unless "
                "the story justifies it. Track the character\u2019s emotional "
                "journey across the entire film.\n"
                "  \u2022 Group scenes with overlapping dialogue: distinguish "
                "each voice clearly even during interruptions, cross-talk, "
                "and multi-character arguments. The listener must be able to "
                "follow who is saying what at all times.\n"
                "  \u2022 Maintain ambient consistency: the acoustic space "
                "(echoey hall, intimate room, outdoor wind) should feel "
                "consistent within a scene. Don\u2019t jump between vocal "
                "proximities without the scene changing.\n\n"
                "7. FORBIDDEN IN PROFESSIONAL DUBBING:\n"
                "  \u2022 NEVER sound like a translator or interpreter reading "
                "a script. No flat, educational, or documentary-presenter tone.\n"
                "  \u2022 NEVER leave source-language words untranslated or "
                "insert \u201c[unintelligible]\u201d into audio delivery \u2014 "
                "adapt around content you cannot parse.\n"
                "  \u2022 NEVER break character. If a character speaks in a "
                "consistent dialect or idiolect, stay in it 100% of the time.\n"
                "  \u2022 NEVER sanitise, soften, or \u201cmake polite\u201d "
                "a character\u2019s rough edges. A toxic character must still "
                "sound toxic. A funny character must still sound funny.\n"
                "  \u2022 NEVER add words that weren\u2019t in the original "
                "to \u201cfill space\u201d \u2014 every word in your output "
                "must correspond to something the character actually conveyed.\n"
                "  \u2022 NEVER sound like you are reading. It must sound like "
                "you ARE the character, saying these words for the first time."
            )

        elif self._content_type == CONTENT_TYPE_CINEMATIC_FAITHFUL:
            voice_pool_block = self._voice_pool_text()
            system_instruction_text += (
                "\n\n"
                "CINEMATIC FAITHFUL TRANSLATION \u2014 LIVE DIRECTOR MODE:\n"
                "You are a live cinematic dubbing director. "
                "The audio you receive is dynamic \u2014 it may come from a "
                "shared-screen video, a live stream, or an uploaded file. "
                "Treat the audio as the PRIMARY source of truth. "
                "The raw transcript (if any) is only a helper reference. "
                "Listen to the audio carefully and follow these rules:\n\n"
                "--- PRIMARY RULES ---\n\n"
                "1. AUDIO SOURCE COMES FIRST:\n"
                "  \u2022 Use the audio to determine speaker changes, emotion, "
                "tone, pacing, pauses, intensity, and character identity.\n"
                "  \u2022 If a raw transcript exists but conflicts with the "
                "audio, follow the audio.\n"
                "  \u2022 Do NOT rely only on a written transcript. The "
                "performance is in the sound.\n\n"
                "2. TRANSLATE MEANING, NOT JUST WORDS:\n"
                "  \u2022 The translation MUST carry the same emotional weight "
                "as the source audio.\n"
                "  \u2022 Preserve the speaker\u2019s intention, authority, "
                "fear, sadness, anger, restraint, exhaustion, pride, "
                "tenderness, or grief.\n"
                "  \u2022 Avoid flat literal translation when it weakens the "
                "scene. Use natural phrasing in the target language while "
                "keeping the original cinematic nuance.\n\n"
                "3. MIMIC THE NUANCE OF THE AUDIO:\n"
                "  \u2022 Match the speaking speed, rhythm, silence, hesitation, "
                "pauses, breath, and dramatic emphasis perfectly. "
                "If the original speaker talks fast, you MUST speak at the same speed. "
                "If they speak slow, match that slow speed.\n"
                "  \u2022 If a line is whispered, solemn, commanding, broken, "
                "emotional, or reflective, the translated line MUST feel "
                "the same.\n"
                "  \u2022 Do NOT modernize, soften, exaggerate, or over-explain "
                "the dialogue unless the audio clearly supports it.\n\n"
                "4. ASSIGN SPEAKERS BASED ON CHARACTERS:\n"
                "  \u2022 Identify every speaker using the AUDIO source.\n"
                "  \u2022 Use recognizable character names when possible. "
                "If the character is unknown, use \u201cUnknown Speaker 1\u201d, "
                "\u201cUnknown Speaker 2\u201d, etc.\n"
                "  \u2022 Each speaker MUST have a voice profile based on "
                "the character\u2019s sound and role.\n\n"
                "5. ONLY ONE SPEAKER ACTIVE AT A TIME:\n"
                "  \u2022 Every output segment MUST identify exactly one "
                "active speaker.\n"
                "  \u2022 Do NOT create multiple simultaneous hosts or "
                "speakers in one segment.\n"
                "  \u2022 The speaker label MUST show who is speaking.\n"
                "  \u2022 All other characters are treated as not speaking "
                "during that segment.\n\n"
                "6. SPEAKER TAGS IN OUTPUT:\n"
                "  \u2022 Prepend the output transcription with "
                "\u201c[A] \u201d, \u201c[B] \u201d, etc. for each speaker "
                "in order of first appearance.\n"
                "  \u2022 If a speaker is identifiable by name from context, "
                "use their name tag: \u201c[King Aragorn] \u201d.\n"
                "  \u2022 Re-use existing tags when a speaker resumes \u2014 "
                "NEVER assign a new letter to the same speaker.\n"
                "  \u2022 The speaker tag MUST match the voice assigned "
                "from the voice pool below.\n\n"
                "--- VOICE CASTING ---\n\n"
                "7. VOICE-CASTING MAP:\n"
                "  \u2022 On the FIRST appearance of each new speaker, you "
                "MUST add a voice-casting JSON block to the output "
                "transcription. Format:\n"
                '    <cast>{"speaker":"A","name":"Character Name",'
                '"role":"protagonist / antagonist / supporting / narrator",'
                '"gender":"male / female / unclear",'
                '"age":"young adult / middle-aged / elderly",'
                '"tone":"deep / bright / raspy / smooth / warm / cold",'
                '"emotion":"stoic / volatile / warm / haunted / authoritative",'
                '"pace":"slow / medium / fast / varied",'
                '"accent":"none / regional / aristocratic / foreign",'
                '"voice_id":"Zephyr / Kore / Orus / ...",'
                '"notes":"brief description of vocal character"}</cast>\n"'
                "  \u2022 The voice_id MUST come from the AVAILABLE VOICE "
                "POOL below. Assign in order of speaker appearance.\n"
                "  \u2022 Once cast, that voice_id is PERMANENT for the "
                "entire session.\n\n"
                "AVAILABLE VOICE POOL:\n" + voice_pool_block + "\n\n"
                "--- PERFORMANCE MARKERS ---\n\n"
                "8. PRESERVE CINEMATIC PERFORMANCE:\n"
                "  \u2022 Use pause/emotion markers when supported by the "
                "audio. Valid markers:\n"
                "    [pause] \u2014 brief hesitation\n"
                "    [long pause] \u2014 extended silence\n"
                "    [breath] \u2014 audible inhale or exhale\n"
                "    [whispered] \u2014 very low volume, intimate\n"
                "    [strained] \u2014 voice under effort or stress\n"
                "    [commanding] \u2014 authoritative, forceful\n"
                "    [softly] \u2014 gentle, tender delivery\n"
                "    [voice breaking] \u2014 cracking with emotion\n"
                "    [shouted] \u2014 full-voice cry or yell\n"
                "    [crosstalk] \u2014 overlapping dialogue\n"
                "  \u2022 Place markers INLINE in the output transcription "
                "where the audio supports them. E.g.:\n"
                '    "[A] I will not [pause] I cannot do this. [breath]"\n'
                "  \u2022 NEVER insert markers that are not supported by "
                "the audio.\n\n"
                "9. OUTPUT TRANSCRIPTION SPEAKER TAGS WITH MARKERS:\n"
                "  \u2022 Format for each output transcription segment:\n"
                '    "[A] Translated line. [marker] Next sentence."\n'
                "  \u2022 Markers go INSIDE the speaker\u2019s block, "
                "after the speaker tag.\n\n"
                "--- TRANSLATION STYLE ---\n\n"
                "10. TRANSLATION STYLE:\n"
                "  \u2022 Translation mode: cinematic faithful translation.\n"
                "  \u2022 Keep names, titles, places, and cultural "
                "references accurate.\n"
                "  \u2022 Preserve honorifics, rank, and formal/informal "
                "speech levels where relevant.\n"
                "  \u2022 Do NOT censor, sanitize, or simplify unless "
                "required by the target language.\n"
                "  \u2022 Your audio delivery MUST carry the same dramatic "
                "weight as the original speaker.\n\n"
                "--- CHARACTER VOICE EXAMPLES ---\n\n"
                "When casting voices, follow these archetype guidelines:\n"
                "  \u2022 Elder ruler / emperor: older male, low register, "
                "slow pace, weary authority, reflective sadness, "
                "restrained power.\n"
                "  \u2022 Soldier / general: mature male, controlled tone, "
                "grounded delivery, disciplined emotion, strong but not "
                "theatrical.\n"
                "  \u2022 Narrator: calm cinematic voice, reflective "
                "cadence, intimate but epic, slow-to-medium pace.\n"
                "  \u2022 Warrior / challenger: deep physical voice, "
                "aggressive energy, heavier breath, direct and forceful "
                "delivery.\n"
                "  \u2022 Never assign the same voice profile to every "
                "speaker unless the audio clearly has only one speaker.\n"
            )

        return {
            "setup": {
                "model": f"models/{GEMINI_MODEL}",
                "systemInstruction": {"parts": [{"text": system_instruction_text}]},
                "outputAudioTranscription": {},
                "inputAudioTranscription": {},
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "translationConfig": {
                        "targetLanguageCode": self._base_lang_code(self._target_lang),
                        "echoTargetLanguage": True,
                    },
                },
                "realtimeInputConfig": {
                    "automaticActivityDetection": {"disabled": False},
                },
            }
        }

    async def _pump_input(
        self,
        ws: websockets.WebSocketClientProtocol,
        setup_complete: asyncio.Event,
    ) -> None:
        """Read PCM from the speaker's track and forward to Gemini as base64."""
        # Don't start streaming audio until Gemini acknowledges setup; otherwise
        # the model has nothing telling it what to do with the bytes.
        await setup_complete.wait()
        sent = 0
        mime = f"audio/pcm;rate={GEMINI_INPUT_SAMPLE_RATE}"
        async for pcm in iter_pcm_for_gemini(self._speaker_track):
            if self._closed.is_set():
                return
            b64 = base64.b64encode(pcm).decode("ascii")
            msg = {
                "realtimeInput": {
                    "audio": {
                        "mimeType": mime,
                        "data": b64,
                    }
                }
            }
            await ws.send(json.dumps(msg))
            sent += 1
            if sent in (1, 50) or sent % 500 == 0:
                logger.info(
                    "eburon <- %s frames=%d (%s mic in)",
                    self._target_lang,
                    sent,
                    self._speaker_identity,
                )

    async def _pump_output(
        self,
        ws: websockets.WebSocketClientProtocol,
        setup_complete: asyncio.Event,
    ) -> None:
        """Receive Gemini translated audio + transcription, route into the room."""
        audio_frames = 0
        text_chunks = 0
        _first_content_seen = False
        async for raw in ws:
            if self._closed.is_set():
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("ignoring non-JSON WS frame")
                continue

            if msg.get("setupComplete") is not None:
                logger.info(
                    "Eburon setup complete: %s -> %s",
                    self._speaker_identity,
                    self._target_lang,
                )
                self._consecutive_failures = 0
                setup_complete.set()
                continue

            sc = msg.get("serverContent")
            if not sc:
                # Log unrecognized message types once per session for debugging
                if not _first_content_seen:
                    logger.debug(
                        "Eburon non-serverContent msg (%s -> %s): keys=%s",
                        self._speaker_identity,
                        self._target_lang,
                        list(msg.keys())[:5],
                    )
                continue

            if not _first_content_seen:
                _first_content_seen = True
                logger.info(
                    "Eburon first serverContent (%s -> %s): keys=%s",
                    self._speaker_identity,
                    self._target_lang,
                    list(sc.keys()),
                )

            # Translated audio frames.
            model_turn = sc.get("modelTurn")
            if model_turn is not None:
                for part in model_turn.get("parts", []) or []:
                    inline = part.get("inlineData")
                    if inline and inline.get("data"):
                        pcm = base64.b64decode(inline["data"])
                        await push_pcm_to_source(self._audio_source, pcm)
                        audio_frames += 1
                        if audio_frames in (1, 10, 100) or audio_frames % 500 == 0:
                            logger.info(
                                "eburon -> %s frames=%d (%s -> %s)",
                                self._target_lang,
                                audio_frames,
                                self._speaker_identity,
                                self._target_lang,
                            )

            # --- OUTPUT TRANSCRIPTION (translated text) ---
            ot = sc.get("outputTranscription")
            if not ot and model_turn is not None:
                ot = model_turn.get("outputTranscription")
            if ot and ot.get("text"):
                ot_text = ot["text"]
                # Parse structured [OUTPUT]...[/OUTPUT] segments
                output_segments = self._parse_output_segments(ot_text)
                for seg in output_segments:
                    self._append_history("target", seg)
                    self._pending_segments.append(seg)
                # Publish the plain text version for backward-compatible captions
                await self._publish_transcript(ot_text, final=False)
                # For cinematic faithful mode, parse cast blocks
                if self._content_type == CONTENT_TYPE_CINEMATIC_FAITHFUL:
                    self._parse_cinematic_output(ot_text)

            # --- SOURCE TRANSCRIPTION (what the speaker said) ---
            it = sc.get("inputTranscription")
            if it and it.get("text"):
                it_text = it["text"]
                # Parse structured [SEGMENT]...[/SEGMENT] segments
                source_segments = self._parse_source_segments(it_text)
                for seg in source_segments:
                    self._append_history("source", seg)
                    self._pending_segments.append(seg)
                # Publish the plain text version for backward-compatible captions
                await self._publish_source_transcript(it_text, final=False)
                text_chunks += 1
                if text_chunks in (1, 10) or text_chunks % 50 == 0:
                    logger.info(
                        "eburon transcript chunk #%d for %s -> %s",
                        text_chunks,
                        self._speaker_identity,
                        self._target_lang,
                    )

            if sc.get("turnComplete"):
                await self._publish_transcript("", final=True)
                # Publish accumulated structured segments as JSON
                await self._publish_structured_json()
                # For cinematic faithful mode, publish the structured segment summary
                if self._content_type == CONTENT_TYPE_CINEMATIC_FAITHFUL:
                    await self._publish_cinematic_json()

    def _append_history(self, kind: str, segment: dict) -> None:
        """Add a structured segment to rolling memory, capping at limits."""
        # Normalize: output segments may use "translated_text" — store as "text" too
        if "translated_text" in segment and "text" not in segment:
            segment["text"] = segment["translated_text"]
        if not segment.get("text", "").strip():
            return
        segment["kind"] = kind  # "source" or "target"
        self._segment_history.append(segment)
        # Drop oldest entries if over count limit
        while len(self._segment_history) > MAX_HISTORY_SEGMENTS:
            self._segment_history.pop(0)

    @staticmethod
    def _parse_source_segments(text: str) -> list[dict]:
        """Parse structured [SEGMENT]...[/SEGMENT] blocks from source transcription.

        Returns a list of segment dicts. If no structured segments are found,
        falls back to creating a simple segment from the raw text.
        """
        matches = SEGMENT_RE.findall(text)
        if not matches:
            # Fallback: create a basic segment from plain text
            cleaned = text.strip()
            if not cleaned:
                return []
            return [
                {
                    "speaker_id": "speaker_1",
                    "speaker_label": "",
                    "text": cleaned,
                    "language": "",
                    "confidence": 0.0,
                    "tone": "",
                    "emotion": "",
                    "speech_style": "",
                    "overlap_status": "none",
                    "active_speaker": True,
                }
            ]

        segments: list[dict] = []
        for raw in matches:
            raw = raw.strip()
            if not raw:
                continue
            try:
                seg = json.loads(raw)
                # Ensure required fields
                seg.setdefault("speaker_id", "speaker_1")
                seg.setdefault("speaker_label", "")
                seg.setdefault("text", "")
                seg.setdefault("language", "")
                seg.setdefault("confidence", 0.0)
                seg.setdefault("tone", "")
                seg.setdefault("emotion", "")
                seg.setdefault("speech_style", "")
                seg.setdefault("overlap_status", "none")
                seg.setdefault("active_speaker", True)
                segments.append(seg)
            except (json.JSONDecodeError, TypeError):
                logger.debug("failed to parse source segment JSON: %s", raw[:80])
        return segments

    @staticmethod
    def _parse_output_segments(text: str) -> list[dict]:
        """Parse structured [OUTPUT]...[/OUTPUT] blocks from translated output.

        Returns a list of segment dicts. Falls back to raw text if no markers found.
        """
        matches = OUTPUT_RE.findall(text)
        if not matches:
            cleaned = text.strip()
            if not cleaned:
                return []
            return [GeminiSession._make_fallback_output_segment(cleaned)]

        segments: list[dict] = []
        for raw in matches:
            raw = raw.strip()
            if not raw:
                continue
            try:
                seg = json.loads(raw)
                seg.setdefault("speaker_id", "speaker_1")
                seg.setdefault("speaker_label", "")
                seg.setdefault("translated_text", "")
                seg.setdefault("confidence", 0.0)
                seg.setdefault("nuance_notes", "")
                # Normalize text key for history storage
                if "translated_text" in seg and "text" not in seg:
                    seg["text"] = seg["translated_text"]
                segments.append(seg)
            except (json.JSONDecodeError, TypeError):
                logger.debug("failed to parse output segment JSON: %s", raw[:80])
        return segments

    @staticmethod
    def _make_fallback_output_segment(text: str) -> dict:
        """Create a basic output segment from plain text when [OUTPUT] markers are absent."""
        return {
            "speaker_id": "speaker_1",
            "speaker_label": "",
            "text": text,
            "translated_text": text,
            "confidence": 0.0,
            "nuance_notes": "",
        }

    async def _publish_structured_json(self) -> None:
        """Publish accumulated source+target segments as structured JSON at turn end."""
        if not self._pending_segments:
            return

        payload = {
            "type": "structured_segments",
            "conversation_id": self._conversation_id,
            "target_lang": self._target_lang,
            "source_identity": self._speaker_identity,
            "segments": list(self._pending_segments),
        }

        try:
            writer = await self._room.local_participant.stream_text(
                topic="lk.translation",
                sender_identity=self._speaker_identity,
                attributes={
                    "target_lang": self._target_lang,
                    "source_identity": self._speaker_identity,
                    "kind": "structured_json",
                    "final": "true",
                },
            )
            await writer.write(json.dumps(payload, ensure_ascii=False))
            await writer.aclose()
        except Exception as exc:
            logger.debug("structured JSON publish failed: %s", exc)

        # Clear pending segments
        self._pending_segments.clear()

    async def _publish_transcript(self, text: str, *, final: bool) -> None:
        """Best-effort text-stream publish. Frontend filters by attributes."""
        if not text and not final:
            return
        try:
            # Send each chunk as its own text-stream message; frontend appends.
            writer = await self._room.local_participant.stream_text(
                topic="lk.translation",
                sender_identity=self._speaker_identity,
                attributes={
                    "target_lang": self._target_lang,
                    "source_identity": self._speaker_identity,
                    "final": "true" if final else "false",
                },
            )
            if text:
                await writer.write(text)
            await writer.aclose()
        except Exception as exc:
            logger.debug("text-stream publish failed: %s", exc)

    def _parse_cinematic_output(self, text: str) -> None:
        """Extract cast blocks and accumulate segments from cinematic faithful output.

        Gemini outputs text like:
          <cast>{"speaker":"A","name":"Aragorn",...}</cast>
          [A] I will not [pause] I cannot do this. [breath]

        This method:
        1. Extracts <cast>...</cast> blocks and stores them in _voice_casting_map.
        2. Strips cast blocks from the text and accumulates the remaining
           speaker-tagged segments in _cinematic_segments.
        """
        import re

        # Extract all <cast>...</cast> blocks
        while True:
            m = re.search(r"<cast>(.*?)</cast>", text, re.DOTALL)
            if not m:
                break
            raw = m.group(1).strip()
            text = text[: m.start()] + text[m.end() :]
            try:
                cast = json.loads(raw)
                speaker_letter = cast.get("speaker", "")
                if speaker_letter:
                    self._voice_casting_map[speaker_letter] = cast
                    logger.info(
                        "cinematic cast: %s -> %s (voice=%s)",
                        speaker_letter,
                        cast.get("name", "?"),
                        cast.get("voice_id", "?"),
                    )
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("cinematic cast parse error: %s", exc)

        # Accumulate non-empty speaker-tagged segments
        stripped = text.strip()
        if stripped:
            # Detect speaker tag: [A], [King], etc.
            tag_m = re.match(r"^\[([^\]]+)\]\s*(.*)", stripped, re.DOTALL)
            if tag_m:
                speaker_tag = tag_m.group(1)
                dialogue = tag_m.group(2).strip()
                self._cinematic_segments.append(
                    {
                        "speaker_tag": speaker_tag,
                        "dialogue": dialogue,
                        "character_name": (
                            self._voice_casting_map.get(speaker_tag, {}).get(
                                "name", speaker_tag
                            )
                        ),
                    }
                )
            else:
                # No speaker tag — accumulate as continuation
                if self._cinematic_segments:
                    self._cinematic_segments[-1]["dialogue"] += " " + stripped
                else:
                    self._cinematic_segments.append(
                        {
                            "speaker_tag": "?",
                            "dialogue": stripped,
                            "character_name": "?",
                        }
                    )

    async def _publish_cinematic_json(self) -> None:
        """Publish the accumulated cinematic segments as structured JSON.

        The JSON is published as a text-stream entry with a 'cinematic' kind
        attribute so the frontend can distinguish it from regular captions.
        """
        if not self._cinematic_segments:
            return

        # Build voice casting array from the map
        voice_cast = []
        for letter in sorted(self._voice_casting_map.keys()):
            voice_cast.append(self._voice_casting_map[letter])

        payload = {
            "type": "cinematic_segments",
            "target_lang": self._target_lang,
            "source_identity": self._speaker_identity,
            "voice_cast": voice_cast,
            "segments": list(self._cinematic_segments),
        }

        try:
            writer = await self._room.local_participant.stream_text(
                topic="lk.translation",
                sender_identity=self._speaker_identity,
                attributes={
                    "target_lang": self._target_lang,
                    "source_identity": self._speaker_identity,
                    "kind": "cinematic_json",
                    "final": "true",
                },
            )
            await writer.write(json.dumps(payload, ensure_ascii=False))
            await writer.aclose()
        except Exception as exc:
            logger.debug("cinematic JSON publish failed: %s", exc)

        # Clear accumulated segments after publishing
        self._cinematic_segments.clear()

    async def _publish_source_transcript(self, text: str, *, final: bool) -> None:
        """Publish source transcription (what the speaker said in their language)."""
        if not text:
            return
        try:
            writer = await self._room.local_participant.stream_text(
                topic="lk.translation",
                sender_identity=self._speaker_identity,
                attributes={
                    "target_lang": self._target_lang,
                    "source_identity": self._speaker_identity,
                    "kind": "source",
                    "final": "true" if final else "false",
                },
            )
            await writer.write(text)
            await writer.aclose()
        except Exception as exc:
            logger.debug("source transcript publish failed: %s", exc)

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

import websockets
from livekit import rtc

from audio import iter_pcm_for_gemini, make_audio_source, push_pcm_to_source
from config import (
    GEMINI_INPUT_SAMPLE_RATE,
    GEMINI_MAX_FAILURES_BEFORE_LONG_BACKOFF,
    GEMINI_MODEL,
    GEMINI_RECONNECT_BACKOFF_SEC,
    MAX_HISTORY_WORDS,
    MAX_TRANSCRIPT_HISTORY,
    NATIVE_LANG,
    PARTICIPANT_LANG_ATTR,
)

logger = logging.getLogger("translator.session")


GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)


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
    ) -> None:
        self._room = room
        self._speaker_identity = speaker_identity
        self._speaker_track = speaker_track
        self._track_source = track_source
        self._target_lang = target_lang
        self._gemini_api_key = gemini_api_key
        self._glossary = glossary or []

        participant = self._room.remote_participants.get(self._speaker_identity)
        source_lang = (participant.attributes or {}).get(PARTICIPANT_LANG_ATTR) if participant else None
        self._source_lang = None if source_lang == NATIVE_LANG else source_lang

        self._audio_source = make_audio_source()
        self._local_track: rtc.LocalAudioTrack | None = None
        self._track_sid: str | None = None
        self._consecutive_failures = 0
        self._tasks: list[asyncio.Task] = []
        self._closed = asyncio.Event()
        # Rolling translation memory: list of (kind, text) tuples where
        # kind is "source" or "target". Used to re-inject context on reconnect.
        self._transcript_history: list[tuple[str, str]] = []

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
            "2. You MUST sound like a real person having a real conversation. "
            "Use natural contractions, common idioms, and colloquial flow "
            "appropriate to the target language and the speaker\u2019s register "
            "(formal, casual, professional, etc.). Avoid literal word-for-word "
            "translations \u2014 instead, convey the MEANING and INTENT using "
            "the most natural expression a native speaker would choose.\n"
            "3. Use the recent conversation context below (under IMPORTANT "
            "CONTEXT) to disambiguate ambiguous terms, maintain topic "
            "consistency, choose the correct formality level, and produce "
            "domain-appropriate vocabulary. If a speaker has glossary terms "
            "defined, you MUST honour those exact translations.\n"
            "4. Never summarize, truncate, sanitize, or soften the original "
            "message. Preserve every point, every emotion, every nuance."
            "\n\n"
            "VOCAL MIMICRY \u2014 YOU MUST SOUND LIKE THE SOURCE SPEAKER:\n"
            "Your output audio must be a 100% faithful vocal reproduction of "
            "the source speaker. Copy their speed, rhythm, pauses, pitch "
            "contour, volume, and intonation exactly. Every micro-timing, "
            "every breath, every hesitation must be replicated identically.\n"
            "- If they laugh, giggle, or chuckle \u2014 you produce the same "
            "laugh, giggle, or chuckle at the same duration and intensity.\n"
            "- If they sigh \u2014 you sigh the same way.\n"
            "- If they whisper \u2014 you whisper at the same volume.\n"
            "- If they speak fast \u2014 you speak exactly as fast.\n"
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
            "identical vocal delivery."
            f"\n\n"
            f"You MUST translate the input into the language with code "
            f"'{self._target_lang}'. All output audio and text must be in "
            f"that language."
        )

        if self._source_lang:
            base_instruction += (
                f"\n\nCRITICAL STT ACCURACY & AUTO-DETECTION: The source speaker's language is dynamic and "
                f"may change at any time (though their primary profile language is '{self._source_lang}'). "
                f"You MUST first accurately auto-detect the language being spoken and completely and "
                f"perfectly transcribe the source audio in that detected language. Accurate language detection "
                f"and transcription are essential; any errors will cause a domino effect on the translation. "
                f"Pay close attention to phonetics, context, and domain terminology to ensure a perfect "
                f"transcription before translating to '{self._target_lang}'."
            )
        else:
            base_instruction += (
                f"\n\nCRITICAL STT ACCURACY & AUTO-DETECTION: The source speaker's language is dynamic and "
                f"may change at any time. You MUST first accurately auto-detect the language being spoken "
                f"and completely and perfectly transcribe the source audio in that detected language. "
                f"Accurate language detection and transcription are essential; any errors will cause a "
                f"domino effect on the translation. Pay close attention to phonetics, context, and domain "
                f"terminology to ensure a perfect transcription before translating to '{self._target_lang}'."
            )

        # Inject rolling translation memory (recent transcript context).
        if self._transcript_history:
            total_words = 0
            context_lines: list[str] = []
            # Walk in reverse (newest first) until we hit the word cap.
            for kind, text in reversed(self._transcript_history):
                words = len(text.split())
                if total_words + words > MAX_HISTORY_WORDS:
                    break
                total_words += words
                prefix = "Speaker said" if kind == "source" else "Translation"
                context_lines.insert(0, f"  {prefix}: {text}")
            if context_lines:
                base_instruction += (
                    "\n\nIMPORTANT CONTEXT from the conversation so far:\n"
                    f"{chr(10).join(context_lines)}"
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

        return {
            "setup": {
                "model": f"models/{GEMINI_MODEL}",
                "systemInstruction": {
                    "parts": [{"text": system_instruction_text}]
                },
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

            # Translated transcript -> text stream for the captions sidebar.
            # The outputTranscription field may appear at the serverContent level
            # (as documented in the v1beta proto) or nested inside modelTurn
            # (observed in some API versions). Check both locations.
            ot = sc.get("outputTranscription")
            if not ot and model_turn is not None:
                ot = model_turn.get("outputTranscription")
            if ot and ot.get("text"):
                await self._publish_transcript(ot["text"], final=False)

            # Source transcription (what the speaker said in their language)
            it = sc.get("inputTranscription")
            if it and it.get("text"):
                await self._publish_source_transcript(it["text"], final=False)
                # Append to rolling memory
                self._append_history("source", it["text"])
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

    def _append_history(self, kind: str, text: str) -> None:
        """Add an entry to the rolling transcript memory, capping at limits."""
        text = text.strip()
        if not text:
            return
        self._transcript_history.append((kind, text))
        # Drop oldest entries if over count limit
        while len(self._transcript_history) > MAX_TRANSCRIPT_HISTORY:
            self._transcript_history.pop(0)

    async def _publish_transcript(self, text: str, *, final: bool) -> None:
        """Best-effort text-stream publish. Frontend filters by attributes."""
        if not text and not final:
            return
        # Append target transcript to rolling memory (only on complete utterances)
        if text:
            self._append_history("target", text)
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

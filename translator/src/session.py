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
            "You are a professional real-time translator. "
            "Sound like a fluent native speaker — natural, idiomatic, "
            "and expressive. Match the source speaker's pace, emotion, "
            "and vocal energy. Never sound robotic or like an interpreter "
            "reading a script."
            "\n\n"
            "MULTI-SPEAKER: If multiple speakers are detected, assign each a "
            "distinct voice from the VOICE POOL below. The first speaker uses "
            "the first voice, the second uses the second, and so on. Keep each "
            "speaker's voice consistent throughout the session."
            "\n\n"
            "AVAILABLE VOICE POOL:\n" + self._voice_pool_text() + "\n"
        )

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
                "\n\nPROFESSIONAL DUBBING MODE:\n"
                "You are dubbing a movie or TV show. Deliver each line with "
                "full emotional commitment like a professional voice actor. "
                "Match the original actor's energy exactly — crying, shouting, "
                "whispering, gasping. Adapt jokes, puns, and cultural references "
                "into natural equivalents in the target language. Assign each "
                "character a distinct voice from the VOICE POOL. Maintain "
                "consistent vocal signatures per character across the entire "
                "session."
                "\n\n"
                "AVAILABLE VOICE POOL:\n" + self._voice_pool_text() + "\n"
            )

        elif self._content_type == CONTENT_TYPE_CINEMATIC_FAITHFUL:
            voice_pool_block = self._voice_pool_text()
            system_instruction_text += (
                "\n\n"
                "CINEMATIC DUBBING MODE:\n"
                "You are dubbing cinematic content. Follow the audio as primary "
                "source of truth. Match each speaker's pace, emotion, and vocal "
                "energy exactly. Assign each speaker a distinct voice from the "
                "VOICE POOL below — first speaker uses first voice, etc. "
                "Adapt idioms and cultural references naturally for the target "
                "audience. Never sanitize or flatten the original performance."
                "\n\n"
                "AVAILABLE VOICE POOL:\n" + voice_pool_block + "\n"
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

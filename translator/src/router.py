"""Reconciles the set of active GeminiSession instances to room demand."""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from livekit import rtc

from config import (
    GLOSSARY_ATTR,
    NATIVE_LANG,
    PARTICIPANT_LANG_ATTR,
    RECONCILE_DEBOUNCE_SEC,
    SESSION_GRACE_SEC,
)
from session import GeminiSession

logger = logging.getLogger("translator.router")

# (speaker_identity, track_sid, target_lang)
SessionKey = tuple[str, str, str]


class TranslationRouter:
    """Owns the room's translation-session lifecycle.

    Demand model (from grill Q16):
      A session (S, T) exists iff there is at least one listener with lang == T
      AND speaker S has an enabled mic track AND S.lang != T.

    Mute or last-listener-leaves triggers a SESSION_GRACE_SEC teardown so brief
    coughs/toggles don't thrash Gemini connections.
    """

    def __init__(self, room: rtc.Room, gemini_api_key: str) -> None:
        self._room = room
        self._gemini_api_key = gemini_api_key

        # Per-speaker mic track that is currently subscribed and unmuted.
        self._speaker_tracks: dict[str, dict[str, rtc.RemoteAudioTrack]] = {}

        # Active sessions keyed by (speaker_identity, target_lang).
        self._sessions: dict[SessionKey, GeminiSession] = {}

        # Pending teardown timers keyed the same way.
        self._grace_tasks: dict[SessionKey, asyncio.Task] = {}

        # Detached close tasks (fire-and-forget); we keep references to prevent
        # the GC from collecting them mid-shutdown.
        self._detached_tasks: set[asyncio.Task] = set()

        self._reconcile_handle: asyncio.TimerHandle | None = None
        self._reconcile_lock = asyncio.Lock()

    # --- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        room = self._room

        @room.on("participant_connected")
        def _on_conn(_: rtc.RemoteParticipant) -> None:
            self._schedule_reconcile()

        @room.on("data_received")
        def _on_data_received(data: rtc.DataPacket) -> None:
            if data.topic == "retranslation_request":
                asyncio.create_task(self._handle_retranslation(data))

        @room.on("participant_disconnected")
        def _on_disc(p: rtc.RemoteParticipant) -> None:
            self._on_participant_left(p.identity)
            self._schedule_reconcile()

        @room.on("participant_attributes_changed")
        def _on_attrs(_changed: dict[str, str], _p: rtc.Participant) -> None:
            self._schedule_reconcile()

        @room.on("track_subscribed")
        def _on_subscribed(
            track: rtc.Track,
            _pub: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO and isinstance(
                track, rtc.RemoteAudioTrack
            ):
                if participant.identity not in self._speaker_tracks:
                    self._speaker_tracks[participant.identity] = {}
                self._speaker_tracks[participant.identity][track.sid] = track
                self._schedule_reconcile()

        @room.on("track_unsubscribed")
        def _on_unsubscribed(
            track: rtc.Track,
            _pub: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                if participant.identity in self._speaker_tracks:
                    self._speaker_tracks[participant.identity].pop(track.sid, None)
                    if not self._speaker_tracks[participant.identity]:
                        del self._speaker_tracks[participant.identity]
                self._schedule_reconcile()

        @room.on("track_muted")
        def _on_muted(_pub: rtc.TrackPublication, _p: rtc.Participant) -> None:
            self._schedule_reconcile()

        @room.on("track_unmuted")
        def _on_unmuted(_pub: rtc.TrackPublication, _p: rtc.Participant) -> None:
            self._schedule_reconcile()

        # Backfill any participants/tracks already present at startup.
        for p in room.remote_participants.values():
            for pub in p.track_publications.values():
                if (
                    pub.track
                    and pub.kind == rtc.TrackKind.KIND_AUDIO
                    and isinstance(pub.track, rtc.RemoteAudioTrack)
                ):
                    if p.identity not in self._speaker_tracks:
                        self._speaker_tracks[p.identity] = {}
                    self._speaker_tracks[p.identity][pub.track.sid] = pub.track

        self._schedule_reconcile()

    async def aclose(self) -> None:
        if self._reconcile_handle:
            self._reconcile_handle.cancel()
            self._reconcile_handle = None

        for task in self._grace_tasks.values():
            task.cancel()
        self._grace_tasks.clear()

        await asyncio.gather(
            *(s.aclose() for s in self._sessions.values()),
            return_exceptions=True,
        )
        self._sessions.clear()

    # --- Reconciliation ----------------------------------------------------

    def _schedule_reconcile(self) -> None:
        loop = asyncio.get_event_loop()
        if self._reconcile_handle is not None:
            self._reconcile_handle.cancel()
        self._reconcile_handle = loop.call_later(
            RECONCILE_DEBOUNCE_SEC,
            lambda: asyncio.create_task(self._reconcile()),
        )

    async def _reconcile(self) -> None:
        async with self._reconcile_lock:
            desired = self._compute_desired_sessions()
            existing = set(self._sessions.keys())

            # Cancel any pending grace teardowns for sessions we still want.
            for key in desired & set(self._grace_tasks.keys()):
                task = self._grace_tasks.pop(key)
                task.cancel()

            # Schedule grace teardown for sessions no longer desired.
            for key in existing - desired:
                if key not in self._grace_tasks:
                    self._grace_tasks[key] = asyncio.create_task(
                        self._grace_teardown(key)
                    )

            # Start newly-desired sessions.
            for key in desired - existing:
                if key in self._grace_tasks:
                    # Race: an old session is still cooling down — let it finish
                    # before starting a new one. Reschedule.
                    continue
                speaker_identity, track_sid, target_lang = key
                tracks = self._speaker_tracks.get(speaker_identity, {})
                track = tracks.get(track_sid)
                if track is None:
                    continue

                # Determine a source name to help the frontend logic.
                # Use publication source since RemoteAudioTrack has no .source.
                participant = self._room.remote_participants.get(speaker_identity)
                pub = (
                    participant.track_publications.get(track_sid)
                    if participant
                    else None
                )
                source_str = (
                    "screen_share_audio"
                    if pub is not None
                    and pub.source == rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO
                    else "mic"
                )

                # Read glossary and content type from the speaker's participant attributes
                glossary: list[dict[str, str]] = []
                content_type = "normal"
                if participant:
                    raw = (participant.attributes or {}).get(GLOSSARY_ATTR, "")
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            if isinstance(parsed, list):
                                glossary = parsed
                        except (json.JSONDecodeError, TypeError):
                            logger.debug("invalid glossary attr for %s", speaker_identity)

                    content_type = (participant.attributes or {}).get("orbit_content_type", "normal")

                session = GeminiSession(
                    room=self._room,
                    speaker_identity=speaker_identity,
                    speaker_track=track,
                    track_source=source_str,
                    target_lang=target_lang,
                    gemini_api_key=self._gemini_api_key,
                    glossary=glossary,
                    content_type=content_type,
                )
                self._sessions[key] = session
                try:
                    await session.start()
                except Exception as exc:
                    logger.exception(
                        "failed to start session %s -> %s: %s",
                        speaker_identity,
                        target_lang,
                        exc,
                    )
                    self._sessions.pop(key, None)

    def _compute_desired_sessions(self) -> set[SessionKey]:
        # Map of target_lang → set of listener identities that want it
        target_langs = self._listener_target_langs()
        if not target_langs:
            return set()

        speakers = self._active_speakers()
        total_participants = len(self._room.remote_participants)
        single_user_mode = total_participants == 1

        desired: set[SessionKey] = set()
        for speaker_identity, track_sid, source_lang, is_screen_share in speakers:
            for tgt, listeners in target_langs.items():
                if not is_screen_share and not single_user_mode and tgt == source_lang:
                    continue
                # Screen share: only translate if at least one listener of this
                # target language wants screen share translation.
                if is_screen_share:
                    wants_ss = any(
                        self._wants_screen_share_translation(lid) for lid in listeners
                    )
                    if not wants_ss:
                        continue
                desired.add((speaker_identity, track_sid, tgt))
        return desired

    def _wants_screen_share_translation(self, identity: str) -> bool:
        """Check if a listener wants screen share audio translated."""
        p = self._room.remote_participants.get(identity)
        if p is None:
            return True  # default: translate
        attr = (p.attributes or {}).get("orbit_translate_screenshare")
        return attr != "false"

    def _listener_target_langs(self) -> dict[str, set[str]]:
        """Map of target_lang → set of listener identities wanting that language.
        Excludes the native sentinel."""
        langs: dict[str, set[str]] = {}
        for identity, p in self._room.remote_participants.items():
            lang = (p.attributes or {}).get(PARTICIPANT_LANG_ATTR)
            if lang and lang != NATIVE_LANG:
                langs.setdefault(lang, set()).add(identity)
        return langs

    def _active_speakers(self) -> list[tuple[str, str, str, bool]]:
        """List of (identity, track_sid, lang, is_screen_share) for speakers
        that have enabled audio tracks.

        Screen share audio is always included regardless of the speaker's
        declared language — shared content (e.g. a video in a browser tab)
        may be in any language, so skipping it would silence translation.
        """
        out: list[tuple[str, str, str, bool]] = []
        for p in self._room.remote_participants.values():
            lang = (p.attributes or {}).get(PARTICIPANT_LANG_ATTR) or ""
            tracks = self._speaker_tracks.get(p.identity, {})
            for track_sid, _track in tracks.items():
                if not self._is_track_unmuted(p, track_sid):
                    continue
                # Get source from the publication, not the track.
                # RemoteAudioTrack doesn't have a .source attribute.
                pub = p.track_publications.get(track_sid)
                is_ss = (
                    pub is not None
                    and pub.source == rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO
                )
                if is_ss:
                    out.append((p.identity, track_sid, lang or "und", True))
                elif lang and lang != NATIVE_LANG:
                    out.append((p.identity, track_sid, lang, False))
        return out

    def _is_track_unmuted(self, p: rtc.RemoteParticipant, track_sid: str) -> bool:
        pub = p.track_publications.get(track_sid)
        return bool(pub and pub.kind == rtc.TrackKind.KIND_AUDIO and not pub.muted)

    # --- Teardown ----------------------------------------------------------

    async def _grace_teardown(self, key: SessionKey) -> None:
        try:
            await asyncio.sleep(SESSION_GRACE_SEC)
        except asyncio.CancelledError:
            return

        # If, after the grace window, the session is still undesired, kill it.
        if key in self._sessions and key not in self._compute_desired_sessions():
            session = self._sessions.pop(key)
            await session.aclose()
        self._grace_tasks.pop(key, None)

    def _on_participant_left(self, identity: str) -> None:
        """Speaker fully left: immediate teardown of all their sessions."""
        self._speaker_tracks.pop(identity, None)
        for key in list(self._sessions.keys()):
            if key[0] == identity:
                session = self._sessions.pop(key)
                # Cancel any pending grace teardown so we don't double-close.
                pending = self._grace_tasks.pop(key, None)
                if pending:
                    pending.cancel()
                task = asyncio.create_task(session.aclose())
                self._detached_tasks.add(task)
                task.add_done_callback(self._detached_tasks.discard)

    async def _handle_retranslation(self, data: rtc.DataPacket) -> None:
        try:
            payload = json.loads(data.data.decode("utf-8"))
            key = payload.get("key")
            source_text = payload.get("sourceText")
            target_lang = payload.get("target_lang")
            adjustment = payload.get("adjustment")

            if not key or not source_text or not target_lang or not adjustment:
                logger.warning("Invalid retranslation request: %s", payload)
                return

            adjusted_text = await self._retranslate_text(source_text, target_lang, adjustment)

            if adjusted_text:
                response = {
                    "key": key,
                    "text": adjusted_text
                }
                await self._room.local_participant.publish_data(
                    payload=json.dumps(response).encode("utf-8"),
                    topic="retranslation_response",
                    reliable=True
                )
                logger.info("Retranslation successful for key=%s", key)
        except Exception as exc:
            logger.exception("Error handling retranslation: %s", exc)

    async def _retranslate_text(self, text: str, target_lang: str, adjustment: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={self._gemini_api_key}"

        prompt = (
            f"You are a professional real-time translator. "
            f"Translate the following text into the language with code '{target_lang}'.\n"
            f"Apply the following style/tone adjustment: {adjustment}.\n"
            f"Output ONLY the final translated text. Do not include any explanations, markers, or original text.\n\n"
            f"Original text: {text}"
        )

        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result["candidates"][0]["content"]["parts"][0]["text"].strip()
                    else:
                        body = await resp.text()
                        logger.error("Gemini API error during retranslation status=%d body=%s", resp.status, body)
                        return ""
        except Exception as e:
            logger.error("Failed to call Gemini API for retranslation: %s", e)
            return ""

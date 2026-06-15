"use client";

import { useEffect } from "react";
import { useRoomContext } from "@livekit/components-react";
import {
  ParticipantKind,
  RoomEvent,
  type RemoteParticipant,
  type RemoteTrackPublication,
  Track,
} from "livekit-client";
import { NATIVE_LANG, PARTICIPANT_LANG_ATTR } from "@/lib/config";

// Translator-track name format set by the Python agent in
// translator/src/session.py: f"tx:{speaker_identity}:{target_lang}"
const TRANSLATION_TRACK_PREFIX = "tx:";

function parseTranslationTrackName(
  name: string,
): { sourceIdentity: string; targetLang: string; trackSource: string } | null {
  if (!name.startsWith(TRANSLATION_TRACK_PREFIX)) return null;
  const parts = name.slice(TRANSLATION_TRACK_PREFIX.length).split(":");
  if (parts.length < 3) {
    // Fallback for old sessions that didn't have trackSource
    if (parts.length === 2) {
      return { sourceIdentity: parts[0], targetLang: parts[1], trackSource: "mic" };
    }
    return null;
  }
  const targetLang = parts.pop()!;
  const trackSource = parts.pop()!;
  const sourceIdentity = parts.join(":");
  if (!sourceIdentity || !targetLang || !trackSource) return null;
  return { sourceIdentity, targetLang, trackSource };
}

/**
 * Subscribes/unsubscribes to audio tracks based on the listener's chosen
 * language and preferences.
 *
 * Normal Zoom audio flow (always on):
 *   - All human mic + screen share tracks subscribed at full volume.
 *   - Translation is a parallel pipeline on top — listeners hear both
 *     the original AND the translated version simultaneously.
 *
 * Mute original toggle:
 *   - When ON: original audio silenced (user hears only translation).
 *   - When OFF: original audio plays at 100% alongside translation.
 *
 * Translation routing:
 *   - Agent translation tracks are subscribed when translationEnabled=true,
 *     the track's target_lang matches myLang, and either:
 *       - trackSource === "screen_share_audio" AND translateScreenShare=true
 *       - trackSource === "mic" AND speaker's lang !== myLang
 */
export function useTranslationRouting(
  myLang: string,
  myIdentity: string,
  translationEnabled: boolean = true,
  muteOriginal: boolean = true,
  translateScreenShare: boolean = true,
  translatorMuted: boolean = false,
  speakerMuted: boolean = false,
) {
  const room = useRoomContext();

  useEffect(() => {
    if (!room) return;

    const apply = () => {
      const remotes = Array.from(room.remoteParticipants.values());
      const peerLangs = new Map<string, string | undefined>();
      for (const p of remotes) {
        if (p.kind === ParticipantKind.AGENT) continue;
        peerLangs.set(p.identity, p.attributes?.[PARTICIPANT_LANG_ATTR]);
      }

      for (const p of remotes) {
        if (p.kind === ParticipantKind.AGENT) {
          applyAgentSubscriptions(p, myLang, myIdentity, peerLangs, translationEnabled, translateScreenShare, translatorMuted);
        } else {
          applyHumanSubscriptions(p, myLang, translationEnabled, muteOriginal, translateScreenShare, speakerMuted);
        }
      }
    };

    apply();

    const handlers: Array<[Parameters<typeof room.on>[0], () => void]> = [
      [RoomEvent.ParticipantConnected, apply],
      [RoomEvent.ParticipantDisconnected, apply],
      [RoomEvent.ParticipantAttributesChanged, apply],
      [RoomEvent.TrackPublished, apply],
      [RoomEvent.TrackUnpublished, apply],
      [RoomEvent.TrackSubscribed, apply],
      [RoomEvent.TrackUnsubscribed, apply],
      [RoomEvent.LocalTrackPublished, apply],
    ];
    for (const [event, handler] of handlers) {
      room.on(event, handler);
    }
    return () => {
      for (const [event, handler] of handlers) {
        room.off(event, handler);
      }
    };
  }, [room, myLang, myIdentity, translationEnabled, muteOriginal, translateScreenShare, translatorMuted, speakerMuted]);
}

function applyHumanSubscriptions(
  p: RemoteParticipant,
  myLang: string,
  translationEnabled: boolean,
  muteOriginal: boolean,
  translateScreenShare: boolean,
  speakerMuted: boolean,
) {

  for (const pub of p.audioTrackPublications.values()) {
    if (pub.source !== Track.Source.Microphone && pub.source !== Track.Source.ScreenShareAudio) continue;
    
    // Always subscribe to human tracks (normal Zoom flow).
    setSubscribed(pub, true);
    
    if (pub.track && pub.track instanceof Track) {
      const audioTrack = pub.track as Track & { setVolume?: (volume: number) => void };
      if (typeof audioTrack.setVolume === "function") {
        if (speakerMuted) {
          audioTrack.setVolume(0);
        } else {
          // "Mute original" ducks source to 15% — still audible behind translation.
          // OFF = original at 100% alongside translation.
          audioTrack.setVolume(muteOriginal ? 0.15 : 1.0);
        }
      }
    }
  }
}

function applyAgentSubscriptions(
  agent: RemoteParticipant,
  myLang: string,
  myIdentity: string,
  peerLangs: Map<string, string | undefined>,
  translationEnabled: boolean,
  translateScreenShare: boolean,
  translatorMuted: boolean,
) {
  for (const pub of agent.audioTrackPublications.values()) {
    const parsed = parseTranslationTrackName(pub.trackName);
    if (!parsed) {
      // Not a translation track (e.g., agent state audio). Don't touch.
      continue;
    }

    // When translation is off or user wants native only: never agent tracks.
    if (!translationEnabled || myLang === NATIVE_LANG) {
      setSubscribed(pub, false);
      continue;
    }

    const matchesMe = parsed.targetLang === myLang || 
                         parsed.targetLang === myLang.split("-")[0] || 
                         myLang === parsed.targetLang.split("-")[0];
    if (!matchesMe) {
      setSubscribed(pub, false);
      continue;
    }

    // Never subscribe to translations of our own voice (unless it's screen share audio)
    if (parsed.sourceIdentity === myIdentity && parsed.trackSource !== "screen_share_audio") {
      setSubscribed(pub, false);
      continue;
    }

    // If the remote user is already speaking our target language,
    // we don't need a translation. We'll hear their original voice.
    const sourcePeerLang = peerLangs.get(parsed.sourceIdentity);
    if (sourcePeerLang === myLang) {
      setSubscribed(pub, false);
      continue;
    }

    if (parsed.trackSource === "screen_share_audio") {
      setSubscribed(pub, translateScreenShare);
    } else {
      // Mic translation: subscribe to whatever the agent publishes.
      // The agent decides which sessions to create (single-user mode, etc.)
      setSubscribed(pub, true);
    }

    if (pub.track && pub.track instanceof Track) {
      const audioTrack = pub.track as Track & { setVolume?: (volume: number) => void };
      if (typeof audioTrack.setVolume === "function") {
        audioTrack.setVolume(translatorMuted ? 0 : 1.0);
      }
    }
  }
}

function setSubscribed(pub: RemoteTrackPublication, desired: boolean) {
  if (pub.isSubscribed !== desired) {
    pub.setSubscribed(desired);
  }
}

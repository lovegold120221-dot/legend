"use client";

import { useEffect, useMemo, useState } from "react";
import {
  useDataChannel,
  useLocalParticipant,
  useRemoteParticipants,
  useRoomContext,
  useTracks,
} from "@livekit/components-react";
import { ConnectionState, ParticipantKind, RoomEvent, Track } from "livekit-client";
import { useRouter } from "next/navigation";
import { PARTICIPANT_LANG_ATTR } from "@/lib/config";
import { getLanguageByCode } from "@/lib/languages";
import { useTranslationRouting } from "./useTranslationRouting";
import { useUser } from "@/context/UserContext";

import ControlBar from "./ControlBar";
import ParticipantsPanel from "./ParticipantsPanel";
import ChatSidebar from "./ChatSidebar";
import BreakoutSidebar from "./BreakoutSidebar";
import ScreenShareView from "./ScreenShareView";
import OrbitTranslationPanel from "./OrbitTranslationPanel";
import GalleryView from "./GalleryView";
import { SpeakerIcon, SpeakerOffIcon, ChevronDownIcon, LinkIcon, ShieldCheckIcon } from "./icons";

export default function InCall({
  initialLang,
  onLeave,
}: {
  initialLang: string;
  onLeave: () => void;
}) {
  const room = useRoomContext();
  const { localParticipant } = useLocalParticipant();
  const remotes = useRemoteParticipants();
  const { profile } = useUser();
  const [lang, setLang] = useState(initialLang);
  const [translatorMuted, setTranslatorMuted] = useState(false);
  const [activeSidebar, setActiveSidebar] = useState<"participants" | "captions" | "translation" | "chat" | "breakout" | null>("translation");
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [headerCopied, setHeaderCopied] = useState(false);
  const [handRaised, setHandRaised] = useState(false);
  const router = useRouter();
  const isHost = typeof window !== 'undefined' && window.sessionStorage.getItem("orbitHostRoom") === room.name;

  useDataChannel("moderate", (msg) => {
    try {
      const payload = JSON.parse(new TextDecoder().decode(msg.payload));
      if (payload.type === "REQUEST_VIDEO" && payload.targetIdentity === localParticipant.identity) {
        if (confirm("The host has requested you to turn on your camera. Turn it on now?")) {
          localParticipant.setCameraEnabled(true);
        }
      }
    } catch {}
  });

  const [reactions, setReactions] = useState<Map<string, { emoji: string; ts: number }>>(new Map());

  useDataChannel("react", (msg) => {
    try {
      const payload = JSON.parse(new TextDecoder().decode(msg.payload));
      if (payload.emoji && payload.fromId) {
        setReactions((prev) => {
          const next = new Map(prev);
          next.set(payload.fromId, { emoji: payload.emoji, ts: Date.now() });
          return next;
        });
        // Auto-clear after 4 seconds
        setTimeout(() => {
          setReactions((prev) => {
            const next = new Map(prev);
            next.delete(payload.fromId);
            return next;
          });
        }, 4000);
      }
    } catch {}
  });

  useDataChannel("breakout", (msg) => {
    try {
      const payload = JSON.parse(new TextDecoder().decode(msg.payload));
      if (payload.type === "BREAKOUT_JOIN" && payload.newRoom) {
        // Preserve identity for the new room
        const name = sessionStorage.getItem("lt.displayName") || localParticipant.name || "participant";
        const lang = sessionStorage.getItem("lt.lang") || initialLang;
        sessionStorage.setItem("lt.displayName", name);
        sessionStorage.setItem("lt.lang", lang);
        if (payload.token) {
          // Store pre-generated token for the breakout room
          sessionStorage.setItem("orbit.breakout-token", payload.token);
          sessionStorage.setItem("orbit.breakout-server-url", payload.serverUrl || "");
        }
        alert("You have been assigned to a breakout room. Moving now...");
        router.push(`/session/${payload.newRoom}/room?returnTo=${payload.originalRoom}`);
      } else if (payload.type === "BREAKOUT_END" && payload.originalRoom) {
        const name = sessionStorage.getItem("lt.displayName") || localParticipant.name || "participant";
        const lang = sessionStorage.getItem("lt.lang") || initialLang;
        sessionStorage.setItem("lt.displayName", name);
        sessionStorage.setItem("lt.lang", lang);
        alert("Breakout session ended. Returning to main room...");
        router.push(`/session/${payload.originalRoom}/room`);
      }
    } catch {
      // Ignore non-JSON or unrelated messages
    }
  });

  const toggleSidebar = (sidebar: "participants" | "captions" | "translation" | "chat" | "breakout") => {
    setActiveSidebar((current) => (current === sidebar ? null : sidebar));
  };

  // Push the local lang into participant attributes so the agent + peers see
  // it. setAttributes is silently dropped before the room is connected, so we
  // both fire on `lang` change and re-fire when the connection becomes ready.
  // Host status is also broadcast so all participants can identify the host.
  useEffect(() => {
    if (!localParticipant || !room) return;
    const apply = () => {
      if (room.state === ConnectionState.Connected) {
        // Serialize glossary as JSON string for the agent to read
        const glossaryStr = profile?.glossary?.length
          ? JSON.stringify(profile.glossary)
          : "";
        localParticipant.setAttributes({
          [PARTICIPANT_LANG_ATTR]: lang,
          orbit_hand: handRaised ? "raised" : "",
          orbit_host: isHost ? "true" : "",
          orbit_glossary: glossaryStr,
          orbit_content_type: profile?.content_type || "normal",
        });
      }
    };
    apply();
    room.on(RoomEvent.Connected, apply);
    return () => {
      room.off(RoomEvent.Connected, apply);
    };
  }, [room, localParticipant, lang, handRaised, isHost, profile?.glossary, profile?.content_type]);

  useTranslationRouting(lang, localParticipant.identity, true, true, true, translatorMuted, speakerMuted);




  const humanRemotes = useMemo(
    () => remotes.filter((p) => p.kind !== ParticipantKind.AGENT),
    [remotes],
  );
  const peerLangs = useMemo(() => {
    const map = new Map<string, string | undefined>();
    for (const p of humanRemotes) {
      map.set(p.identity, p.attributes?.[PARTICIPANT_LANG_ATTR]);
    }
    return map;
  }, [humanRemotes]);

  const langInfo = getLanguageByCode(lang);

  const screenShareTracks = useTracks([Track.Source.ScreenShare]);
  const hasScreenShare = screenShareTracks.length > 0;


  const shareUrl = typeof window !== "undefined"
    ? `${window.location.origin}/session/${room.name}`
    : "";

  async function copyShareLink() {
    if (!shareUrl) return;
    await navigator.clipboard.writeText(shareUrl);
    setHeaderCopied(true);
    setTimeout(() => setHeaderCopied(false), 2000);
  }

  return (
    <div className="room-shell">
      <div className="room">
        {/* Top chrome */}
        <header className="orbit-header">
          {/* Desktop Single Line Header */}
          <div className="orbit-topbar-desktop">
            <div className="orbit-topbar-left">
              <span className="orbit-titlebar-title">Orbit Meeting</span>
              <span className="orbit-translation-status orbit-translation-status-text">
                Translation: {langInfo?.name || lang}
              </span>
              <button
                className="orbit-view-btn"
                onClick={() => setTranslatorMuted(!translatorMuted)}
                title={translatorMuted ? "Unmute translator" : "Mute translator"}
                aria-label={translatorMuted ? "Unmute translator" : "Mute translator"}
              >
                {translatorMuted ? <SpeakerOffIcon /> : <SpeakerIcon />}
              </button>
            </div>
            
            <div className="orbit-topbar-right">
              <span className="orbit-room-id">{room.name}</span>
              <button
                className="orbit-copy-btn"
                onClick={copyShareLink}
                title={headerCopied ? "Copied!" : "Copy meeting link"}
                aria-label="Copy meeting link"
              >
                <LinkIcon />
                <span>{headerCopied ? "Copied" : "Copy"}</span>
              </button>
            </div>
          </div>

          {/* Mobile topbar content — hidden on desktop */}
          <div className="orbit-topbar-mobile">
            <button className="orbit-mobile-audio" aria-label="Audio">
              <SpeakerIcon />
            </button>
            <button className="orbit-mobile-brand">
              <ShieldCheckIcon style={{ color: "#22c55e", strokeWidth: 1.5, width: "18px", height: "18px" }} />
              <span>Orbit</span>
              <ChevronDownIcon />
            </button>
            <button className="orbit-mobile-leave" onClick={async () => { await room.disconnect(); onLeave(); }}>
              Leave
            </button>
          </div>
        </header>

        {/* Stage */}
        <main className="room-stage orbit-stage">
          <div className="orbit-stage-center">
            {hasScreenShare ? (
              <ScreenShareView
                myLang={lang}
              />
            ) : (
              <GalleryView remotes={humanRemotes} myLang={lang} isHost={isHost} roomName={room.name} />
            )}
          </div>
          {/* Right Sidebar Panel */}
          {activeSidebar === "participants" && (
            <ParticipantsPanel 
              localParticipant={localParticipant}
              participants={humanRemotes} 
              myLang={lang} 
              isHost={isHost}
              roomName={room.name}
              onClose={() => setActiveSidebar(null)}
              onToggleChat={() => toggleSidebar("chat")}
              reactions={reactions}
            />
          )}
          {activeSidebar === "translation" && (
            <OrbitTranslationPanel
              onClose={() => setActiveSidebar(null)}
              myLang={lang}
              onLangChange={setLang}
              translatorMuted={translatorMuted}
              onToggleTranslator={() => setTranslatorMuted((v) => !v)}
              peerLangs={peerLangs}
              roomName={room.name}
            />
          )}
          {activeSidebar === "chat" && (
            <ChatSidebar onClose={() => setActiveSidebar(null)} />
          )}
          {activeSidebar === "breakout" && (
            <BreakoutSidebar onClose={() => setActiveSidebar(null)} />
          )}
        </main>

        {/* Control bar */}
        <ControlBar
          onLeave={onLeave}
          activeSidebar={activeSidebar}
          onToggleSidebar={toggleSidebar}
          speakerMuted={speakerMuted}
          onToggleSpeaker={() => setSpeakerMuted((v) => !v)}
          handRaised={handRaised}
          onToggleHand={() => {
            const cur = localParticipant?.attributes?.orbit_hand === "raised";
            setHandRaised(!cur);
            localParticipant?.setAttributes({ orbit_hand: cur ? "" : "raised" });
          }}
        />
      </div>

    </div>
  );
}

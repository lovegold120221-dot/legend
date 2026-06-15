"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@/context/UserContext";
import { useAuth } from "@/context/AuthContext";
import Link from "next/link";

type ActivePanel = "join" | "schedule";

export default function Home() {
  const router = useRouter();
  const { user, loading: authLoading, signOut } = useAuth();
  const [creating, setCreating] = useState(false);
  const [authCheckDone, setAuthCheckDone] = useState(false);
  // All hooks must be called before any conditional returns (Rules of Hooks).
  const [activePanel, setActivePanel] = useState<ActivePanel>("join");
  const [joinValue, setJoinValue] = useState("");
  const [joinError, setJoinError] = useState("");
  const [scheduleTitle, setScheduleTitle] = useState("Orbit Meeting");
  const [scheduleTime, setScheduleTime] = useState("");
  const [scheduledLink, setScheduledLink] = useState("");
  const [copied, setCopied] = useState(false);
  const { profile, updateProfile } = useUser();
  const theme = profile?.theme || "dark";

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // Redirect unauthenticated users to the login page.
  // Skip redirect if Supabase isn't configured (anonymous usage).
  useEffect(() => {
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
    if (!supabaseUrl) {
      // Supabase not configured — skip auth and show landing directly.
      setAuthCheckDone(true);
      return;
    }
    if (authLoading) return;
    setAuthCheckDone(true);
    if (!user) {
      router.replace("/auth/login");
    }
  }, [user, authLoading, router]);

  // Show nothing while auth state is loading or redirecting.
  if (!authCheckDone) {
    return (
      <main className="auth-shell">
        <div className="auth-card">
          <h1 className="auth-title">Orbit Meeting</h1>
        </div>
      </main>
    );
  }
  if (!user && process.env.NEXT_PUBLIC_SUPABASE_URL) return null; // Redirecting

  function createSession() {
    setCreating(true);
    const sessionId = crypto.randomUUID();
    window.sessionStorage.setItem("orbitHostRoom", sessionId);
    router.push(`/session/${sessionId}`);
  }

  function parseMeetingId(value: string) {
    const trimmed = value.trim();
    if (!trimmed) return "";

    try {
      const url = new URL(trimmed);
      const parts = url.pathname.split("/").filter(Boolean);
      const sessionIndex = parts.indexOf("session");
      if (sessionIndex !== -1 && parts[sessionIndex + 1]) {
        return parts[sessionIndex + 1];
      }
    } catch {
      // Plain room names are handled below.
    }

    return trimmed
      .replace(/^\/+|\/+$/g, "")
      .replace(/^session\//, "")
      .replace(/\/room$/, "");
  }

  function joinMeeting() {
    const meetingId = parseMeetingId(joinValue);
    if (!meetingId) {
      setJoinError("Enter a meeting link or meeting ID.");
      return;
    }
    setJoinError("");
    router.push(`/session/${encodeURIComponent(meetingId)}`);
  }

  function showSchedulePanel() {
    setActivePanel("schedule");
    setCopied(false);
    if (!scheduleTime) {
      setScheduleTime(getDefaultScheduleTime());
    }
    if (!scheduledLink) {
      setScheduledLink(`${window.location.origin}/session/${crypto.randomUUID()}`);
    }
  }

  async function copyScheduleLink() {
    if (!scheduledLink) return;
    await navigator.clipboard.writeText(scheduledLink);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function toggleTheme() {
    updateProfile({ theme: theme === "dark" ? "light" : "dark" });
  }

  return (
    <main className="entry-shell" data-theme={theme}>

      <section className="entry-main">
        <header className="entry-topbar">
          <div className="entry-topbar-left">
            <div className="entry-brand">
              <img src="/icon-eburon.svg" alt="Eburon AI" className="entry-brand-logo" />
              <span>Orbit Meeting</span>
            </div>

          </div>
          <div className="entry-topbar-actions">
            <button
              className="entry-theme-toggle"
              type="button"
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            >
              <ThemeIcon />
              <span>{theme === "dark" ? "Light" : "Dark"}</span>
            </button>
            {user ? (
              <div className="entry-auth-section">
                <span className="entry-auth-email" title={user.email ?? ""}>{user.email}</span>
                <button className="entry-auth-btn" onClick={() => signOut()}>Sign out</button>
              </div>
            ) : (
              <div className="entry-auth-section">
                <Link href="/auth/login" className="entry-auth-btn">Sign in</Link>
                <Link href="/auth/signup" className="entry-auth-btn">Create account</Link>
              </div>
            )}
          </div>
        </header>

        <div className="entry-content">
          <section className="entry-actions" aria-label="Meeting actions">
            <button
              className="meeting-action meeting-action--create"
              onClick={createSession}
              disabled={creating}
              id="create-session-btn"
            >
              <span className="meeting-action-icon" aria-hidden>
                <VideoPlusIcon />
              </span>
              <span>{creating ? "Creating..." : "Create"}</span>
            </button>

            <button
              className="meeting-action meeting-action--join"
              onClick={() => setActivePanel("join")}
            >
              <span className="meeting-action-icon" aria-hidden>
                <JoinIcon />
              </span>
              <span>Join</span>
            </button>

            <button
              className="meeting-action meeting-action--schedule"
              onClick={showSchedulePanel}
            >
              <span className="meeting-action-icon" aria-hidden>
                <CalendarIcon />
              </span>
              <span>Schedule meeting</span>
            </button>
          </section>

          <section className="entry-panel" aria-live="polite">
            {activePanel === "join" ? (
              <form
                className="entry-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  joinMeeting();
                }}
              >
                <div>
                  <p className="entry-panel-eyebrow">Join meeting</p>
                  <h2>Enter a meeting link or ID</h2>
                </div>
                <label className="entry-field">
                  <span>Meeting link or ID</span>
                  <input
                    value={joinValue}
                    onChange={(event) => {
                      setJoinValue(event.target.value);
                      setJoinError("");
                    }}
                    placeholder="https://.../session/room-id"
                    autoComplete="off"
                  />
                </label>
                {joinError && <p className="entry-error">{joinError}</p>}
                <button className="entry-primary" type="submit">
                  Join meeting
                </button>
              </form>
            ) : (
              <div className="entry-form">
                <div>
                  <p className="entry-panel-eyebrow">Schedule meeting</p>
                  <h2>Create an invite link</h2>
                </div>
                <label className="entry-field">
                  <span>Topic</span>
                  <input
                    value={scheduleTitle}
                    onChange={(event) => setScheduleTitle(event.target.value)}
                    maxLength={60}
                  />
                </label>
                <label className="entry-field">
                  <span>Date and time</span>
                  <input
                    type="datetime-local"
                    value={scheduleTime}
                    onChange={(event) => setScheduleTime(event.target.value)}
                  />
                </label>
                <div className="schedule-link">
                  <span>{scheduledLink}</span>
                </div>
                <button
                  className="entry-primary"
                  type="button"
                  onClick={copyScheduleLink}
                >
                  {copied ? "Copied" : "Copy invite"}
                </button>
              </div>
            )}
          </section>
        </div>

        <section className="entry-upcoming" aria-label="Upcoming meeting">
          <div>
            <p className="entry-panel-eyebrow">Next up</p>
            <h2>{activePanel === "schedule" ? scheduleTitle : "Ready when you are"}</h2>
          </div>
          <p>
            {activePanel === "schedule" && scheduleTime
              ? formatScheduleTime(scheduleTime)
              : "Create a room now or join with an invite link."}
          </p>
        </section>
      </section>
    </main>
  );
}

function getDefaultScheduleTime() {
  const date = new Date();
  date.setMinutes(date.getMinutes() + 30);
  date.setSeconds(0, 0);
  const offset = date.getTimezoneOffset();
  const localDate = new Date(date.getTime() - offset * 60 * 1000);
  return localDate.toISOString().slice(0, 16);
}

function formatScheduleTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time not set";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function VideoPlusIcon() {
  return (
    <svg
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h7A2.5 2.5 0 0 1 16 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-7A2.5 2.5 0 0 1 4 16.5z" />
      <path d="m16 10 4-2.5v9L16 14" />
      <path d="M10 9v6" />
      <path d="M7 12h6" />
    </svg>
  );
}

function JoinIcon() {
  return (
    <svg
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 12h11" />
      <path d="m11 8 4 4-4 4" />
      <path d="M15 5h2.5A2.5 2.5 0 0 1 20 7.5v9a2.5 2.5 0 0 1-2.5 2.5H15" />
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M7 3v4" />
      <path d="M17 3v4" />
      <path d="M4.5 8.5h15" />
      <path
        d="M6.5 5h11A2.5 2.5 0 0 1 20 7.5v10A2.5 2.5 0 0 1 17.5 20h-11A2.5 2.5 0 0 1 4 17.5v-10A2.5 2.5 0 0 1 6.5 5z"
      />
      <path d="M9 13h6" />
      <path d="M9 16h3" />
    </svg>
  );
}

function ThemeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2" />
      <path d="M12 20v2" />
      <path d="m4.93 4.93 1.41 1.41" />
      <path d="m17.66 17.66 1.41 1.41" />
      <path d="M2 12h2" />
      <path d="M20 12h2" />
      <path d="m6.34 17.66-1.41 1.41" />
      <path d="m19.07 4.93-1.41 1.41" />
    </svg>
  );
}



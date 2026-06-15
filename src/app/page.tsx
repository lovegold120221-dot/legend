"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@/context/UserContext";
import { useAuth } from "@/context/AuthContext";
import Link from "next/link";
import type { ScheduledMeeting } from "@/lib/reminder";
import {
  getScheduledMeetings,
  saveScheduledMeeting,
  getNextMeeting,
  getUpcomingMeetings,
  getMinutesUntilMeeting,
  formatMeetingTime,
  formatCountdown,
  scheduleMeetingReminder,
  rescheduleAllReminders,
  requestNotificationPermission,
  showBrowserNotification,
  downloadIcsFile,
  clearPastMeetings,
} from "@/lib/reminder";

type ActivePanel = "join" | "schedule";

export default function Home() {
  const router = useRouter();
  const { user, loading: authLoading, signOut } = useAuth();
  const [creating, setCreating] = useState(false);
  // All hooks must be called before any conditional returns (Rules of Hooks).
  const [activePanel, setActivePanel] = useState<ActivePanel>("join");
  const [joinValue, setJoinValue] = useState("");
  const [joinError, setJoinError] = useState("");
  const [scheduleTitle, setScheduleTitle] = useState("Orbit Meeting");
  const [scheduleDate, setScheduleDate] = useState(new Date().toISOString().split("T")[0]);
  const [scheduleHour, setScheduleHour] = useState("12");
  const [scheduleMinute, setScheduleMinute] = useState("00");
  const [scheduledLink, setScheduledLink] = useState("");
  const [copied, setCopied] = useState(false);
  const [reminderToast, setReminderToast] = useState(false);
  const [scheduledMeetings, setScheduledMeetings] = useState<ScheduledMeeting[]>([]);
  const [nextMeeting, setNextMeeting] = useState<ScheduledMeeting | null>(null);
  const [minutesUntil, setMinutesUntil] = useState(0);
  const { profile } = useUser();
  const theme = profile?.theme || "system";
  const supabaseConfigured = Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL);
  const waitingForAuth = supabaseConfigured && authLoading;
  const redirectingToAuth = supabaseConfigured && !authLoading && !user;

  // Redirect unauthenticated users to the login page.
  // Skip redirect if Supabase isn't configured (anonymous usage).
  useEffect(() => {
    if (redirectingToAuth) {
      router.replace("/auth/login");
    }
  }, [redirectingToAuth, router]);

  // Load scheduled meetings and reschedule reminders on mount
  useEffect(() => {
    refreshMeetings();
    rescheduleAllReminders();

    // Refresh countdown every 30s
    const interval = setInterval(() => {
      refreshMeetings();
    }, 30_000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Show nothing while auth state is loading or redirecting.
  if (waitingForAuth) {
    return (
      <main className="auth-shell">
        <div className="auth-card">
          <h1 className="auth-title">Orbit Meeting</h1>
        </div>
      </main>
    );
  }
  if (redirectingToAuth) return null;

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

  async function showSchedulePanel() {
    setActivePanel("schedule");
    setCopied(false);
    
    // Default to 30 mins from now
    const now = new Date();
    const future = new Date(now.getTime() + 30 * 60_000);
    
    setScheduleDate(future.toISOString().split("T")[0]);
    setScheduleHour(future.getHours().toString().padStart(2, "0"));
    setScheduleMinute(future.getMinutes().toString().padStart(2, "0"));
    
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

  // Derived value for existing logic that expects a datetime-local string
  const scheduleTime = `${scheduleDate}T${scheduleHour}:${scheduleMinute}`;

  async function saveAndRemind() {
    if (!scheduledLink || !scheduleDate || !scheduleHour || !scheduleMinute) return;

    const meeting: ScheduledMeeting = {
      id: scheduledLink.split("/").pop() || crypto.randomUUID(),
      title: scheduleTitle,
      scheduledAt: new Date(scheduleTime).toISOString(),
      link: scheduledLink,
      createdAt: new Date().toISOString(),
      reminded: [],
    };

    saveScheduledMeeting(meeting);
    refreshMeetings();

    // Request notification permission
    const granted = await requestNotificationPermission();
    if (granted) {
      showBrowserNotification(
        "Reminder Set",
        `You'll be notified before "${meeting.title}"`,
        meeting.link,
      );
    }

    setReminderToast(true);
    setTimeout(() => setReminderToast(false), 4000);
  }

  function refreshMeetings() {
    clearPastMeetings();
    setScheduledMeetings(getUpcomingMeetings(48));
    const next = getNextMeeting();
    setNextMeeting(next);
    if (next) {
      setMinutesUntil(getMinutesUntilMeeting(next));
    }
  }

  function getEmailLink() {
    const timeStr = scheduleTime ? formatScheduleTime(scheduleTime) : "Not set";
    const subject = `Formal Invitation: ${scheduleTitle}`;
    const body = `Dear Participant,\n\nYou are formally invited to attend the following virtual meeting via Orbit Meeting.\n\nMeeting Details:\n• Topic: ${scheduleTitle}\n• Date & Time: ${timeStr}\n• Meeting Link: ${scheduledLink}\n\nPlease join the session using the link provided above at the scheduled time.\n\nSincerely,\nOrbit Meeting Team`;
    return `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  }

  function getGmailLink() {
    const timeStr = scheduleTime ? formatScheduleTime(scheduleTime) : "Not set";
    const subject = `Formal Invitation: ${scheduleTitle}`;
    const body = `Dear Participant,\n\nYou are formally invited to attend the following virtual meeting via Orbit Meeting.\n\nMeeting Details:\n• Topic: ${scheduleTitle}\n• Date & Time: ${timeStr}\n• Meeting Link: ${scheduledLink}\n\nPlease join the session using the link provided above at the scheduled time.\n\nSincerely,\nOrbit Meeting Team`;
    return `https://mail.google.com/mail/?view=cm&fs=1&su=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  }

  function getWhatsAppLink() {
    const timeStr = scheduleTime ? formatScheduleTime(scheduleTime) : "Not set";
    const text = `Dear Participant,\n\nYou are formally invited to attend the following virtual meeting via Orbit Meeting.\n\nMeeting Details:\n• Topic: ${scheduleTitle}\n• Date & Time: ${timeStr}\n• Meeting Link: ${scheduledLink}\n\nPlease join the session using the link provided above at the scheduled time.\n\nSincerely,\nOrbit Meeting Team`;
    return `https://api.whatsapp.com/send?text=${encodeURIComponent(text)}`;
  }

  return (
    <main className="entry-shell" data-theme-preference={theme}>

      <section className="entry-main">
        <header className="entry-topbar">
          <div className="entry-topbar-inner">
            <div className="entry-topbar-left">
              <div className="entry-brand">
                <Image src="/icon-eburon.svg" alt="Eburon AI" width={34} height={34} className="entry-brand-logo" unoptimized />
                <span>Orbit Meeting</span>
              </div>
            </div>
            <div className="entry-topbar-actions">
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
                   <div className="schedule-time-picker">
                     <input
                       type="date"
                       value={scheduleDate}
                       onChange={(event) => setScheduleDate(event.target.value)}
                     />
                     <div className="time-selects">
                       <select
                         value={scheduleHour}
                         onChange={(event) => setScheduleHour(event.target.value)}
                       >
                         {Array.from({ length: 24 }, (_, i) => (
                           <option key={i} value={i.toString().padStart(2, "0")}>
                             {i.toString().padStart(2, "0")}
                           </option>
                         ))}
                       </select>
                       <span className="time-separator">:</span>
                       <select
                         value={scheduleMinute}
                         onChange={(event) => setScheduleMinute(event.target.value)}
                       >
                         {Array.from({ length: 60 }, (_, i) => (
                           <option key={i} value={i.toString().padStart(2, "0")}>
                             {i.toString().padStart(2, "0")}
                           </option>
                         ))}
                       </select>
                     </div>
                   </div>
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

                <div className="invite-share-bar">
                  <button
                    type="button"
                    className="share-icon-btn share-icon-email"
                    onClick={() => {
                      saveAndRemind();
                      window.location.href = getEmailLink();
                    }}
                    title="Share via Email"
                    aria-label="Share via Email"
                  >
                    <span className="share-icon-mark share-icon-mark--email" aria-hidden>
                      <MailIcon />
                    </span>
                    <span>Email</span>
                  </button>
                  <button
                    type="button"
                    className="share-icon-btn share-icon-gmail"
                    onClick={() => {
                      saveAndRemind();
                      window.open(getGmailLink(), "_blank", "noopener,noreferrer");
                    }}
                    title="Share via Gmail"
                    aria-label="Share via Gmail"
                  >
                    <span className="share-icon-mark share-icon-mark--gmail" aria-hidden>
                      <GmailIcon />
                    </span>
                    <span>Gmail</span>
                  </button>
                  <button
                    type="button"
                    className="share-icon-btn share-icon-whatsapp"
                    onClick={() => {
                      saveAndRemind();
                      window.open(getWhatsAppLink(), "_blank", "noopener,noreferrer");
                    }}
                    title="Share via WhatsApp"
                    aria-label="Share via WhatsApp"
                  >
                    <span className="share-icon-mark share-icon-mark--whatsapp" aria-hidden>
                      <WhatsAppIcon />
                    </span>
                    <span>WhatsApp</span>
                  </button>
                </div>

                {reminderToast && (
                  <div className="reminder-toast">
                    <span className="reminder-toast-icon">🔔</span>
                    <div>
                      <strong>Reminder set</strong>
                      <span>You'll get a notification 15 minutes before.</span>
                    </div>
                    <button
                      type="button"
                      className="ics-download-btn"
                      onClick={() => {
                        const meeting: ScheduledMeeting = {
                          id: scheduledLink?.split("/").pop() || crypto.randomUUID(),
                          title: scheduleTitle,
                          scheduledAt: new Date(scheduleTime).toISOString(),
                          link: scheduledLink || "",
                          createdAt: new Date().toISOString(),
                          reminded: [],
                        };
                        downloadIcsFile(meeting);
                      }}
                      title="Add to Calendar"
                      aria-label="Download calendar event"
                    >
                      <CalendarPlusIcon />
                    </button>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>

        <section className="entry-upcoming" aria-label="Upcoming meeting">
          <div>
            <p className="entry-panel-eyebrow">
              {nextMeeting ? "Next up" : "Next up"}
            </p>
            {nextMeeting ? (
              <>
                <h2>{nextMeeting.title}</h2>
                <p className="entry-upcoming-time">
                  {formatMeetingTime(nextMeeting.scheduledAt)}
                  {" · "}
                  <strong>{formatCountdown(minutesUntil)}</strong>
                </p>
                <div className="entry-upcoming-actions">
                  <Link
                    href={nextMeeting.link}
                    className="entry-upcoming-join"
                  >
                    Join
                  </Link>
                  <button
                    type="button"
                    className="entry-upcoming-ics"
                    onClick={() => downloadIcsFile(nextMeeting)}
                    title="Add to Calendar"
                    aria-label="Download calendar event"
                  >
                    <CalendarPlusIcon />
                  </button>
                </div>
              </>
            ) : activePanel === "schedule" && scheduleTime ? (
              <>
                <h2>{scheduleTitle}</h2>
                <p className="entry-upcoming-time">
                  {formatScheduleTime(scheduleTime)}
                </p>
              </>
            ) : (
              <>
                <h2>Ready when you are</h2>
                <p className="entry-upcoming-time">
                  Create a room now or join with an invite link.
                </p>
              </>
            )}
          </div>

          {scheduledMeetings.length > 1 && (
            <div className="entry-upcoming-list">
              <p className="entry-upcoming-list-label">All scheduled</p>
              {scheduledMeetings.map((m) => (
                <div key={m.id} className="entry-upcoming-list-item">
                  <span className="entry-upcoming-list-title">{m.title}</span>
                  <span className="entry-upcoming-list-time">
                    {formatMeetingTime(m.scheduledAt)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </section>
    </main>
  );
}

// No longer needed as we handle defaults in showSchedulePanel
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

function MailIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="currentColor"
    >
      <path d="M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z" />
    </svg>
  );
}

function WhatsAppIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="currentColor"
    >
      <path d="M12.012 2c-5.506 0-9.988 4.482-9.988 9.988 0 1.76.456 3.474 1.32 4.98L2 22l5.166-1.356a9.92 9.92 0 0 0 4.846 1.258h.004c5.504 0 9.986-4.482 9.986-9.988C22 6.482 17.518 2 12.012 2zm5.782 14.168c-.246.696-1.428 1.374-1.968 1.464-.492.084-1.134.12-1.8.12-2.796 0-5.832-1.638-7.728-4.296-1.122-1.572-1.92-3.468-1.92-5.466 0-1.848.882-2.82 1.698-3.084.246-.084.498-.12.75-.12.246 0 .498.012.678.024.192.012.456-.072.714.54.258.624.882 2.148.96 2.304.078.156.132.336.024.54-.108.204-.204.348-.36.528-.156.18-.324.396-.462.528-.156.156-.324.324-.138.636.18.3.804 1.326 1.722 2.142.924.822 1.704 1.38 2.022 1.542.318.156.498.132.684-.084.186-.216.792-.924.996-1.236.21-.312.414-.258.696-.156.282.102 1.782.84 2.088.996.3.156.504.228.576.36.072.132.072.756-.174 1.452z" />
    </svg>
  );
}

function GmailIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="currentColor"
    >
      <path d="M24 5.457v13.909c0 .904-.732 1.636-1.636 1.636h-3.819V11.73L12 16.64l-6.545-4.91v9.273H1.636A1.636 1.636 0 0 1 0 19.366V5.457c0-2.023 2.309-3.178 3.927-1.964L5.455 4.64 12 9.548l6.545-4.91 1.528-1.145C21.69 2.28 24 3.434 24 5.457z"/>
    </svg>
  );
}

function CalendarPlusIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="M7 10v-2" />
      <path d="M17 10v-2" />
      <path d="M3 6h18" />
      <path d="M12 12v6" />
      <path d="M9 15h6" />
    </svg>
  );
}

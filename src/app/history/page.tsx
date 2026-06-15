"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@/context/UserContext";
import { getLanguageByCode } from "@/lib/languages";
import {
  loadHistory,
  downloadHistoryFromSupabase,
  formatTimestamp,
  formatRoomName,
  type TranslationHistoryEntry,
} from "@/lib/translationHistory";

export default function HistoryPage() {
  const router = useRouter();
  const { profile, loading: profileLoading } = useUser();
  const theme = profile?.theme || "dark";
  const [entries, setEntries] = useState<TranslationHistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");

  useEffect(() => {
    async function load() {
      // Always load from localStorage first (instant)
      const local = loadHistory();
      const merged = [...local];

      // If user is logged in, try to merge Supabase data
      if (profile?.id) {
        try {
          const remote = await downloadHistoryFromSupabase(profile.id);
          if (remote.length > 0) {
            // Merge remote entries with local, deduplicating by id
            const seen = new Set(merged.map((e) => e.id));
            for (const entry of remote) {
              if (!seen.has(entry.id)) {
                merged.push(entry);
                seen.add(entry.id);
              }
            }
          }
        } catch {
          // Table may not exist — silently continue
        }
      }

      // Sort newest first
      merged.sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      );

      setEntries(merged);
      setLoading(false);
    }

    if (!profileLoading) {
      load();
    }
  }, [profile, profileLoading]);

  // Group entries by room name
  const grouped = entries.reduce<
    Record<string, { roomName: string; entries: TranslationHistoryEntry[] }>
  >((acc, entry) => {
    const key = entry.room_name || "_unknown";
    if (!acc[key]) {
      acc[key] = { roomName: key, entries: [] };
    }
    acc[key].entries.push(entry);
    return acc;
  }, {});

  // Filter by search query
  const filteredGroups = Object.entries(grouped)
    .map(([key, group]) => ({
      ...group,
      entries: searchQuery
        ? group.entries.filter(
            (e) =>
              e.source_text.toLowerCase().includes(searchQuery.toLowerCase()) ||
              e.translated_text
                .toLowerCase()
                .includes(searchQuery.toLowerCase()) ||
              e.speaker_name
                .toLowerCase()
                .includes(searchQuery.toLowerCase()),
          )
        : group.entries,
    }))
    .filter((g) => g.entries.length > 0)
    .sort((a, b) => {
      const aTime = a.entries[0]?.created_at ?? "";
      const bTime = b.entries[0]?.created_at ?? "";
      return new Date(bTime).getTime() - new Date(aTime).getTime();
    });

  return (
    <main className="history-shell" data-theme={theme}>
      {/* Header (Matching Settings Style) */}
      <header className="settings-topbar">
        <div className="settings-topbar-left">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/icon-eburon.svg" alt="Orbit AI" className="settings-brand-logo" />
          <span className="settings-brand">Orbit Meeting</span>
        </div>
        <div className="settings-topbar-center">
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
            <path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <h1>Translation History</h1>
        </div>
        <div className="settings-topbar-right">
          <div className="history-search-wrapper" style={{ marginRight: '16px' }}>
            <svg
              className="history-search-icon"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="11" cy="11" r="8" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input
              type="text"
              className="history-search-input"
              placeholder="Search translations…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>
          <button
            type="button"
            className="settings-close-btn"
            onClick={() => { if (window.history.length > 1) router.back(); else router.push('/'); }}
            aria-label="Close history"
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M5 5l10 10M15 5l-10 10" />
            </svg>
          </button>
        </div>
      </header>

      {/* Content */}
      <main className="history-content">
        {loading ? (
          <div className="history-empty">
            <div className="history-spinner" />
            <p>Loading history…</p>
          </div>
        ) : filteredGroups.length === 0 ? (
          <div className="history-empty">
            <svg
              width="48"
              height="48"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="history-empty-icon"
            >
              <circle cx="12" cy="12" r="10" />
              <polyline points="12 6 12 12 16 14" />
            </svg>
            <h2>No translation history yet</h2>
            <p>
              Join a meeting and start translating — your translations will
              appear here automatically.
            </p>
            <button
              className="btn btn-dark"
              onClick={() => router.push("/")}
            >
              Go to Meetings
            </button>
          </div>
        ) : (
          filteredGroups.map((group) => (
            <section key={group.roomName} className="history-session">
              <h2 className="history-session-title">
                {formatRoomName(group.roomName)}
              </h2>
              <div className="history-entry-list">
                {group.entries.map((entry) => (
                  <div key={entry.id} className="history-entry">
                    <div className="history-entry-meta">
                      <span className="history-entry-speaker">
                        {entry.speaker_name}
                      </span>
                      <span className="history-entry-langs">
                        {getLanguageByCode(entry.source_lang)?.flag}{" "}
                        {getLanguageByCode(entry.source_lang)?.name ??
                          entry.source_lang}{" "}
                        →{" "}
                        {getLanguageByCode(entry.target_lang)?.flag}{" "}
                        {getLanguageByCode(entry.target_lang)?.name ??
                          entry.target_lang}
                      </span>
                      <span className="history-entry-time">
                        {formatTimestamp(entry.created_at)}
                      </span>
                    </div>
                    <div className="history-entry-texts">
                      <p className="history-entry-source">
                        <strong>
                          {entry.speaker_name}:
                        </strong>{" "}
                        {entry.source_text}
                      </p>
                      <p className="history-entry-translated">
                        <strong>Orbit Translator:</strong>{" "}
                        {entry.translated_text}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))
        )}
      </main>
    </main>
  );
}

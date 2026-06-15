"use client";

import { useUser, GlossaryEntry } from "@/context/UserContext";
import { useRouter } from "next/navigation";
import { useState, useEffect } from "react";
import { PICKER_LANGUAGES } from "@/lib/languages";
import { SettingsIcon } from "@/app/session/[id]/room/icons";
import CameraPreview from "./CameraPreview";
import TranslationPlayground from "./TranslationPlayground";

type SettingsTab = "general" | "audio" | "video" | "translation" | "glossary" | "recording";

const VOICES = [
  { id: "male1", label: "Male 1" },
  { id: "male2", label: "Male 2" },
  { id: "female1", label: "Female 1" },
  { id: "female2", label: "Female 2" },
];

const TABS: { id: SettingsTab; label: string }[] = [
  { id: "general", label: "General" },
  { id: "audio", label: "Audio" },
  { id: "video", label: "Video" },
  { id: "translation", label: "Translation" },
  { id: "glossary", label: "Glossary" },
  { id: "recording", label: "Recording" },
];

export default function SettingsPage() {
  const router = useRouter();
  const { profile, loading, updateProfile } = useUser();
  const [activeTab, setActiveTab] = useState<SettingsTab>("general");
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Local form state
  const [name, setName] = useState("");
  const [theme, setTheme] = useState<"light" | "dark">("dark");
  const [defaultLanguage, setDefaultLanguage] = useState("en");
  const [contentType, setContentType] = useState<"normal" | "movie">("normal");
  const [voice, setVoice] = useState("male1");
  const [autoJoinAudio, setAutoJoinAudio] = useState(false);
  const [noiseSuppression, setNoiseSuppression] = useState(true);
  const [mirrorVideo, setMirrorVideo] = useState(true);
  const [cameraOffOnJoin, setCameraOffOnJoin] = useState(false);
  const [videoBackground, setVideoBackground] = useState("none");
  const [showCaptions, setShowCaptions] = useState(true);
  const [muteOriginalAudio, setMuteOriginalAudio] = useState(true);
  const [translateAudioPlayback, setTranslateAudioPlayback] = useState(true);
  const [glossary, setGlossary] = useState<GlossaryEntry[]>([]);
  const [recordingSavePath, setRecordingSavePath] = useState("");
  const [recordingAutoStart, setRecordingAutoStart] = useState(false);

  useEffect(() => {
    if (profile) {
      const t = setTimeout(() => {
        setName(profile.name || "");
        setTheme(profile.theme || "dark");
        setDefaultLanguage(profile.default_language || "en");
        setContentType(profile.content_type || "normal");
        setVoice(profile.voice || "Orus");
        setAutoJoinAudio(profile.auto_join_audio ?? false);
        setNoiseSuppression(profile.noise_suppression ?? true);
        setMirrorVideo(profile.mirror_video ?? true);
        setCameraOffOnJoin(profile.camera_off_on_join ?? false);
        setVideoBackground(profile.video_background ?? "none");
        setShowCaptions(profile.show_captions ?? true);
        setMuteOriginalAudio(profile.mute_original_audio ?? true);
        setTranslateAudioPlayback(profile.translate_audio_playback ?? true);
        setGlossary(profile.glossary ?? []);
        setRecordingSavePath(profile.recording_save_path ?? "");
        setRecordingAutoStart(profile.recording_auto_start ?? false);
      }, 0);
      return () => clearTimeout(t);
    }
  }, [profile]);

  function markDirty() {
    if (!dirty) setDirty(true);
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    await updateProfile({
      name,
      theme,
      default_language: defaultLanguage,
      content_type: contentType,
      voice,
      auto_join_audio: autoJoinAudio,
      noise_suppression: noiseSuppression,
      mirror_video: mirrorVideo,
      camera_off_on_join: cameraOffOnJoin,
      video_background: videoBackground,
      show_captions: showCaptions,
      mute_original_audio: muteOriginalAudio,
      translate_audio_playback: translateAudioPlayback,
      glossary,
      recording_save_path: recordingSavePath,
      recording_auto_start: recordingAutoStart,
    });
    setSaving(false);
    setDirty(false);
  }

  if (loading) {
    return (
      <main className="settings-shell">
        <div className="settings-loading">Loading your settings...</div>
      </main>
    );
  }

  return (
    <main className="settings-shell" data-theme={theme}>
      {/* Header */}
      <header className="settings-topbar">
        <div className="settings-topbar-left">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/icon-eburon.svg" alt="Eburon AI" className="settings-brand-logo" />
          <span className="settings-brand">Orbit Meeting</span>
        </div>
        <div className="settings-topbar-center">
          <SettingsIcon />
          <h1>Settings</h1>
        </div>
        <div className="settings-topbar-right">
          <button
            type="button"
            className="settings-close-btn"
            onClick={() => { if (window.history.length > 1) router.back(); else router.push('/'); }}
            aria-label="Close settings"
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M5 5l10 10M15 5l-10 10" />
            </svg>
          </button>
        </div>
      </header>

      <div className="settings-layout">
        {/* Sidebar nav */}
        <nav className="settings-nav" aria-label="Settings categories">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              className={`settings-nav-item${activeTab === tab.id ? " settings-nav-item--active" : ""}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        {/* Content */}
        <section className="settings-content">
          <form onSubmit={handleSave}>
            {activeTab === "general" && (
              <div className="settings-tab">
                <h2 className="settings-tab-title">General</h2>
                <p className="settings-tab-desc">Configure your profile and app preferences.</p>

                <div className="settings-field">
                  <label className="settings-label">Display Name</label>
                  <input
                    className="settings-input"
                    value={name}
                    onChange={(e) => { setName(e.target.value); markDirty(); }}
                    placeholder="Enter your name"
                    maxLength={40}
                  />
                </div>

                <div className="settings-field">
                  <label className="settings-label">Theme</label>
                  <select
                    className="settings-select"
                    value={theme}
                    onChange={(e) => { setTheme(e.target.value as "light" | "dark"); markDirty(); }}
                    aria-label="Theme"
                  >
                    <option value="dark">Dark</option>
                    <option value="light">Light</option>
                  </select>
                </div>

                <div className="settings-field">
                  <label className="settings-label">Language</label>
                  <select
                    className="settings-select"
                    value={defaultLanguage}
                    onChange={(e) => { setDefaultLanguage(e.target.value); markDirty(); }}
                    aria-label="Language"
                  >
                    {PICKER_LANGUAGES.map(lang => (
                      <option key={lang.code} value={lang.code}>{lang.flag} {lang.name}</option>
                    ))}
                  </select>
                </div>
              </div>
            )}

            {activeTab === "audio" && (
              <div className="settings-tab">
                <h2 className="settings-tab-title">Audio</h2>
                <p className="settings-tab-desc">Configure your microphone and audio preferences.</p>

                <div className="settings-toggle-row">
                  <div>
                    <span className="settings-label">Auto-join audio</span>
                    <p className="settings-hint">Automatically connect to audio when joining a meeting</p>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={autoJoinAudio}
                      onChange={(e) => { setAutoJoinAudio(e.target.checked); markDirty(); }}
                      aria-label="Auto-join audio"
                    />
                    <span className="slider" />
                  </label>
                </div>

                <div className="settings-toggle-row">
                  <div>
                    <span className="settings-label">Background noise suppression</span>
                    <p className="settings-hint">Filter out background noise from your microphone</p>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={noiseSuppression}
                      onChange={(e) => { setNoiseSuppression(e.target.checked); markDirty(); }}
                      aria-label="Background noise suppression"
                    />
                    <span className="slider" />
                  </label>
                </div>
              </div>
            )}

            {activeTab === "video" && (
              <div className="settings-tab">
                <div className="settings-page-header">
                  <h1 className="settings-page-title">Video</h1>
                  <p className="settings-page-subtitle">Configure your camera, preview, and background effects.</p>
                </div>

                {/* Live camera preview with backgrounds */}
                <CameraPreview
                  mirror={mirrorVideo}
                  onMirrorChange={setMirrorVideo}
                  background={videoBackground}
                  onBackgroundChange={setVideoBackground}
                  onDirty={markDirty}
                />

                {/* Turn off camera when joining */}
                <div className="setting-row">
                  <div className="setting-info">
                    <h4>Turn off camera when joining</h4>
                    <p>Keep your camera off when you enter a meeting</p>
                  </div>
                  <div className="setting-actions">
                    <label className="toggle-switch">
                      <input
                        type="checkbox"
                        checked={cameraOffOnJoin}
                        onChange={(e) => { setCameraOffOnJoin(e.target.checked); markDirty(); }}
                        aria-label="Turn off camera when joining"
                      />
                      <span className="slider" />
                    </label>
                  </div>
                </div>

              </div>
            )}

            {activeTab === "translation" && (
              <div className="settings-tab">
                <h2 className="settings-tab-title">Translation</h2>
                <p className="settings-tab-desc">Configure real-time translation preferences.</p>

                <div className="settings-field">
                  <label className="settings-label">Default target language</label>
                  <select
                    className="settings-select"
                    value={defaultLanguage}
                    onChange={(e) => { setDefaultLanguage(e.target.value); markDirty(); }}
                    aria-label="Default target language"
                  >
                    {PICKER_LANGUAGES.map(lang => (
                      <option key={lang.code} value={lang.code}>{lang.flag} {lang.name}</option>
                    ))}
                  </select>
                </div>

                <div className="settings-field">
                  <label className="settings-label">Translation voice</label>
                  <select
                    className="settings-select"
                    value={voice}
                    onChange={(e) => { setVoice(e.target.value); markDirty(); }}
                    aria-label="Translation voice"
                  >
                    {VOICES.map(v => (
                      <option key={v.id} value={v.id}>{v.label}</option>
                    ))}
                  </select>
                </div>

                <div className="settings-field">
                  <label className="settings-label">Shared Screen Content Type</label>
                  <p className="settings-hint">Select the type of content you share to optimize translation</p>
                  <select
                    className="settings-select"
                    value={contentType}
                    onChange={(e) => { setContentType(e.target.value as "normal" | "movie"); markDirty(); }}
                    aria-label="Shared Screen Content Type"
                  >
                    <option value="normal">Normal Presentation</option>
                    <option value="movie">Movie/Cinematic</option>
                  </select>
                </div>

                <div className="settings-toggle-row">
                  <div>
                    <span className="settings-label">Show captions</span>
                    <p className="settings-hint">Display translated captions during meetings</p>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={showCaptions}
                      onChange={(e) => { setShowCaptions(e.target.checked); markDirty(); }}
                      aria-label="Show captions"
                    />
                    <span className="slider" />
                  </label>
                </div>

                <div className="settings-toggle-row">
                  <div>
                    <span className="settings-label">Mute original audio</span>
                    <p className="settings-hint">Silence the speaker&apos;s original language and only hear the translation</p>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={muteOriginalAudio}
                      onChange={(e) => { setMuteOriginalAudio(e.target.checked); markDirty(); }}
                      aria-label="Mute original audio"
                    />
                    <span className="slider" />
                  </label>
                </div>

                <div className="settings-toggle-row">
                  <div>
                    <span className="settings-label">Play translated audio</span>
                    <p className="settings-hint">Hear the translated speech through your speakers</p>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={translateAudioPlayback}
                      onChange={(e) => { setTranslateAudioPlayback(e.target.checked); markDirty(); }}
                      aria-label="Play translated audio"
                    />
                    <span className="slider" />
                  </label>
                </div>

                {/* ——— Translation Test Playground ——— */}
                <div className="settings-divider" />
                <TranslationPlayground voice={voice} />
              </div>
            )}

            {activeTab === "glossary" && (
              <div className="settings-tab">
                <h2 className="settings-tab-title">Custom Glossary</h2>
                <p className="settings-tab-desc">
                  Define terms and phrases that should always be translated in a specific way.
                  Useful for brand names, technical jargon, or specialized vocabulary.
                </p>

                <div className="settings-glossary-section">
                  <div className="settings-glossary-entries">
                    {glossary.map((entry, idx) => (
                      <div key={idx} className="settings-glossary-row">
                        <input
                          className="settings-input settings-input-sm"
                          value={entry.source}
                          onChange={(e) => {
                            const next = [...glossary];
                            next[idx] = { ...next[idx], source: e.target.value };
                            setGlossary(next);
                            markDirty();
                          }}
                          placeholder="Original term"
                          aria-label={`Glossary entry ${idx + 1} original term`}
                        />
                        <span className="settings-glossary-arrow">→</span>
                        <input
                          className="settings-input settings-input-sm"
                          value={entry.translation}
                          onChange={(e) => {
                            const next = [...glossary];
                            next[idx] = { ...next[idx], translation: e.target.value };
                            setGlossary(next);
                            markDirty();
                          }}
                          placeholder="Preferred translation"
                          aria-label={`Glossary entry ${idx + 1} translation`}
                        />
                        <button
                          type="button"
                          className="settings-glossary-remove"
                          onClick={() => {
                            setGlossary(glossary.filter((_, i) => i !== idx));
                            markDirty();
                          }}
                          aria-label={`Remove glossary entry ${idx + 1}`}
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="btn btn-outline btn-sm"
                    onClick={() => {
                      setGlossary([...glossary, { source: "", translation: "" }]);
                      markDirty();
                    }}
                  >
                    + Add term
                  </button>
                </div>
              </div>
            )}

            {activeTab === "recording" && (
              <div className="settings-tab">
                <h2 className="settings-tab-title">Recording</h2>
                <p className="settings-tab-desc">Configure local meeting recording settings.</p>

                <div className="settings-field">
                  <label className="settings-label">Default save location</label>
                  <div className="settings-recording-path-row">
                    <input
                      className="settings-input settings-input-flex"
                      value={recordingSavePath}
                      onChange={(e) => { setRecordingSavePath(e.target.value); markDirty(); }}
                      placeholder="e.g. ~/Recordings or C:\Users\You\Videos"
                    />
                    <button
                      type="button"
                      className="btn btn-outline"
                      onClick={async () => {
                        try {
                          // @ts-expect-error - File System Access API
                          const dir = await window.showDirectoryPicker?.();
                          if (dir) {
                            setRecordingSavePath(dir.name);
                            markDirty();
                            const permissionState = await dir.requestPermission?.({ mode: 'readwrite' });
                            if (permissionState === 'granted') {
                              window.localStorage.setItem('orbit.recording-dir-handle', JSON.stringify({ name: dir.name }));
                            }
                          }
                        } catch (e: unknown) {
                          const err = e as Error;
                          if (err.name !== 'AbortError' && err.name !== 'SecurityError') {
                            console.warn("Directory picker failed:", e);
                          }
                        }
                      }}
                      aria-label="Browse for save folder"
                    >
                      Browse
                    </button>
                  </div>
                  <p className="settings-hint">
                    Choose where recordings are saved. Uses your browser&apos;s File System Access API on supported browsers; falls back to download otherwise.
                  </p>
                </div>

                <div className="settings-toggle-row">
                  <div>
                    <span className="settings-label">Auto-start recording on join</span>
                    <p className="settings-hint">Automatically begin recording when you enter a meeting</p>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={recordingAutoStart}
                      onChange={(e) => { setRecordingAutoStart(e.target.checked); markDirty(); }}
                      aria-label="Auto-start recording"
                    />
                    <span className="slider" />
                  </label>
                </div>

                <div className="settings-info-box">
                  <p><strong>How recording works:</strong> Local recording uses the MediaRecorder API to capture your screen and meeting audio. When you stop recording, the file is saved to your chosen folder (File System Access API) or downloaded via your browser. No server-side storage is used.</p>
                </div>
              </div>
            )}

            <div className="settings-divider" />
            <div className="settings-form-actions">
              <button
                className="btn btn-primary"
                type="submit"
                disabled={saving || !dirty}
              >
                {saving ? "Saving..." : "Save"}
              </button>
              <button
                className="btn btn-outline"
                type="button"
                onClick={() => { if (window.history.length > 1) router.back(); else router.push('/'); }}
              >
                Cancel
              </button>
            </div>
          </form>
        </section>
      </div>
    </main>
  );
}

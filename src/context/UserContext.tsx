"use client";

import React, { createContext, useContext, useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import { useAuth } from "@/context/AuthContext";

export type GlossaryEntry = {
  source: string;       // original term/phrase
  translation: string;  // preferred translation
};

export type UserProfile = {
  id: string;
  name: string;
  theme: "light" | "dark";
  default_language: string;
  voice: string;
  // Audio
  mic_device_id?: string;
  speaker_device_id?: string;
  auto_join_audio?: boolean;
  noise_suppression?: boolean;
  // Video
  cam_device_id?: string;
  mirror_video?: boolean;
  camera_off_on_join?: boolean;
  video_background?: string; // "none" | "blur" | "color-#hex" | "custom-{name}"
  // Translation
  content_type?: "normal" | "movie";
  show_captions?: boolean;
  mute_original_audio?: boolean;
  translate_audio_playback?: boolean;
  glossary?: GlossaryEntry[];
  // Recording
  recording_save_path?: string;
  recording_auto_start?: boolean;
};

type UserContextType = {
  profile: UserProfile | null;
  loading: boolean;
  updateProfile: (updates: Partial<UserProfile>) => Promise<void>;
};

const UserContext = createContext<UserContextType | undefined>(undefined);

const DEFAULT_PROFILE: UserProfile = {
  id: "",
  name: "",
  theme: "dark",
  default_language: "en",
  voice: "Orus",
  auto_join_audio: false,
  noise_suppression: true,
  mirror_video: true,
  camera_off_on_join: false,
  video_background: "none",
  content_type: "normal",
  show_captions: true,
  mute_original_audio: true,
  translate_audio_playback: true,
  glossary: [],
  recording_save_path: "",
  recording_auto_start: false,
};

export function UserProvider({ children }: { children: React.ReactNode }) {
  const { user, loading: authLoading } = useAuth();
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function loadProfile() {
      // Wait for auth to settle
      if (authLoading) return;

      if (!user) {
        // Not logged in — use anonymous profile for limited functionality
        let anonId = window.localStorage.getItem("orbitUserId");
        if (!anonId) {
          anonId = crypto.randomUUID();
          window.localStorage.setItem("orbitUserId", anonId);
        }

        const { data, error } = await supabase
          .from("profiles")
          .select("*")
          .eq("id", anonId)
          .single();

        if (data) {
          setProfile(data);
          document.documentElement.dataset.theme = data.theme || "dark";
        } else {
          const anonProfile = { ...DEFAULT_PROFILE, id: anonId };
          setProfile(anonProfile);
          try {
            await supabase.from("profiles").upsert(anonProfile);
          } catch {
            /* table may not exist */
          }
        }
        setLoading(false);
        return;
      }

      // Logged in — use auth user ID
      try {
        const { data, error } = await supabase
          .from("profiles")
          .select("*")
          .eq("id", user.id)
          .single();

        if (data) {
          setProfile(data);
          document.documentElement.dataset.theme = data.theme || "dark";
        } else {
          // Profile should have been auto-created by the DB trigger,
          // but just in case, create it now
          const newProfile: UserProfile = { ...DEFAULT_PROFILE, id: user.id, name: user.user_metadata?.name || "" };
          setProfile(newProfile);
          try {
            await supabase.from("profiles").upsert(newProfile);
          } catch {
            /* table may not exist */
          }
        }
      } catch (err) {
        console.error("Failed to load profile:", err);
      } finally {
        setLoading(false);
      }
    }

    loadProfile();
  }, [user, authLoading]);

  const updateProfile = async (updates: Partial<UserProfile>) => {
    if (!profile) return;
    const newProfile = { ...profile, ...updates };
    setProfile(newProfile);

    if (updates.theme) {
      document.documentElement.dataset.theme = updates.theme;
      window.localStorage.setItem("orbit.theme", updates.theme);
    }

    try {
      await supabase.from("profiles").upsert(newProfile);
    } catch (err) {
      console.error("Failed to sync profile:", err);
    }
  };

  return (
    <UserContext.Provider value={{ profile, loading, updateProfile }}>
      {children}
    </UserContext.Provider>
  );
}

export function useUser() {
  const context = useContext(UserContext);
  if (context === undefined) {
    throw new Error("useUser must be used within a UserProvider");
  }
  return context;
}

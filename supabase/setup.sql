-- =============================================================================
-- Orbit Meeting — COMPLETE DATABASE SETUP
-- =============================================================================
-- Run this ONCE in your Supabase SQL Editor (or via `supabase db push`).
-- Safe to re-run multiple times (idempotent).
--
-- This file replaces all individual migration files (001–007).
-- It creates:
--   • Tables: profiles, meetings, meeting_participants, recordings,
--             chat_messages, translation_history
--   • Storage bucket: chat-files
--   • Row-Level Security policies for all tables
--   • Indexes for performance
--   • Triggers for auto-created profiles and updated_at
-- =============================================================================

-- =============================================================================
-- 1. EXTENSION
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- 2. FUNCTIONS
-- =============================================================================

-- Auto-create profile row when a new user signs up via Supabase Auth
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  INSERT INTO public.profiles (id, name)
  VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data ->> 'name', '')
  );
  RETURN NEW;
END;
$$;

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

-- =============================================================================
-- 3. TRIGGERS (dropped + recreated for idempotency)
-- =============================================================================

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

DROP TRIGGER IF EXISTS on_profile_updated ON public.profiles;
CREATE TRIGGER on_profile_updated
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

DROP TRIGGER IF EXISTS on_meeting_updated ON public.meetings;
CREATE TRIGGER on_meeting_updated
  BEFORE UPDATE ON public.meetings
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

-- =============================================================================
-- 4. TABLES (idempotent CREATE + ALTER)
-- =============================================================================

-- ── PROFILES ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  name TEXT DEFAULT '',
  theme TEXT DEFAULT 'dark' CHECK (theme IN ('light', 'dark')),
  default_language TEXT DEFAULT 'en',
  voice TEXT DEFAULT 'Orus',
  mic_device_id TEXT,
  speaker_device_id TEXT,
  auto_join_audio BOOLEAN DEFAULT false,
  noise_suppression BOOLEAN DEFAULT true,
  cam_device_id TEXT,
  mirror_video BOOLEAN DEFAULT true,
  camera_off_on_join BOOLEAN DEFAULT false,
  video_background TEXT DEFAULT 'none',
  show_captions BOOLEAN DEFAULT true,
  mute_original_audio BOOLEAN DEFAULT true,
  translate_audio_playback BOOLEAN DEFAULT true,
  recording_save_path TEXT DEFAULT '',
  recording_auto_start BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Extra columns added by later migrations (idempotent)
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS glossary JSONB DEFAULT '[]'::jsonb;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS content_type TEXT DEFAULT 'normal';

-- ── MEETINGS ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.meetings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  creator_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
  title TEXT DEFAULT 'Orbit Meeting',
  scheduled_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  status TEXT DEFAULT 'scheduled'
    CHECK (status IN ('scheduled', 'active', 'ended')),
  room_name TEXT UNIQUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── MEETING PARTICIPANTS ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.meeting_participants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id UUID NOT NULL REFERENCES public.meetings(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  joined_at TIMESTAMPTZ DEFAULT NOW(),
  left_at TIMESTAMPTZ,
  UNIQUE(meeting_id, user_id)
);

-- ── RECORDINGS ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.recordings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id UUID REFERENCES public.meetings(id) ON DELETE SET NULL,
  user_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
  file_name TEXT,
  file_path TEXT,
  file_size BIGINT,
  duration_seconds INTEGER,
  recording_type TEXT DEFAULT 'local'
    CHECK (recording_type IN ('local', 'server')),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── CHAT MESSAGES ────────────────────────────────────────────────────────
-- Note: meeting_id and user_id are TEXT (not UUID FK) because the app uses
-- arbitrary room names (crypto.randomUUID()) and anonymous peer identities.
-- All attachment columns are added idempotently.

CREATE TABLE IF NOT EXISTS public.chat_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id TEXT NOT NULL,
  user_id TEXT,
  message TEXT,
  sender_name TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Attachment columns (from migration 006_chat_attachments)
ALTER TABLE public.chat_messages ADD COLUMN IF NOT EXISTS attachment_name TEXT;
ALTER TABLE public.chat_messages ADD COLUMN IF NOT EXISTS attachment_type TEXT;
ALTER TABLE public.chat_messages ADD COLUMN IF NOT EXISTS attachment_size BIGINT;
ALTER TABLE public.chat_messages ADD COLUMN IF NOT EXISTS attachment_url TEXT;

-- Drop stale FK constraints if table was previously created with UUID columns
ALTER TABLE public.chat_messages DROP CONSTRAINT IF EXISTS chat_messages_meeting_id_fkey;
ALTER TABLE public.chat_messages DROP CONSTRAINT IF EXISTS chat_messages_user_id_fkey;

-- Ensure correct column types (safe no-ops if already TEXT)
ALTER TABLE public.chat_messages ALTER COLUMN meeting_id TYPE TEXT;
ALTER TABLE public.chat_messages ALTER COLUMN meeting_id SET NOT NULL;
ALTER TABLE public.chat_messages ALTER COLUMN user_id TYPE TEXT;
ALTER TABLE public.chat_messages ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE public.chat_messages ALTER COLUMN user_id SET DEFAULT NULL;
ALTER TABLE public.chat_messages ALTER COLUMN message DROP NOT NULL;

-- ── TRANSLATION HISTORY ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.translation_history (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL DEFAULT '',
  room_name TEXT NOT NULL DEFAULT '',
  source_identity TEXT NOT NULL DEFAULT '',
  speaker_name TEXT NOT NULL DEFAULT '',
  source_text TEXT NOT NULL DEFAULT '',
  translated_text TEXT NOT NULL DEFAULT '',
  source_lang TEXT NOT NULL DEFAULT '',
  target_lang TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- 5. ROW LEVEL SECURITY
-- =============================================================================

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meetings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meeting_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.translation_history ENABLE ROW LEVEL SECURITY;

-- ═════════════════════════════════════════════════════════════════════════
-- PROFILES
-- ═════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS profiles_select_own ON public.profiles;
CREATE POLICY profiles_select_own
  ON public.profiles FOR SELECT
  USING (auth.uid() = id);

DROP POLICY IF EXISTS profiles_insert_own ON public.profiles;
CREATE POLICY profiles_insert_own
  ON public.profiles FOR INSERT
  WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS profiles_update_own ON public.profiles;
CREATE POLICY profiles_update_own
  ON public.profiles FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

-- ═════════════════════════════════════════════════════════════════════════
-- MEETINGS
-- ═════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS meetings_select_participant ON public.meetings;
CREATE POLICY meetings_select_participant
  ON public.meetings FOR SELECT
  USING (
    auth.uid() = creator_id
    OR auth.uid() IN (
      SELECT user_id FROM public.meeting_participants WHERE meeting_id = meetings.id
    )
  );

DROP POLICY IF EXISTS meetings_insert_own ON public.meetings;
CREATE POLICY meetings_insert_own
  ON public.meetings FOR INSERT
  WITH CHECK (auth.uid() = creator_id);

DROP POLICY IF EXISTS meetings_update_own ON public.meetings;
CREATE POLICY meetings_update_own
  ON public.meetings FOR UPDATE
  USING (auth.uid() = creator_id);

DROP POLICY IF EXISTS meetings_delete_own ON public.meetings;
CREATE POLICY meetings_delete_own
  ON public.meetings FOR DELETE
  USING (auth.uid() = creator_id);

-- ═════════════════════════════════════════════════════════════════════════
-- MEETING PARTICIPANTS
-- ═════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS mp_select_own ON public.meeting_participants;
CREATE POLICY mp_select_own
  ON public.meeting_participants FOR SELECT
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS mp_insert_own ON public.meeting_participants;
CREATE POLICY mp_insert_own
  ON public.meeting_participants FOR INSERT
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS mp_update_own ON public.meeting_participants;
CREATE POLICY mp_update_own
  ON public.meeting_participants FOR UPDATE
  USING (auth.uid() = user_id);

-- ═════════════════════════════════════════════════════════════════════════
-- RECORDINGS
-- ═════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS recordings_select_own ON public.recordings;
CREATE POLICY recordings_select_own
  ON public.recordings FOR SELECT
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS recordings_insert_own ON public.recordings;
CREATE POLICY recordings_insert_own
  ON public.recordings FOR INSERT
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS recordings_delete_own ON public.recordings;
CREATE POLICY recordings_delete_own
  ON public.recordings FOR DELETE
  USING (auth.uid() = user_id);

-- ═════════════════════════════════════════════════════════════════════════
-- CHAT MESSAGES
-- ═════════════════════════════════════════════════════════════════════════

-- Drop ALL old policies first for clean slate
DROP POLICY IF EXISTS chat_select_meeting ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_own ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_participant ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_anonymous ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_any ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_all ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_all ON public.chat_messages;

-- Open policies: any user in the room can read/write chat (the app filters
-- by meeting_id in the query, so access is implicitly scoped).
CREATE POLICY chat_select_all
  ON public.chat_messages FOR SELECT
  USING (true);

CREATE POLICY chat_insert_all
  ON public.chat_messages FOR INSERT
  WITH CHECK (true);

-- ═════════════════════════════════════════════════════════════════════════
-- TRANSLATION HISTORY
-- ═════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS select_own_history ON public.translation_history;
CREATE POLICY select_own_history ON public.translation_history
  FOR SELECT
  USING (user_id = current_setting('app.user_id', true) OR user_id = '');

DROP POLICY IF EXISTS insert_translation_history ON public.translation_history;
CREATE POLICY insert_translation_history ON public.translation_history
  FOR INSERT
  WITH CHECK (true);

-- =============================================================================
-- 6. INDEXES
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_meetings_creator ON public.meetings(creator_id);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON public.meetings(status);
CREATE INDEX IF NOT EXISTS idx_meeting_participants_meeting ON public.meeting_participants(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_participants_user ON public.meeting_participants(user_id);
CREATE INDEX IF NOT EXISTS idx_recordings_meeting ON public.recordings(meeting_id);
CREATE INDEX IF NOT EXISTS idx_recordings_user ON public.recordings(user_id);

DROP INDEX IF EXISTS idx_chat_messages_meeting;
CREATE INDEX IF NOT EXISTS idx_chat_messages_meeting ON public.chat_messages(meeting_id);

CREATE INDEX IF NOT EXISTS idx_translation_history_user_id ON public.translation_history (user_id);
CREATE INDEX IF NOT EXISTS idx_translation_history_created_at ON public.translation_history (created_at DESC);

-- =============================================================================
-- 7. STORAGE BUCKET: chat-files
-- =============================================================================

-- Create the bucket (10 MB limit, whitelisted MIME types)
INSERT INTO storage.buckets (id, name, public, avif_autodetection, file_size_limit, allowed_mime_types)
SELECT
  'chat-files', 'chat-files', true, false,
  10485760, -- 10 MB
  ARRAY[
    'image/jpeg','image/png','image/gif','image/webp','image/svg+xml',
    'application/pdf','text/plain','text/csv',
    'application/msword','application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint','application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/zip','application/x-zip-compressed',
    'audio/mpeg','audio/wav','audio/ogg','audio/mp4','video/mp4'
  ]
WHERE NOT EXISTS (SELECT 1 FROM storage.buckets WHERE id = 'chat-files');

-- RLS: allow uploads, reads, and deletes
DROP POLICY IF EXISTS chat_files_insert ON storage.objects;
CREATE POLICY chat_files_insert ON storage.objects
  FOR INSERT
  WITH CHECK (bucket_id = 'chat-files');

DROP POLICY IF EXISTS chat_files_select ON storage.objects;
CREATE POLICY chat_files_select ON storage.objects
  FOR SELECT
  USING (bucket_id = 'chat-files');

DROP POLICY IF EXISTS chat_files_delete ON storage.objects;
CREATE POLICY chat_files_delete ON storage.objects
  FOR DELETE
  USING (bucket_id = 'chat-files' AND auth.uid() IS NOT NULL);

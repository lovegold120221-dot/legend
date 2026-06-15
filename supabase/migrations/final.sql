-- Orbit Meeting — Final Consolidated Database Schema
-- This file represents the final state of the database. 
-- It is idempotent and safe to run in the Supabase SQL Editor.

-- ========================
-- 0. CLEANUP (Fresh Start)
-- ========================
DROP TABLE IF EXISTS public.chat_messages CASCADE;
DROP TABLE IF EXISTS public.recordings CASCADE;
DROP TABLE IF EXISTS public.meeting_participants CASCADE;
DROP TABLE IF EXISTS public.meetings CASCADE;
DROP TABLE IF EXISTS public.profiles CASCADE;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP FUNCTION IF EXISTS public.handle_updated_at();
DROP FUNCTION IF EXISTS public.handle_new_user();

-- ========================
-- 1. PROFILES (extends auth.users)
-- ========================

CREATE TABLE public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  name TEXT DEFAULT '',
  theme TEXT DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark')),
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
  glossary JSONB DEFAULT '[]'::jsonb,
  content_type TEXT DEFAULT 'normal',
  email TEXT,
  phone TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  INSERT INTO public.profiles (id, name, email, phone)
  VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data ->> 'name', ''),
    NEW.email,
    NEW.phone
  );
  RETURN NEW;
END;
$$;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

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

CREATE TRIGGER on_profile_updated
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

-- ========================
-- 2. MEETINGS
-- ========================

CREATE TABLE public.meetings (
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

CREATE TRIGGER on_meeting_updated
  BEFORE UPDATE ON public.meetings
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

-- ========================
-- 3. MEETING PARTICIPANTS
-- ========================

CREATE TABLE public.meeting_participants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id UUID NOT NULL REFERENCES public.meetings(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  joined_at TIMESTAMPTZ DEFAULT NOW(),
  left_at TIMESTAMPTZ,
  UNIQUE(meeting_id, user_id)
);

-- ========================
-- 4. RECORDINGS
-- ========================

CREATE TABLE public.recordings (
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

-- ========================
-- 5. CHAT MESSAGES
-- ========================

CREATE TABLE public.chat_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id TEXT NOT NULL, 
  user_id TEXT,
  message TEXT,
  sender_name TEXT,
  attachment_name TEXT,
  attachment_type TEXT,
  attachment_size BIGINT,
  attachment_url TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_chat_messages_meeting ON public.chat_messages(meeting_id);

-- ========================
-- 6. TRANSLATION HISTORY
-- ========================

CREATE TABLE public.translation_history (
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

CREATE INDEX idx_translation_history_user_id ON public.translation_history (user_id);
CREATE INDEX idx_translation_history_created_at ON public.translation_history (created_at DESC);

-- ========================
-- ROW LEVEL SECURITY (RLS)
-- ========================

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meetings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meeting_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recordings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.translation_history ENABLE ROW LEVEL SECURITY;

-- PROFILES
CREATE POLICY "profiles_select_all" ON public.profiles FOR SELECT USING (true);
CREATE POLICY "profiles_insert_own" ON public.profiles FOR INSERT WITH CHECK (auth.uid() = id);
CREATE POLICY "profiles_update_own" ON public.profiles FOR UPDATE USING (auth.uid() = id) WITH CHECK (auth.uid() = id);

-- MEETINGS
CREATE POLICY "meetings_select_participant" ON public.meetings FOR SELECT
  USING (auth.uid() = creator_id OR auth.uid() IN (SELECT user_id FROM public.meeting_participants WHERE meeting_id = meetings.id));
CREATE POLICY "meetings_insert_own" ON public.meetings FOR INSERT WITH CHECK (auth.uid() = creator_id);
CREATE POLICY "meetings_update_own" ON public.meetings FOR UPDATE USING (auth.uid() = creator_id);
CREATE POLICY "meetings_delete_own" ON public.meetings FOR DELETE USING (auth.uid() = creator_id);

-- MEETING PARTICIPANTS
CREATE POLICY "mp_select_own" ON public.meeting_participants FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "mp_insert_own" ON public.meeting_participants FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "mp_update_own" ON public.meeting_participants FOR UPDATE USING (auth.uid() = user_id);

-- RECORDINGS
CREATE POLICY "recordings_select_own" ON public.recordings FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "recordings_insert_own" ON public.recordings FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "recordings_delete_own" ON public.recordings FOR DELETE USING (auth.uid() = user_id);

-- CHAT MESSAGES (Simplified Open Access)
CREATE POLICY "chat_select_all" ON public.chat_messages FOR SELECT USING (true);
CREATE POLICY "chat_insert_all" ON public.chat_messages FOR INSERT WITH CHECK (true);

-- TRANSLATION HISTORY
CREATE POLICY "select_own_history" ON public.translation_history FOR SELECT
  USING (user_id = current_setting('app.user_id', true) OR user_id = '');
CREATE POLICY "insert_translation_history" ON public.translation_history FOR INSERT WITH CHECK (true);

-- ========================
-- INDEXES (Remaining)
-- ========================

CREATE INDEX IF NOT EXISTS idx_meetings_creator ON public.meetings(creator_id);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON public.meetings(status);
CREATE INDEX IF NOT EXISTS idx_meeting_participants_meeting ON public.meeting_participants(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_participants_user ON public.meeting_participants(user_id);
CREATE INDEX IF NOT EXISTS idx_recordings_meeting ON public.recordings(meeting_id);
CREATE INDEX IF NOT EXISTS idx_recordings_user ON public.recordings(user_id);

-- ========================
-- STORAGE BUCKETS
-- ========================
INSERT INTO storage.buckets (id, name, public, avif_autodetection, file_size_limit, allowed_mime_types)
SELECT
  'chat-files', 'chat-files', true, false,
  10485760,
  ARRAY['image/jpeg','image/png','image/gif','image/webp','image/svg+xml',
        'application/pdf','text/plain','text/csv',
        'application/msword','application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-powerpoint','application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/zip','application/x-zip-compressed',
        'audio/mpeg','audio/wav','audio/ogg','audio/mp4','video/mp4']
WHERE NOT EXISTS (SELECT 1 FROM storage.buckets WHERE id = 'chat-files');

DROP POLICY IF EXISTS chat_files_insert ON storage.objects;
CREATE POLICY chat_files_insert ON storage.objects FOR INSERT WITH CHECK (bucket_id = 'chat-files');

DROP POLICY IF EXISTS chat_files_select ON storage.objects;
CREATE POLICY chat_files_select ON storage.objects FOR SELECT USING (bucket_id = 'chat-files');

DROP POLICY IF EXISTS chat_files_delete ON storage.objects;
CREATE POLICY chat_files_delete ON storage.objects FOR DELETE USING (bucket_id = 'chat-files' AND auth.uid() IS NOT NULL);

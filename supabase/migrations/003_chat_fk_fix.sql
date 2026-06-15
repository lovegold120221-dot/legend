-- ========================
-- Migration 003: Fix chat FK constraints for arbitrary room names + anonymous users
-- ========================
-- chat_messages.meeting_id was UUID FK → public.meetings(id), but the app uses
-- arbitrary UUID room names from crypto.randomUUID() that never get a meetings row.
-- chat_messages.user_id was UUID FK → public.profiles(id), but anonymous users
-- don't have a Supabase profile and send their LiveKit identity (string) instead.
--
-- This migration drops RLS policies that depend on the columns, changes both to
-- TEXT, drops FK constraints, then recreates the policies with proper CASTs so the
-- now-TEXT chat_messages.meeting_id can join against UUID columns in related tables.

-- Step 1: Drop ALL RLS policies that could possibly depend on meeting_id or user_id
-- We drop every known policy name to ensure no dependencies remain before the type change.
DROP POLICY IF EXISTS chat_select_participant ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_anonymous ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_any ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_meeting ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_own ON public.chat_messages;

-- Step 2: Drop FK constraints
ALTER TABLE public.chat_messages DROP CONSTRAINT IF EXISTS chat_messages_meeting_id_fkey;
ALTER TABLE public.chat_messages DROP CONSTRAINT IF EXISTS chat_messages_user_id_fkey;

-- Step 3: Change meeting_id from UUID → TEXT (room names are arbitrary strings)
ALTER TABLE public.chat_messages ALTER COLUMN meeting_id TYPE TEXT;
ALTER TABLE public.chat_messages ALTER COLUMN meeting_id SET NOT NULL;

-- Step 4: Change user_id from UUID → TEXT (accepts auth UUIDs or anonymous peer-xxx identities)
ALTER TABLE public.chat_messages ALTER COLUMN user_id TYPE TEXT;
ALTER TABLE public.chat_messages ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE public.chat_messages ALTER COLUMN user_id SET DEFAULT NULL;

-- Step 5: Rebuild indexes for fast chat-history queries
DROP INDEX IF EXISTS idx_chat_messages_meeting;
CREATE INDEX idx_chat_messages_meeting ON public.chat_messages(meeting_id);

-- Step 6: Recreate RLS policies (DROP IF EXISTS for full idempotency)
-- Note: meeting_participants.meeting_id and meetings.id remain UUID, so we CAST
-- them to text for comparison with the now-TEXT chat_messages.meeting_id.
-- This is safe because existing chat_messages values are valid UUID strings.

-- Allow meeting participants (authenticated) to read chat messages
DROP POLICY IF EXISTS chat_select_participant ON public.chat_messages;
CREATE POLICY chat_select_participant
  ON public.chat_messages FOR SELECT
  USING (
    auth.uid() IS NOT NULL AND (
      auth.uid() IN (
        SELECT user_id FROM public.meeting_participants
        WHERE meeting_id::text = chat_messages.meeting_id
      )
      OR auth.uid() = (
        SELECT creator_id FROM public.meetings
        WHERE id::text = chat_messages.meeting_id
      )
    )
  );

-- Allow anonymous users to read chat messages too
DROP POLICY IF EXISTS chat_select_anonymous ON public.chat_messages;
CREATE POLICY chat_select_anonymous
  ON public.chat_messages FOR SELECT
  USING (auth.uid() IS NULL);

-- Allow any user (authenticated or anonymous) to insert chat messages
DROP POLICY IF EXISTS chat_insert_any ON public.chat_messages;
CREATE POLICY chat_insert_any
  ON public.chat_messages FOR INSERT
  WITH CHECK (true);

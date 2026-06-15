-- ========================
-- Migration 004: Complete Chat Fix — idempotent, safe to re-run
-- ========================
-- Fixes three issues:
-- 1. chat_messages.sender_name was missing (needed by ChatSidebar)
-- 2. chat_messages.meeting_id was UUID FK → TEXT (room names are arbitrary UUIDs without meetings row)
-- 3. chat_messages.user_id was UUID FK → TEXT (anonymous users have peer-xxx identities)
-- 4. RLS policies blocked anonymous users from inserting
--
-- Run this in Supabase SQL Editor. Safe to re-run multiple times.

-- Step 0: Drop all existing RLS policies (clean slate, idempotent)
-- We drop every known policy name to ensure no dependencies remain before the type change.
DROP POLICY IF EXISTS chat_select_meeting ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_own ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_participant ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_anonymous ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_any ON public.chat_messages;

-- Step 1: Drop FK constraints (idempotent — IF EXISTS handles already-dropped)
ALTER TABLE public.chat_messages DROP CONSTRAINT IF EXISTS chat_messages_meeting_id_fkey;
ALTER TABLE public.chat_messages DROP CONSTRAINT IF EXISTS chat_messages_user_id_fkey;

-- Step 2: Add sender_name column if missing
ALTER TABLE public.chat_messages ADD COLUMN IF NOT EXISTS sender_name TEXT;

-- Step 3: Change meeting_id from UUID → TEXT (arbitrary room names)
ALTER TABLE public.chat_messages ALTER COLUMN meeting_id TYPE TEXT;
ALTER TABLE public.chat_messages ALTER COLUMN meeting_id SET NOT NULL;

-- Step 4: Change user_id from UUID → TEXT (accepts auth UUIDs or anonymous peer-xxx identities)
ALTER TABLE public.chat_messages ALTER COLUMN user_id TYPE TEXT;
ALTER TABLE public.chat_messages ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE public.chat_messages ALTER COLUMN user_id SET DEFAULT NULL;

-- Step 5: Ensure index for fast chat-history queries
DROP INDEX IF EXISTS idx_chat_messages_meeting;
CREATE INDEX IF NOT EXISTS idx_chat_messages_meeting ON public.chat_messages(meeting_id);

-- Step 6: Recreate RLS policies
-- Meeting participants (authenticated) can SELECT chat messages
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

-- Anonymous users can SELECT chat messages too
CREATE POLICY chat_select_anonymous
  ON public.chat_messages FOR SELECT
  USING (auth.uid() IS NULL);

-- Any user (authenticated or anonymous) can INSERT chat messages
CREATE POLICY chat_insert_any
  ON public.chat_messages FOR INSERT
  WITH CHECK (true);

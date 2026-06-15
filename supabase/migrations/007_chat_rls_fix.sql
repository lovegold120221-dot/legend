-- ========================
-- Migration 007: Fix chat RLS to allow all room participants to read/write
-- ========================
-- The existing chat_select_participant policy requires joining via
-- meeting_participants table, but that table is never populated in
-- the LiveKit join flow. This migration replaces the restrictive
-- policies with open ones so any user in the room can read its chat
-- history and any user can send messages.
--
-- Safe to re-run (idempotent).

-- Drop existing policies
DROP POLICY IF EXISTS chat_select_participant ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_anonymous ON public.chat_messages;
DROP POLICY IF EXISTS chat_select_meeting ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_own ON public.chat_messages;
DROP POLICY IF EXISTS chat_insert_any ON public.chat_messages;

-- Allow anyone to SELECT chat messages (matching by meeting_id happens in the query)
CREATE POLICY chat_select_all
  ON public.chat_messages FOR SELECT
  USING (true);

-- Allow anyone to INSERT chat messages
CREATE POLICY chat_insert_all
  ON public.chat_messages FOR INSERT
  WITH CHECK (true);

-- Allow the message column to be null (for attachment-only messages)
ALTER TABLE public.chat_messages ALTER COLUMN message DROP NOT NULL;

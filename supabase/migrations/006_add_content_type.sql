-- Add content_type column to profiles
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS content_type text DEFAULT 'normal';

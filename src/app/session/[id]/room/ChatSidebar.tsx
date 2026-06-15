"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import {
  useLocalParticipant,
  useRoomContext,
  useDataChannel,
} from "@livekit/components-react";
import { supabase } from "@/lib/supabase";
import { useUser } from "@/context/UserContext";
import { AttachmentIcon } from "./icons";
import type { PostgrestError } from "@supabase/supabase-js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ChatMessage = {
  id: string;
  from: string;
  fromId: string;
  message: string;
  timestamp: number;
  /** Public URL to the uploaded file (Supabase Storage) */
  attachmentUrl?: string;
  /** Original file name */
  attachmentName?: string;
  /** MIME type */
  attachmentType?: string;
  /** File size in bytes */
  attachmentSize?: number;
};

/** While a file is uploading (or ready to send) we track it here */
type PendingAttachment = {
  file: File;
  /** 0 – 100 */
  progress: number;
  /** Set once upload completes */
  url?: string;
  name?: string;
  type?: string;
  size?: number;
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CHAT_TOPIC = "orbit_chat";
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
const STORAGE_BUCKET = "chat-files";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimestamp(ts: number): string {
  return new Date(ts).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Derive a unique storage path inside the bucket */
function storagePath(roomName: string, file: File): string {
  const safeName = `${Date.now()}-${file.name.replace(/[^a-zA-Z0-9._-]/g, "_")}`;
  return `${roomName}/${safeName}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function ChatSidebar({
  onClose,
  privateTo,
}: {
  onClose: () => void;
  privateTo?: string;
}) {
  const { localParticipant } = useLocalParticipant();
  const room = useRoomContext();
  const { profile } = useUser();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [pendingAttachment, setPendingAttachment] =
    useState<PendingAttachment | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const bodyRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const seenIds = useRef(new Set<string>());

  // -----------------------------------------------------------------------
  // Load chat history from Supabase on mount
  // -----------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    async function loadHistory() {
      const { data, error } = await supabase
        .from("chat_messages")
        .select("*")
        .eq("meeting_id", room.name)
        .order("created_at", { ascending: true });

      if (cancelled) return;
      if (error) {
        console.error("Failed to load chat history:", error);
        setLoaded(true);
        return;
      }

      const history: ChatMessage[] = (data || []).map(
        (row: Record<string, unknown>) => {
          const msg: ChatMessage = {
            id: row.id as string,
            from:
              (row.sender_name as string) ||
              (row.user_id as string) ||
              "Unknown",
            fromId: (row.user_id as string) || "",
            message: row.message as string,
            timestamp: row.created_at
              ? new Date(row.created_at as string).getTime()
              : Date.now(),
            attachmentUrl: (row.attachment_url as string) || undefined,
            attachmentName: (row.attachment_name as string) || undefined,
            attachmentType: (row.attachment_type as string) || undefined,
            attachmentSize: row.attachment_size
              ? Number(row.attachment_size)
              : undefined,
          };
          seenIds.current.add(msg.id);
          return msg;
        },
      );

      setMessages(history);
      setLoaded(true);
    }

    loadHistory();
    return () => {
      cancelled = true;
    };
  }, [room.name]);

  // -----------------------------------------------------------------------
  // Listen for incoming real-time chat messages
  // -----------------------------------------------------------------------
  useDataChannel(
    CHAT_TOPIC,
    useCallback((msg: { payload: Uint8Array }) => {
      try {
        const payload = JSON.parse(
          new TextDecoder().decode(msg.payload),
        ) as ChatMessage;
        if (!seenIds.current.has(payload.id)) {
          seenIds.current.add(payload.id);
          setMessages((prev) => [...prev, payload]);
        }
      } catch {
        /* ignore malformed messages */
      }
    }, []),
  );

  // -----------------------------------------------------------------------
  // Auto-scroll
  // -----------------------------------------------------------------------
  useEffect(() => {
    if (!bodyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [messages]);

  // -----------------------------------------------------------------------
  // Upload to Supabase Storage
  // -----------------------------------------------------------------------
  const uploadFile = useCallback(
    async (file: File) => {
      const path = storagePath(room.name, file);
      setPendingAttachment({ file, progress: 0 });

      // Simulate progress — Supabase JS SDK doesn't expose upload progress
      // so we nudge it in stages.
      const progressInterval = setInterval(() => {
        setPendingAttachment((prev) => {
          if (!prev || prev.progress >= 90) return prev;
          return { ...prev, progress: Math.min(prev.progress + 10, 90) };
        });
      }, 300);

      try {
        const { error } = await supabase.storage
          .from(STORAGE_BUCKET)
          .upload(path, file, {
            cacheControl: "3600",
            upsert: false,
          });

        clearInterval(progressInterval);

        if (error) {
          console.error("Upload failed:", error);
          setUploadError(error.message || "Upload failed");
          setPendingAttachment(null);
          return;
        }

        // Get public URL
        const { data: publicUrlData } = supabase.storage
          .from(STORAGE_BUCKET)
          .getPublicUrl(path);

        // Store upload result directly in state (no need for a ref)
        setPendingAttachment({
          file,
          progress: 100,
          url: publicUrlData.publicUrl,
          name: file.name,
          type: file.type,
          size: file.size,
        });
      } catch (err) {
        clearInterval(progressInterval);
        console.error("Upload exception:", err);
        setUploadError("Upload failed. Try again.");
        setPendingAttachment(null);
      }
    },
    [room.name],
  );

  // -----------------------------------------------------------------------
  // File picker
  // -----------------------------------------------------------------------
  const handleFilePick = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;

      // Reset file input so re-picking the same file still fires onChange
      e.target.value = "";

      // Validate size
      if (file.size > MAX_FILE_SIZE) {
        setUploadError(`File too large (max ${formatFileSize(MAX_FILE_SIZE)})`);
        return;
      }

      setUploadError(null);
      // Start uploading immediately
      uploadFile(file);
    },
    [uploadFile],
  );

  // -----------------------------------------------------------------------
  // Send
  // -----------------------------------------------------------------------
  const sendMessage = () => {
    if (!localParticipant) return;
    const hasText = text.trim().length > 0;
    const attachReady =
      pendingAttachment?.progress === 100 && pendingAttachment.url;
    if (!hasText && !attachReady) return;

    const senderName =
      localParticipant.name || localParticipant.identity || "Unknown";
    const senderId = profile?.id || localParticipant.identity || "unknown";

    const msg: ChatMessage = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      from: senderName,
      fromId: senderId,
      message: text.trim(),
      timestamp: Date.now(),
      attachmentUrl: pendingAttachment?.url,
      attachmentName: pendingAttachment?.name,
      attachmentType: pendingAttachment?.type,
      attachmentSize: pendingAttachment?.size,
    };

    // 1. Broadcast via LiveKit data channel
    const encoder = new TextEncoder();
    localParticipant.publishData(encoder.encode(JSON.stringify(msg)), {
      topic: CHAT_TOPIC,
      reliable: true,
    });

    // 2. Add to local state immediately
    seenIds.current.add(msg.id);
    setMessages((prev) => [...prev, msg]);
    setText("");

    // 3. Clear pending attachment
    setPendingAttachment(null);
    setUploadError(null);

    // 4. Persist to Supabase (let DB auto-generate id — our client-side id is not a valid UUID)
    setSaveError(null);
    supabase
      .from("chat_messages")
      .insert({
        meeting_id: room.name,
        user_id: senderId,
        sender_name: senderName,
        message: msg.message || "",
        attachment_url: msg.attachmentUrl || null,
        attachment_name: msg.attachmentName || null,
        attachment_type: msg.attachmentType || null,
        attachment_size: msg.attachmentSize || null,
      })
      .then(({ error }: { error: PostgrestError | null }) => {
        if (error) {
          console.error("Failed to save chat:", error);
          setSaveError("Chat not saved to server. Check Supabase migrations.");
        }
      });
  };

  // -----------------------------------------------------------------------
  // Cancel pending attachment
  // -----------------------------------------------------------------------
  const cancelAttachment = () => {
    setPendingAttachment(null);
    setUploadError(null);
  };

  // -----------------------------------------------------------------------
  // Derived
  // -----------------------------------------------------------------------
  const display = privateTo
    ? messages.filter(
        (m) => m.fromId === privateTo || m.from === privateTo,
      )
    : messages;

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------
  return (
    <div className="sidebar-panel">
      <div className="sidebar-header">
        <span>{privateTo ? `Chat to ${privateTo}` : "Chat"}</span>
        <button className="sidebar-close" onClick={onClose} aria-label="Close">
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      {/* Messages list */}
      <div ref={bodyRef} className="sidebar-body chat-sidebar-body">
        {!loaded ? (
          <div className="chat-sidebar-empty">Loading messages...</div>
        ) : display.length === 0 ? (
          <div className="chat-sidebar-empty">No messages yet. Say hi!</div>
        ) : (
          display.map((msg) => (
            <div key={msg.id} className="chat-sidebar-msg-wrapper">
              <div className="chat-sidebar-msg-header">
                <strong>{msg.from}</strong>
                <span>{formatTimestamp(msg.timestamp)}</span>
              </div>
              {msg.message && (
                <div className="chat-sidebar-msg-content">{msg.message}</div>
              )}
              {msg.attachmentUrl && <AttachmentView msg={msg} />}
            </div>
          ))
        )}
      </div>

      {/* Footer */}
      <div className="chat-sidebar-footer">
        {saveError && <div className="chat-sidebar-error">{saveError}</div>}
        {uploadError && (
          <div className="chat-sidebar-error">{uploadError}</div>
        )}

        {/* Pending attachment chip */}
        {pendingAttachment && (
          <div className="chat-attachment-pending">
            <div className="chat-attachment-pending-info">
              <span className="chat-attachment-pending-name">
                {pendingAttachment.file.name}
              </span>
              <span className="chat-attachment-pending-size">
                {formatFileSize(pendingAttachment.file.size)}
              </span>
            </div>
            {pendingAttachment.progress < 100 ? (
              <div className="chat-attachment-pending-bar-track">
                <div
                  className="chat-attachment-pending-bar-fill"
                  style={{ width: `${pendingAttachment.progress}%` }}
                />
              </div>
            ) : (
              <button
                className="chat-attachment-pending-remove"
                onClick={cancelAttachment}
                aria-label="Remove attachment"
              >
                &times;
              </button>
            )}
          </div>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            sendMessage();
          }}
          className="chat-sidebar-form"
        >
          <button
            type="button"
            className="chat-sidebar-attach-btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={pendingAttachment?.progress !== undefined && pendingAttachment.progress < 100}
            aria-label="Attach file"
          >
            <AttachmentIcon />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            className="chat-sidebar-file-input"
            onChange={handleFilePick}
            accept="image/jpeg,image/png,image/gif,image/webp,image/svg+xml,application/pdf,text/plain,text/csv,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,audio/mpeg,audio/wav,audio/ogg,audio/mp4,video/mp4"
          />
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Type a message..."
            className="chat-sidebar-input"
            autoFocus
          />
          <button
            type="submit"
            disabled={
              (!text.trim() &&
                !(pendingAttachment?.url && pendingAttachment.progress === 100)) ||
              (pendingAttachment !== null && pendingAttachment.progress < 100)
            }
            className="chat-sidebar-btn"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AttachmentView — renders an inline attachment for a received/sent message
// ---------------------------------------------------------------------------

function AttachmentView({ msg }: { msg: ChatMessage }) {
  const isImage = msg.attachmentType?.startsWith("image/");
  const isVideo = msg.attachmentType?.startsWith("video/");
  const isAudio = msg.attachmentType?.startsWith("audio/");

  if (isImage) {
    return (
      <div className="chat-attachment-image-wrapper">
        <a href={msg.attachmentUrl} target="_blank" rel="noopener noreferrer">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={msg.attachmentUrl!}
            alt={msg.attachmentName || "Image attachment"}
            className="chat-attachment-image"
            loading="lazy"
          />
        </a>
      </div>
    );
  }

  if (isVideo) {
    return (
      <div className="chat-attachment-file">
        <video
          src={msg.attachmentUrl}
          controls
          preload="metadata"
          className="chat-attachment-video"
          style={{ maxWidth: "100%", maxHeight: 240, borderRadius: 8 }}
        >
          Your browser does not support the video element.
        </video>
      </div>
    );
  }

  if (isAudio) {
    return (
      <div className="chat-attachment-file">
        <audio
          src={msg.attachmentUrl}
          controls
          preload="none"
          style={{ width: "100%", height: 40 }}
        >
          Your browser does not support the audio element.
        </audio>
      </div>
    );
  }

  // Default: file download card
  const extension = msg.attachmentName?.split(".").pop()?.toUpperCase();

  return (
    <a
      href={msg.attachmentUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="chat-attachment-file"
    >
      <span className="chat-attachment-file-icon">
        {extension || "FILE"}
      </span>
      <span className="chat-attachment-file-meta">
        <span className="chat-attachment-file-name">
          {msg.attachmentName}
        </span>
        {msg.attachmentSize !== undefined && (
          <span className="chat-attachment-file-size">
            {formatFileSize(msg.attachmentSize)}
          </span>
        )}
      </span>
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="chat-attachment-file-dl"
      >
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
        <polyline points="7 10 12 15 17 10" />
        <line x1="12" y1="15" x2="12" y2="3" />
      </svg>
    </a>
  );
}

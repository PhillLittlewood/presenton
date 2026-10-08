"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Film, Loader2, Sparkles, Trash2, Upload, X } from "lucide-react";
import { ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DEFAULT_MOTION_SECONDS,
  MAX_MOTION_SECONDS,
  MIN_MOTION_SECONDS,
  clampMotionSeconds,
  suggestMotionDuration,
} from "@/components/slide-editor/images/motion-duration";
import { MotionVideoApi } from "@/app/(presentation-generator)/services/api/motion-video";
import { notify } from "@/components/ui/sonner";
import { resolveBackendAssetSource } from "@/utils/api";

// A slow or unreachable text LLM must not block generation.
const SUGGEST_TIMEOUT_MS = 25_000;
// Keep in sync with MAX_UPLOAD_VIDEO_BYTES in
// api/v1/ppt/endpoints/motion_video.py -- client-side check is just a fast
// rejection; the server enforces the real limit.
const MAX_UPLOAD_VIDEO_BYTES = 300 * 1024 * 1024;

// Drives the AI prompt/length/generate section specifically.
type AiStatus = "checking" | "unavailable" | "ready";
// Drives which action (if any) is in flight; independent of AiStatus so
// upload and the loop checkbox stay usable even when AI isn't configured.
type Activity = "idle" | "suggesting" | "generating" | "uploading";

/**
 * Generate (or regenerate / remove) an AI motion clip for a slide image.
 *
 * The clip is only a sidecar on the image element (`motion_video`); the still
 * image stays the visual everywhere except the final video export.
 */
export default function MotionClipModal({
  open,
  imageUrl,
  imagePrompt,
  motionVideo,
  motionLoop,
  speakerNote,
  onClose,
  onChange,
  onLoopChange,
}: {
  open: boolean;
  imageUrl: string | null | undefined;
  /** The image's stored generation prompt (ImageElement.prompt). */
  imagePrompt: string | null | undefined;
  motionVideo: string | null | undefined;
  /** Loop the clip instead of fading to the static image when it's shorter
   *  than the slide's narration. */
  motionLoop?: boolean | null;
  /** The slide's script; the clip length is suggested from its narration time. */
  speakerNote?: string | null;
  onClose: () => void;
  /** Called with the new clip URL, or null when the clip was removed. */
  onChange: (motionVideo: string | null) => void;
  onLoopChange: (loop: boolean) => void;
}) {
  const [aiStatus, setAiStatus] = useState<AiStatus>("checking");
  const [activity, setActivity] = useState<Activity>("idle");
  const [prompt, setPrompt] = useState("");
  const [progress, setProgress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const suggestion = useMemo(() => suggestMotionDuration(speakerNote), [speakerNote]);
  const [duration, setDuration] = useState(
    suggestion?.seconds ?? DEFAULT_MOTION_SECONDS,
  );
  const [loop, setLoop] = useState(motionLoop === true);
  const abortRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const suggest = useCallback(async () => {
    setActivity("suggesting");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), SUGGEST_TIMEOUT_MS);
    try {
      setPrompt(
        await MotionVideoApi.suggestPrompt(imagePrompt, undefined, controller.signal),
      );
    } catch (error) {
      // Suggestion is a convenience; the user can still type their own.
      console.warn("Motion prompt suggestion failed", error);
    } finally {
      clearTimeout(timeout);
      setActivity("idle");
    }
  }, [imagePrompt]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPrompt("");
    setProgress("");
    setError(null);
    // Start from the script-based suggestion; the user can change it freely.
    setDuration(suggestion?.seconds ?? DEFAULT_MOTION_SECONDS);
    setLoop(motionLoop === true);
    setAiStatus("checking");
    MotionVideoApi.getStatus()
      .then((status) => {
        if (cancelled) return;
        if (!status.enabled) {
          setAiStatus("unavailable");
          return;
        }
        setAiStatus("ready");
        void suggest();
      })
      .catch(() => {
        if (!cancelled) setAiStatus("unavailable");
      });
    return () => {
      cancelled = true;
    };
    // Only re-run when the modal is opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const busy = activity !== "idle";
  const generating = activity === "generating";
  const uploading = activity === "uploading";

  const handleGenerate = async () => {
    if (!imageUrl) {
      setError("This image has no source to animate. Try replacing the image first.");
      return;
    }
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;
    setActivity("generating");
    setProgress("Queued…");
    try {
      const url = await MotionVideoApi.generateClip(
        {
          imageUrl,
          motionPrompt: prompt.trim(),
          durationSeconds: duration,
          previousMotionVideo: motionVideo,
        },
        { signal: controller.signal, onProgress: setProgress },
      );
      onChange(url);
      notify.success("Motion clip ready", "It plays in place of this image in exported videos.");
      onClose();
    } catch (error) {
      if (controller.signal.aborted) {
        setActivity("idle");
        return;
      }
      // The still image (and any previous clip) is left untouched.
      const message = error instanceof Error ? error.message : "Generation failed";
      setError(message);
      notify.error("Motion clip failed", message);
      setActivity("idle");
    } finally {
      abortRef.current = null;
    }
  };

  const handleUpload = async (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith("video/")) {
      setError("Please upload a video file.");
      return;
    }
    if (file.size > MAX_UPLOAD_VIDEO_BYTES) {
      setError(
        `Video files must be smaller than ${Math.round(MAX_UPLOAD_VIDEO_BYTES / (1024 * 1024))} MB.`,
      );
      return;
    }
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;
    setActivity("uploading");
    try {
      const url = await MotionVideoApi.uploadClip(
        { file, previousMotionVideo: motionVideo },
        { signal: controller.signal },
      );
      onChange(url);
      notify.success("Motion clip uploaded", "It plays in place of this image in exported videos.");
      onClose();
    } catch (error) {
      if (controller.signal.aborted) {
        setActivity("idle");
        return;
      }
      const message = error instanceof Error ? error.message : "Upload failed";
      setError(message);
      notify.error("Upload failed", message);
      setActivity("idle");
    } finally {
      abortRef.current = null;
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    void handleUpload(event.target.files?.[0]);
  };

  const handleRemove = async () => {
    if (!motionVideo) return;
    void MotionVideoApi.deleteClip(motionVideo).catch(() => undefined);
    onChange(null);
    onClose();
  };

  const handleLoopChange = (next: boolean) => {
    // Persists immediately -- it shouldn't require a Generate/Regenerate to
    // take effect, since it also applies to a clip that already exists.
    setLoop(next);
    onLoopChange(next);
  };

  const handleOpenChange = (next: boolean) => {
    if (next) return;
    // Closing mid-generation/upload stops waiting for it (the server-side
    // job may still finish on its own).
    abortRef.current?.abort();
    onClose();
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={handleOpenChange}>
      <DialogPrimitive.Portal>
        {/*
          The editor clears the selection (unmounting the image toolbar that
          hosts this modal) on any pointerdown outside the slide unless the
          target is inside one of these marked elements — same contract as
          ImagePickerModal. Stacking is above the floating toolbar (10000/10001).
        */}
        <DialogPrimitive.Overlay
          data-template-v2-floating-toolbar="true"
          data-inline-edit-ignore="true"
          className="fixed inset-0 z-[10050] bg-black/40"
        />
        <DialogPrimitive.Content
          data-template-v2-floating-toolbar="true"
          data-inline-edit-ignore="true"
          style={{ translate: "none" }}
          onPointerDown={(event) => event.stopPropagation()}
          // Don't lose an in-flight generation or upload to a stray click or
          // Escape; "Stop waiting" is the explicit way out.
          onInteractOutside={(event) => {
            if (busy) event.preventDefault();
          }}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          className="fixed left-1/2 top-1/2 z-[10051] w-[min(520px,92vw)] -translate-x-1/2 -translate-y-1/2 rounded-[16px] bg-white p-6 shadow-xl outline-none"
        >
          <div className="mb-4 flex items-start justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-[8px] bg-[#F4F3FF]">
                <Film className="h-5 w-5 text-[#5146E5]" />
              </div>
              <div>
                <DialogPrimitive.Title className="text-lg font-medium text-[#191919]">
                  Motion clip
                </DialogPrimitive.Title>
                <DialogPrimitive.Description className="text-sm text-gray-500">
                  Animate this image for video export. The image itself is unchanged.
                </DialogPrimitive.Description>
              </div>
            </div>
            <div className="flex flex-none items-center gap-2">
              <button
                type="button"
                title="Upload a video file"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy}
                className="flex h-8 items-center gap-1.5 rounded-full border border-[#EDEEEF] px-3 text-xs font-semibold text-[#191919] transition hover:bg-[#F9FAFB] disabled:cursor-wait disabled:opacity-50"
              >
                {uploading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                ) : (
                  <Upload className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                Upload
              </button>
              <input
                ref={fileInputRef}
                className="sr-only"
                type="file"
                accept="video/*"
                onChange={handleFileInput}
              />
              <DialogPrimitive.Close
                aria-label="Close"
                className="rounded p-1 text-gray-500 hover:bg-gray-100"
              >
                <X className="h-4 w-4" />
              </DialogPrimitive.Close>
            </div>
          </div>

          <div className="space-y-4">
            {aiStatus === "checking" ? (
              <div className="flex items-center gap-2 py-4 text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" /> Checking motion video setup…
              </div>
            ) : aiStatus === "unavailable" ? (
              <p className="rounded-lg border border-[#D9D6FE] bg-[#F4F3FF] p-4 text-sm text-[#5146E5]">
                AI generation isn&apos;t set up (add your ComfyUI LTX workflow under
                Settings → Motion Video) — but you can still upload your own clip above.
              </p>
            ) : (
              <>
                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <label
                      htmlFor="motion-prompt"
                      className="text-sm font-medium text-gray-700"
                    >
                      Motion prompt
                    </label>
                    <button
                      type="button"
                      onClick={() => void suggest()}
                      disabled={busy}
                      className="flex items-center gap-1 text-xs font-medium text-[#5146E5] disabled:opacity-50"
                    >
                      <Sparkles className="h-3 w-3" /> Suggest again
                    </button>
                  </div>
                  <textarea
                    id="motion-prompt"
                    rows={4}
                    value={activity === "suggesting" ? "" : prompt}
                    placeholder={
                      activity === "suggesting"
                        ? "Suggesting a motion prompt…"
                        : "Describe how the image should move, e.g. slow camera push-in with drifting clouds"
                    }
                    onChange={(event) => setPrompt(event.target.value)}
                    disabled={busy}
                    className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:bg-gray-50"
                  />
                </div>

                <div className="flex items-center justify-between gap-4">
                  <label htmlFor="motion-duration" className="text-sm font-medium text-gray-700">
                    Length (seconds)
                  </label>
                  <input
                    id="motion-duration"
                    type="number"
                    min={MIN_MOTION_SECONDS}
                    max={MAX_MOTION_SECONDS}
                    step={1}
                    value={duration}
                    disabled={busy}
                    onChange={(event) => {
                      const next = Math.round(Number(event.target.value));
                      if (Number.isFinite(next)) setDuration(clampMotionSeconds(next));
                    }}
                    className="w-20 rounded-lg border border-gray-300 px-3 py-1.5 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:bg-gray-50"
                  />
                </div>

                <p className="-mt-2 text-xs text-gray-500">
                  {suggestion ? (
                    <>
                      Suggested from this slide&apos;s script: about {suggestion.units}{" "}
                      {suggestion.units === 1 ? "word" : "words"} ≈{" "}
                      {Math.max(1, Math.round(suggestion.narrationSeconds))} s of narration
                      {suggestion.capped ? ` (clips are limited to ${MAX_MOTION_SECONDS} s)` : ""}.
                      {" "}
                      {duration !== suggestion.seconds ? (
                        <button
                          type="button"
                          onClick={() => setDuration(suggestion.seconds)}
                          disabled={busy}
                          className="font-medium text-[#5146E5] underline disabled:opacity-50"
                        >
                          Use suggested ({suggestion.seconds} s)
                        </button>
                      ) : null}
                    </>
                  ) : (
                    "This slide has no script, so a default length is used."
                  )}{" "}
                  Longer clips take much longer to generate.
                </p>
              </>
            )}

            <label className="flex items-start gap-2 text-sm text-gray-700">
              <input
                type="checkbox"
                checked={loop}
                disabled={busy}
                onChange={(event) => handleLoopChange(event.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-gray-300 text-[#7C51F8] focus:ring-[#7C51F8]/40 disabled:opacity-50"
              />
              <span>
                Loop the clip to fill the slide
                <span className="block text-xs text-gray-500">
                  If the clip is shorter than this slide&apos;s narration, it repeats instead of
                  fading back to the still image. A longer clip is trimmed either way.
                </span>
              </span>
            </label>

            {motionVideo ? (
              <div className="flex items-center justify-between rounded-lg bg-[#F9F8F8] p-3 text-sm">
                <a
                  href={resolveBackendAssetSource(motionVideo)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[#5146E5] underline"
                >
                  Preview current clip
                </a>
                <button
                  type="button"
                  onClick={() => void handleRemove()}
                  disabled={busy}
                  className="flex items-center gap-1 text-red-600 disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" /> Remove
                </button>
              </div>
            ) : null}

            {error ? (
              <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                {error}
              </p>
            ) : null}

            {generating ? (
              <div className="flex items-center gap-2 rounded-lg bg-[#F4F3FF] p-3 text-sm text-[#5146E5]">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{progress || "Generating…"} This can take several minutes.</span>
              </div>
            ) : null}

            {uploading ? (
              <div className="flex items-center gap-2 rounded-lg bg-[#F4F3FF] p-3 text-sm text-[#5146E5]">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>Uploading…</span>
              </div>
            ) : null}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => handleOpenChange(false)}
                className="rounded-full border border-[#EDEEEF] px-4 py-2 text-xs font-semibold text-gray-700"
              >
                {busy ? "Stop waiting" : "Cancel"}
              </button>
              {aiStatus === "ready" ? (
                <button
                  type="button"
                  onClick={() => void handleGenerate()}
                  disabled={busy || !imageUrl}
                  className="rounded-full bg-[#7C51F8] px-5 py-2 text-xs font-semibold text-white disabled:opacity-50"
                >
                  {motionVideo ? "Regenerate clip" : "Generate clip"}
                </button>
              ) : null}
            </div>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

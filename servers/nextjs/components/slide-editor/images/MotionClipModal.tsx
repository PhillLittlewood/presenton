"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Film, Loader2, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { MotionVideoApi } from "@/app/(presentation-generator)/services/api/motion-video";
import { notify } from "@/components/ui/sonner";
import { resolveBackendAssetSource } from "@/utils/api";

type Phase = "checking" | "unavailable" | "ready" | "suggesting" | "generating";

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
  onClose,
  onChange,
}: {
  open: boolean;
  imageUrl: string | null | undefined;
  /** The image's stored generation prompt (ImageElement.prompt). */
  imagePrompt: string | null | undefined;
  motionVideo: string | null | undefined;
  onClose: () => void;
  /** Called with the new clip URL, or null when the clip was removed. */
  onChange: (motionVideo: string | null) => void;
}) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [prompt, setPrompt] = useState("");
  const [progress, setProgress] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const suggest = useCallback(async () => {
    setPhase("suggesting");
    try {
      setPrompt(await MotionVideoApi.suggestPrompt(imagePrompt));
    } catch (error) {
      // Suggestion is a convenience; the user can still type their own.
      console.warn("Motion prompt suggestion failed", error);
    } finally {
      setPhase("ready");
    }
  }, [imagePrompt]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPrompt("");
    setProgress("");
    setPhase("checking");
    MotionVideoApi.getStatus()
      .then((status) => {
        if (cancelled) return;
        if (!status.enabled) {
          setPhase("unavailable");
          return;
        }
        void suggest();
      })
      .catch(() => {
        if (!cancelled) setPhase("unavailable");
      });
    return () => {
      cancelled = true;
    };
    // Only re-run when the modal is opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const generating = phase === "generating";

  const handleGenerate = async () => {
    if (!imageUrl) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase("generating");
    setProgress("Queued…");
    try {
      const url = await MotionVideoApi.generateClip(
        {
          imageUrl,
          motionPrompt: prompt.trim(),
          previousMotionVideo: motionVideo,
        },
        { signal: controller.signal, onProgress: setProgress },
      );
      onChange(url);
      notify.success("Motion clip ready", "It plays in place of this image in exported videos.");
      onClose();
    } catch (error) {
      if (controller.signal.aborted) {
        setPhase("ready");
        return;
      }
      // The still image (and any previous clip) is left untouched.
      notify.error(
        "Motion clip failed",
        error instanceof Error ? error.message : "Generation failed",
      );
      setPhase("ready");
    } finally {
      abortRef.current = null;
    }
  };

  const handleRemove = async () => {
    if (!motionVideo) return;
    void MotionVideoApi.deleteClip(motionVideo).catch(() => undefined);
    onChange(null);
    onClose();
  };

  const handleOpenChange = (next: boolean) => {
    if (next) return;
    // Closing mid-generation stops waiting for it (ComfyUI may still finish).
    abortRef.current?.abort();
    onClose();
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={handleOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-[120] bg-black/40" />
        <DialogPrimitive.Content className="fixed left-1/2 top-1/2 z-[121] w-[min(520px,92vw)] -translate-x-1/2 -translate-y-1/2 rounded-[16px] bg-white p-6 shadow-xl outline-none">
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
            <DialogPrimitive.Close
              aria-label="Close"
              className="rounded p-1 text-gray-500 hover:bg-gray-100"
            >
              <X className="h-4 w-4" />
            </DialogPrimitive.Close>
          </div>

          {phase === "checking" ? (
            <div className="flex items-center gap-2 py-8 text-sm text-gray-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Checking motion video setup…
            </div>
          ) : phase === "unavailable" ? (
            <p className="rounded-lg border border-[#D9D6FE] bg-[#F4F3FF] p-4 text-sm text-[#5146E5]">
              Motion video isn&apos;t set up. Add your ComfyUI LTX workflow under
              Settings → Motion Video.
            </p>
          ) : (
            <div className="space-y-4">
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
                    disabled={phase !== "ready"}
                    className="flex items-center gap-1 text-xs font-medium text-[#5146E5] disabled:opacity-50"
                  >
                    <Sparkles className="h-3 w-3" /> Suggest again
                  </button>
                </div>
                <textarea
                  id="motion-prompt"
                  rows={4}
                  value={phase === "suggesting" ? "" : prompt}
                  placeholder={
                    phase === "suggesting"
                      ? "Suggesting a motion prompt…"
                      : "Describe how the image should move, e.g. slow camera push-in with drifting clouds"
                  }
                  onChange={(event) => setPrompt(event.target.value)}
                  disabled={phase !== "ready"}
                  className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:bg-gray-50"
                />
              </div>

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
                    disabled={generating}
                    className="flex items-center gap-1 text-red-600 disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Remove
                  </button>
                </div>
              ) : null}

              {generating ? (
                <div className="flex items-center gap-2 rounded-lg bg-[#F4F3FF] p-3 text-sm text-[#5146E5]">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>{progress || "Generating…"} This can take several minutes.</span>
                </div>
              ) : null}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => handleOpenChange(false)}
                  className="rounded-full border border-[#EDEEEF] px-4 py-2 text-xs font-semibold text-gray-700"
                >
                  {generating ? "Stop waiting" : "Cancel"}
                </button>
                <button
                  type="button"
                  onClick={() => void handleGenerate()}
                  disabled={phase !== "ready" || !imageUrl}
                  className="rounded-full bg-[#7C51F8] px-5 py-2 text-xs font-semibold text-white disabled:opacity-50"
                >
                  {motionVideo ? "Regenerate clip" : "Generate clip"}
                </button>
              </div>
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

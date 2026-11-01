"use client";

import { useCallback, useRef, useState } from "react";
import { useDispatch, useStore } from "react-redux";
import { RootState } from "@/store/store";
import { updateSlideUi } from "@/store/slices/presentationGeneration";
import { MotionVideoApi } from "@/app/(presentation-generator)/services/api/motion-video";
import {
  DEFAULT_MOTION_SECONDS,
  suggestMotionDuration,
} from "@/components/slide-editor/images/motion-duration";
import { notify } from "@/components/ui/sonner";
import { isPlaceholderAssetUrl } from "@/app/(presentation-generator)/presentation/utils/streamAssetMerge";

// How many clips are queued with the backend at once. The server also limits
// itself (VIDEO_MOTION_MAX_CONCURRENCY), this just keeps polling modest.
const PARALLEL_JOBS = 3;
const SUGGEST_TIMEOUT_MS = 25_000;

type PathKey = string | number;

type ImageJob = {
  slideIndex: number;
  path: PathKey[];
  imageUrl: string;
  imagePrompt: string | null;
  seconds: number;
};

export type MotionBatchProgress = {
  total: number;
  done: number;
  failed: number;
  message: string;
};

/**
 * True only for images that carry slide content: the template marks its own
 * backgrounds, panels, logos and ornaments `decorative: true`, while content
 * images (generated, uploaded or inserted in the editor) are `decorative: false`.
 * Icons and unfilled placeholder images are not content either.
 */
function isContentImage(element: Record<string, unknown>): boolean {
  return (
    element.decorative === false &&
    !element.is_icon &&
    typeof element.data === "string" &&
    !isPlaceholderAssetUrl(element.data)
  );
}

/** Finds every content image inside a slide's `ui` tree. */
function collectImages(
  node: unknown,
  path: PathKey[],
  found: { path: PathKey[]; element: Record<string, unknown> }[],
) {
  if (Array.isArray(node)) {
    node.forEach((child, i) => collectImages(child, [...path, i], found));
    return;
  }
  if (!node || typeof node !== "object") return;
  const record = node as Record<string, unknown>;
  if (record.type === "image") {
    if (isContentImage(record)) found.push({ path, element: record });
    return; // an image has no children worth walking
  }
  for (const key of Object.keys(record)) {
    const value = record[key];
    if (value && typeof value === "object") {
      collectImages(value, [...path, key], found);
    }
  }
}

function getAtPath(root: unknown, path: PathKey[]): unknown {
  let current: any = root;
  for (const key of path) {
    if (current == null) return undefined;
    current = current[key];
  }
  return current;
}

/** Immutably sets `motion_video` on the image at `path`. */
function withMotionVideo(ui: unknown, path: PathKey[], url: string): unknown {
  const clone = JSON.parse(JSON.stringify(ui));
  const target = getAtPath(clone, path) as Record<string, unknown> | undefined;
  if (!target || target.type !== "image") return null;
  target.motion_video = url;
  return clone;
}

/**
 * Generates a motion clip for every content image that doesn't have one yet.
 * Template decoration, icons and placeholders are ignored, and images that
 * already have a clip are left alone, so re-running
 * only fills in the gaps and never throws away an existing clip.
 */
export function useGenerateAllMotionClips() {
  const dispatch = useDispatch();
  const store = useStore<RootState>();
  const [progress, setProgress] = useState<MotionBatchProgress | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const running = progress !== null;

  const cancel = useCallback(() => abortRef.current?.abort(), []);

  const start = useCallback(async () => {
    if (abortRef.current) return;

    const status = await MotionVideoApi.getStatus().catch(() => null);
    if (!status?.enabled) {
      notify.error(
        "Motion clips aren't set up",
        "Configure a video motion provider in Settings first.",
      );
      return;
    }

    const slides = store.getState().presentationGeneration.presentationData?.slides;
    const jobs: ImageJob[] = [];
    (slides ?? []).forEach((slide: any, slideIndex: number) => {
      if (!slide?.ui) return;
      const images: { path: PathKey[]; element: Record<string, unknown> }[] = [];
      collectImages(slide.ui, [], images);
      const note = typeof slide.speaker_note === "string" ? slide.speaker_note : null;
      const seconds = suggestMotionDuration(note)?.seconds ?? DEFAULT_MOTION_SECONDS;
      for (const { path, element } of images) {
        if (element.motion_video) continue;
        jobs.push({
          slideIndex,
          path,
          imageUrl: element.data as string,
          imagePrompt: typeof element.prompt === "string" ? element.prompt : null,
          seconds,
        });
      }
    });

    if (jobs.length === 0) {
      notify.info(
        "Nothing to generate",
        "There are no content images without a motion clip.",
      );
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    const state = { total: jobs.length, done: 0, failed: 0, message: "Starting…" };
    setProgress({ ...state });
    const push = (message?: string) => {
      if (message !== undefined) state.message = message;
      setProgress({ ...state });
    };

    let next = 0;
    const worker = async () => {
      while (!controller.signal.aborted) {
        const job = jobs[next++];
        if (!job) return;
        try {
          // The prompt suggestion is a convenience; fall back to no prompt.
          let motionPrompt = "";
          const suggestController = new AbortController();
          const timeout = setTimeout(() => suggestController.abort(), SUGGEST_TIMEOUT_MS);
          try {
            motionPrompt = await MotionVideoApi.suggestPrompt(
              job.imagePrompt,
              undefined,
              suggestController.signal,
            );
          } catch {
            /* keep empty prompt */
          } finally {
            clearTimeout(timeout);
          }

          const url = await MotionVideoApi.generateClip(
            {
              imageUrl: job.imageUrl,
              motionPrompt,
              durationSeconds: job.seconds,
            },
            {
              signal: controller.signal,
              onProgress: (message) =>
                push(`Slide ${job.slideIndex + 1}: ${message}`),
            },
          );

          // Re-read the latest slide so edits made while generating survive.
          const latest = (store.getState().presentationGeneration.presentationData
            ?.slides?.[job.slideIndex] as any)?.ui;
          const target = getAtPath(latest, job.path) as
            | Record<string, unknown>
            | undefined;
          const updated =
            target && target.type === "image" && target.data === job.imageUrl
              ? withMotionVideo(latest, job.path, url)
              : null;
          if (updated) {
            dispatch(
              updateSlideUi({
                index: job.slideIndex,
                ui: updated as Record<string, unknown>,
              }),
            );
            state.done += 1;
          } else {
            // The image was changed or removed meanwhile; drop the orphan clip.
            void MotionVideoApi.deleteClip(url).catch(() => undefined);
            state.failed += 1;
          }
        } catch (error) {
          if (controller.signal.aborted) return;
          console.warn("Motion clip failed for slide", job.slideIndex + 1, error);
          state.failed += 1;
        }
        push();
      }
    };

    await Promise.all(
      Array.from({ length: Math.min(PARALLEL_JOBS, jobs.length) }, worker),
    );

    const cancelled = controller.signal.aborted;
    abortRef.current = null;
    setProgress(null);
    if (cancelled) {
      notify.info(
        "Stopped",
        `${state.done} of ${state.total} clips were added. Clips already queued on the server may still finish.`,
      );
    } else if (state.failed > 0) {
      notify.error(
        "Some clips failed",
        `${state.done} of ${state.total} clips were created, ${state.failed} failed. Run it again to retry the missing ones.`,
      );
    } else {
      notify.success(
        "Motion clips ready",
        `Created ${state.done} clip${state.done === 1 ? "" : "s"}. They play in place of the images in exported videos.`,
      );
    }
  }, [dispatch, store]);

  return { start, cancel, progress, running };
}

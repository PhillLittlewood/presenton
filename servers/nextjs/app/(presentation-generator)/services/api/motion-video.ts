import { getHeader } from "./header";
import { ApiResponseHandler } from "./api-error-handler";
import { getApiUrl } from "@/utils/api";

export interface MotionVideoStatus {
  enabled: boolean;
  configured: boolean;
}

interface AsyncTask {
  id: string;
  status: "pending" | "completed" | "error";
  message?: string | null;
  data?: { motion_video?: string } | null;
  error?: { detail?: string } | null;
}

const POLL_INTERVAL_MS = 3000;
// LTX is slow; give up eventually rather than polling forever.
const POLL_TIMEOUT_MS = 40 * 60 * 1000;

export class MotionVideoApi {
  static async getStatus(): Promise<MotionVideoStatus> {
    const response = await fetch(getApiUrl("/api/v1/ppt/motion-video/status"), {
      cache: "no-cache",
    });
    return (await ApiResponseHandler.handleResponse(
      response,
      "Failed to read motion video status",
    )) as MotionVideoStatus;
  }

  /** Ask the text LLM for a motion prompt, seeded from the image's stored prompt. */
  static async suggestPrompt(
    imagePrompt: string | null | undefined,
    slideContext?: string,
  ): Promise<string> {
    const response = await fetch(
      getApiUrl("/api/v1/ppt/motion-video/suggest-prompt"),
      {
        method: "POST",
        headers: getHeader(),
        body: JSON.stringify({
          image_prompt: imagePrompt ?? null,
          slide_context: slideContext ?? null,
        }),
      },
    );
    const result = (await ApiResponseHandler.handleResponse(
      response,
      "Failed to suggest a motion prompt",
    )) as { motion_prompt: string };
    return result.motion_prompt ?? "";
  }

  /**
   * Generate a clip. LTX takes minutes, so this queues an async task and polls
   * it (same pattern as narration / video export) instead of blocking a request.
   * Resolves with the new clip's URL.
   */
  static async generateClip(
    params: {
      imageUrl: string;
      motionPrompt: string;
      previousMotionVideo?: string | null;
    },
    options?: { signal?: AbortSignal; onProgress?: (message: string) => void },
  ): Promise<string> {
    const startResponse = await fetch(
      getApiUrl("/api/v1/ppt/motion-video/generate/async"),
      {
        method: "POST",
        headers: getHeader(),
        body: JSON.stringify({
          image_url: params.imageUrl,
          motion_prompt: params.motionPrompt || null,
          previous_motion_video: params.previousMotionVideo ?? null,
        }),
        signal: options?.signal,
      },
    );
    const task = (await ApiResponseHandler.handleResponse(
      startResponse,
      "Failed to start motion clip generation",
    )) as AsyncTask;

    const startedAt = Date.now();
    while (Date.now() - startedAt < POLL_TIMEOUT_MS) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      if (options?.signal?.aborted) {
        throw new DOMException("Motion clip generation cancelled", "AbortError");
      }
      const statusResponse = await fetch(
        getApiUrl(`/api/v1/async-tasks/status/${encodeURIComponent(task.id)}`),
        { cache: "no-cache", signal: options?.signal },
      );
      const current = (await ApiResponseHandler.handleResponse(
        statusResponse,
        "Failed to read motion clip status",
      )) as AsyncTask;

      if (current.status === "completed") {
        const url = current.data?.motion_video;
        if (!url) throw new Error("Motion clip finished without a video");
        return url;
      }
      if (current.status === "error") {
        throw new Error(
          current.error?.detail || current.message || "Motion clip generation failed",
        );
      }
      if (current.message) options?.onProgress?.(current.message);
    }
    throw new Error("Motion clip generation timed out");
  }

  static async deleteClip(motionVideo: string): Promise<void> {
    await fetch(getApiUrl("/api/v1/ppt/motion-video/delete"), {
      method: "POST",
      headers: getHeader(),
      body: JSON.stringify({ motion_video: motionVideo }),
    });
  }
}

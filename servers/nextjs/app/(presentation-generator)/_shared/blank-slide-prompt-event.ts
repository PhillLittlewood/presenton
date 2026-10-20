import type { TemplateV2Layout } from "@/components/slide-editor/importing/template-v2-import";

export const PRESENTON_BLANK_SLIDE_PROMPT_EVENT =
  "presenton:blank-slide-prompt";

export type BlankSlidePromptEventDetail = {
  prompt: string;
  slideIndex?: number | null;
  layoutId?: string | null;
  promptKind?: "blank" | "layout";
  layout?: TemplateV2Layout | null;
};

// The "new slide" dialog hands its prompt to the AI chat through a window
// event. The chat only exists while the side panel is open, so an event fired
// with the panel closed used to vanish and the slide stayed generic. Prompts
// are therefore also queued here: a chat that is not mounted yet drains the
// queue when it mounts, and one that is mounted handles the event directly.
// A prompt is delivered exactly once either way.
const PENDING_PROMPT_TTL_MS = 30_000;

type PendingPrompt = {
  detail: BlankSlidePromptEventDetail;
  queuedAt: number;
};

let pendingPrompts: PendingPrompt[] = [];
const handledPrompts = new WeakSet<object>();

export function dispatchBlankSlidePrompt(
  detail: BlankSlidePromptEventDetail,
): void {
  if (typeof window === "undefined") return;
  pendingPrompts.push({ detail, queuedAt: Date.now() });
  window.dispatchEvent(
    new CustomEvent<BlankSlidePromptEventDetail>(
      PRESENTON_BLANK_SLIDE_PROMPT_EVENT,
      { detail },
    ),
  );
}

/**
 * Marks a prompt as handled. Returns false if it was already handled, so the
 * caller must skip it.
 */
export function markBlankSlidePromptHandled(
  detail: BlankSlidePromptEventDetail,
): boolean {
  if (handledPrompts.has(detail)) return false;
  handledPrompts.add(detail);
  pendingPrompts = pendingPrompts.filter((entry) => entry.detail !== detail);
  return true;
}

/** Prompts submitted while no chat was listening (stale ones are dropped). */
export function takePendingBlankSlidePrompts(): BlankSlidePromptEventDetail[] {
  const now = Date.now();
  const fresh = pendingPrompts
    .filter((entry) => now - entry.queuedAt <= PENDING_PROMPT_TTL_MS)
    .map((entry) => entry.detail);
  pendingPrompts = [];
  return fresh;
}

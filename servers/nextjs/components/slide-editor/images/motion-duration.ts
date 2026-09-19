/**
 * Suggests a motion-clip length from a slide's speaker note ("script"), since
 * in the exported video each slide runs for its narration.
 */

export const MIN_MOTION_SECONDS = 2;
export const MAX_MOTION_SECONDS = 20;
export const DEFAULT_MOTION_SECONDS = 5;

// Typical TTS pace: ~150 words per minute.
const WORDS_PER_SECOND = 2.5;
// Chinese / Japanese / Korean have no spaces; count characters instead.
const CJK_CHARS_PER_SECOND = 4.5;
const CJK_PATTERN = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]/g;

export type MotionDurationSuggestion = {
  /** Words (or CJK characters) found in the script. */
  units: number;
  /** Estimated narration length, in seconds. */
  narrationSeconds: number;
  /** Suggested clip length: the narration, clamped to the allowed range. */
  seconds: number;
  /** True when the narration is longer than the maximum clip length. */
  capped: boolean;
};

export function clampMotionSeconds(value: number): number {
  return Math.min(MAX_MOTION_SECONDS, Math.max(MIN_MOTION_SECONDS, Math.round(value)));
}

/** Returns null when the slide has no script to base a suggestion on. */
export function suggestMotionDuration(
  speakerNote: string | null | undefined,
): MotionDurationSuggestion | null {
  const text = typeof speakerNote === "string" ? speakerNote.trim() : "";
  if (!text) return null;

  const cjkChars = (text.match(CJK_PATTERN) ?? []).length;
  const words = (text.replace(CJK_PATTERN, " ").match(/\S+/g) ?? []).length;
  const units = words + cjkChars;
  if (units === 0) return null;

  const narrationSeconds = words / WORDS_PER_SECOND + cjkChars / CJK_CHARS_PER_SECOND;
  const seconds = clampMotionSeconds(narrationSeconds);
  return {
    units,
    narrationSeconds,
    seconds,
    capped: Math.round(narrationSeconds) > MAX_MOTION_SECONDS,
  };
}

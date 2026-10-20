import type { ChatStreamTrace } from "../../../services/api/chat";
import type { AssistantActivity } from "./chat-types";

const TOOL_LABELS: Record<string, string> = {
  addOutline: "Outline adder",
  updateOutline: "Outline editor",
  deleteOutline: "Outline remover",
  addNewSlide: "Blank slide adder",
  addNewSlideLayout: "Layout slide adder",
  getAvailableLayouts: "Layout finder",
  getTemplateSummary: "Template reader",
  readSourceDocuments: "Source document reader",
  searchSlide: "Slide search",
  getSlideAtIndex: "Slide reader",
  saveSlide: "Slide saver",
  setSpeakerNote: "Speaker notes",
  updateSlide: "Slide updater",
  deleteSlide: "Slide remover",
  addElement: "Element adder",
  updateElement: "Element updater",
  deleteElement: "Element remover",
  addComponent: "Component adder",
  createComponent: "Component creator",
  updateComponent: "Component updater",
  deleteComponent: "Component remover",
  getPresentationTheme: "Theme reader",
  setPresentationTheme: "Theme applier",
  generateAssets: "Asset generator",
};

export const MUTATING_TOOLS = new Set([
  "addOutline",
  "updateOutline",
  "deleteOutline",
  "addNewSlide",
  "addNewSlideLayout",
  "saveSlide",
  "updateSlide",
  "setSpeakerNote",
  "deleteSlide",
  "addElement",
  "updateElement",
  "deleteElement",
  "addComponent",
  "createComponent",
  "updateComponent",
  "deleteComponent",
  "setPresentationTheme",
]);

// Read/open traces can happen ahead of edits and would make follow mode jumpy.
export const SLIDE_FOCUS_TOOLS = new Set([
  "addNewSlide",
  "addNewSlideLayout",
  "saveSlide",
  "updateSlide",
  "setSpeakerNote",
  "deleteSlide",
  "addElement",
  "updateElement",
  "deleteElement",
  "addComponent",
  "createComponent",
  "updateComponent",
  "deleteComponent",
]);

export const SLIDE_FOCUS_STATUSES = new Set(["start"]);
export const MIN_SLIDE_FOCUS_DWELL_MS = 700;

const getToolLabel = (tool?: string) => {
  if (!tool) return "";
  return TOOL_LABELS[tool] ?? tool;
};

const humanizeTraceMessage = (message: string, tool?: string) => {
  const trimmed = message.trim();
  if (!trimmed) return "";

  const lower = trimmed.toLowerCase();
  const exactMessages: Record<string, string> = {
    "reading deck context": "Reviewing your presentation context.",
    "reading the presentation outline": "Reading the presentation outline.",
    "reading the outline draft": "Reading the outline draft.",
    "adding an outline slide": "Adding an outline slide.",
    "updating the outline slide": "Updating the outline slide.",
    "deleting the outline slide": "Deleting the outline slide.",
    "reordering outline slides": "Reordering outline slides.",
    "searching relevant slides": "Searching slides for relevant content.",
    "opening the requested slide": "Opening the selected slide.",
    "checking available themes": "Checking available color themes.",
    "checking available layouts": "Checking available layouts.",
    "checking the layout schema": "Validating the slide schema.",
    "generating slide assets": "Generating images and icons.",
    "saving the slide": "Saving slide updates.",
    "deleting the slide": "Deleting the slide.",
    "applying presentation theme": "Applying the selected theme.",
    "reading template structure": "Reading the template structure.",
    "reading source documents": "Reading the source documents.",
    "opening the requested template slide": "Opening the selected template slide.",
    "searching template content": "Searching template content.",
    "finding editable elements": "Finding editable elements.",
    "updating template content": "Updating template content.",
    "deleting the template component": "Deleting the selected component.",
    "swapping component variant": "Swapping the component variant.",
  };
  if (exactMessages[lower]) return exactMessages[lower];

  if (lower.startsWith("using tools:")) {
    const toolNames = trimmed
      .slice("using tools:".length)
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean)
      .map((entry) => getToolLabel(entry));
    return toolNames.length === 0
      ? "Planning the next step."
      : "Choosing the best way to help.";
  }
  if (lower.includes("found requested data")) {
    return tool === "getSlideAtIndex"
      ? "Found the requested slide details."
      : "Found the requested information.";
  }
  return trimmed;
};

export const inferStatusState = (
  status: string,
): AssistantActivity["state"] => {
  const normalized = status.trim().toLowerCase();
  if (
    [
      "preparing",
      "thinking",
      "reading",
      "searching",
      "opening",
      "generating",
      "processing",
      "finalizing",
      "saving",
    ].some((term) => normalized.includes(term))
  ) {
    return "running";
  }
  return "info";
};

export const isAbortError = (error: unknown) =>
  (error instanceof DOMException && error.name === "AbortError") ||
  (error instanceof Error &&
    error.message.toLowerCase().includes("aborted") &&
    error.message.toLowerCase().includes("request"));

export const stripBackendContextFromUserMessage = (rawMessage: string) => {
  const message = rawMessage ?? "";
  if (!message.startsWith("UI context:")) return message;

  const marker = "\nUser message:";
  const markerIndex = message.indexOf(marker);
  if (markerIndex === -1) return message;
  return message.slice(markerIndex + marker.length).trimStart();
};

const humanActivityForTool = (
  tool: string | undefined,
  state: "start" | "success",
) => {
  const isDone = state === "success";
  switch (tool) {
    case "searchSlide":
      return isDone
        ? "Found the relevant content."
        : "Looking through the content.";
    case "getSlideAtIndex":
      return isDone ? "Checked the slide." : "Checking the slide.";
    case "addNewSlide":
    case "addNewSlideLayout":
    case "updateElement":
    case "updateComponent":
    case "addElement":
    case "addComponent":
    case "createComponent":
    case "updateSlide":
    case "saveSlide":
      return isDone ? "Applied the change." : "Applying the change.";
    case "setSpeakerNote":
      return isDone ? "Saved the speaker notes." : "Saving the speaker notes.";
    case "deleteComponent":
    case "deleteElement":
    case "deleteSlide":
      return isDone
        ? "Removed the selected item."
        : "Removing the selected item.";
    case "generateAssets":
      return isDone
        ? "Prepared the visual assets."
        : "Preparing visual assets.";
    case "setPresentationTheme":
      return isDone ? "Updated the theme." : "Updating the theme.";
    default:
      return isDone ? "Finished that step." : "Working on it.";
  }
};

export const formatTraceActivity = (
  trace: ChatStreamTrace,
): Omit<AssistantActivity, "id"> | null => {
  if (typeof trace.message === "string" && trace.message.trim().length > 0) {
    return {
      label: humanizeTraceMessage(trace.message, trace.tool),
      kind: trace.kind,
      round: trace.round,
      tool: trace.tool,
      state:
        trace.status === "error"
          ? "error"
          : trace.status === "success"
            ? "success"
            : trace.status === "ready" || trace.status === "info"
              ? "info"
              : "running",
    };
  }
  if (trace.tool && trace.status === "start") {
    return {
      label: humanActivityForTool(trace.tool, "start"),
      kind: trace.kind,
      round: trace.round,
      tool: trace.tool,
      state: "running",
    };
  }
  if (trace.tool && trace.status === "success") {
    return {
      label: humanActivityForTool(trace.tool, "success"),
      kind: trace.kind,
      round: trace.round,
      tool: trace.tool,
      state: "success",
    };
  }
  if (trace.tool && trace.status === "error") {
    return {
      label: "I could not finish that step.",
      kind: trace.kind,
      round: trace.round,
      tool: trace.tool,
      state: "error",
    };
  }
  if (trace.kind === "tool_plan" && Array.isArray(trace.tools) && trace.tools.length) {
    return {
      label: "Planning the next step.",
      kind: trace.kind,
      round: trace.round,
      state: "info",
    };
  }
  return null;
};

export const readTraceSlideIndex = (trace: ChatStreamTrace) => {
  if (typeof trace.slideIndex === "number" && trace.slideIndex >= 0) {
    return trace.slideIndex;
  }
  if (typeof trace.slideNumber === "number" && trace.slideNumber > 0) {
    return trace.slideNumber - 1;
  }
  return null;
};

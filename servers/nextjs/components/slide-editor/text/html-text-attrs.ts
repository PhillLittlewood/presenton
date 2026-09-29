import {
  asRecord,
  readArray,
  readString,
  type RawElement,
} from "@/components/slide-editor/model/model";

export const HTML_TEXT_WIDTH_ATTR = "presentonHtmlTextWidth";
export const HTML_TEXT_HEIGHT_ATTR = "presentonHtmlTextHeight";

export function shouldRenderTextElementAsHtml(element: RawElement) {
  const type = readString(element.type);
  if (type === "text") {
    return (
      runsContainLatex(readArray(element.runs)) ||
      containsRtlText(readString(element.text) ?? "") ||
      runsContainRtlText(readArray(element.runs))
    );
  }
  if (type !== "text-list") return false;

  return readArray(element.items).some((item) => {
    if (typeof item === "string") return containsRtlText(item);
    if (Array.isArray(item)) {
      return runsContainLatex(item) || runsContainRtlText(item);
    }
    const record = asRecord(item);
    return Boolean(
      record &&
        (containsRtlText(readString(record.text) ?? "") ||
          runsContainLatex(readArray(record.runs)) ||
          runsContainRtlText(readArray(record.runs))),
    );
  });
}

function runsContainLatex(runs: unknown[]) {
  return runs.some((run) => readString(asRecord(run)?.type) === "latex");
}

function runsContainRtlText(runs: unknown[]) {
  return runs.some((run) =>
    containsRtlText(readString(asRecord(run)?.text) ?? ""),
  );
}

// Konva lays rich-text runs out as individual left-to-right canvas segments.
// Route scripts that require bidi reordering through the browser text engine so
// their visual order stays identical when the DOM-based editor is opened.
export function containsRtlText(text: string) {
  return /[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]/u.test(text);
}

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any


class _LatexTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.runs: list[dict[str, str]] = []
        self._buffer: list[str] = []
        self._in_latex = False
        self.saw_latex = False
        self.invalid = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "latex":
            self._buffer.append(self.get_starttag_text() or f"<{tag}>")
            return
        if self._in_latex:
            self.invalid = True
            return
        self._flush()
        self._in_latex = True
        self.saw_latex = True

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._buffer.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "latex":
            self._buffer.append(f"</{tag}>")
            return
        if not self._in_latex:
            self.invalid = True
            return
        self._flush()
        self._in_latex = False

    def handle_data(self, data: str) -> None:
        self._buffer.append(data)

    def handle_entityref(self, name: str) -> None:
        self._buffer.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._buffer.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._buffer.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._buffer.append(f"<!{decl}>")

    def finish(self) -> list[dict[str, str]] | None:
        self.close()
        if self._in_latex:
            self.invalid = True
        self._flush()
        if self.invalid or not self.saw_latex:
            return None
        return self.runs

    def _flush(self) -> None:
        content = "".join(self._buffer)
        self._buffer.clear()
        if content == "":
            return
        if self._in_latex:
            latex = normalize_latex(content)
            if not latex:
                self.invalid = True
                return
            run = {"type": "latex", "latex": latex}
        else:
            run = {"text": content}

        if self.runs and _is_latex_run(self.runs[-1]) == _is_latex_run(run):
            key = "latex" if _is_latex_run(run) else "text"
            self.runs[-1][key] += run[key]
        else:
            self.runs.append(run)


def parse_latex_tags(value: str) -> list[dict[str, str]] | None:
    parser = _LatexTagParser()
    parser.feed(value)
    return parser.finish()


def replace_text_runs(
    existing_runs: Any,
    value: str,
    fallback_font: Any = None,
    *,
    parse_markdown_bold: bool = False,
) -> list[dict[str, Any]]:
    parsed_runs = parse_latex_tags(value)
    templates = (
        [run for run in existing_runs if isinstance(run, dict)]
        if isinstance(existing_runs, list)
        else []
    )
    if parsed_runs is None:
        expanded_runs = (
            _parse_markdown_bold([{"text": value}], value)
            if parse_markdown_bold
            else [{"text": value}]
        )
        return [
            _replace_single_run(
                templates[0] if templates else None,
                parsed_run["text"],
                fallback_font,
                bold=parsed_run.get("_bold") is True,
            )
            for parsed_run in expanded_runs
        ]

    expanded_runs = (
        _parse_markdown_bold(parsed_runs, value)
        if parse_markdown_bold
        else [
            {**parsed_run, "_source_index": index}
            for index, parsed_run in enumerate(parsed_runs)
        ]
    )
    return [
        _build_parsed_run(
            parsed_run,
            _matching_template_run(
                templates,
                parsed_run,
                int(parsed_run.get("_source_index", index)),
            ),
            fallback_font,
        )
        for index, parsed_run in enumerate(expanded_runs)
    ]


def text_runs_to_tagged_text(runs: Any) -> str:
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if _is_latex_run(run):
            parts.append(f"<latex>{str(run.get('latex') or '')}</latex>")
        else:
            parts.append(str(run.get("text") or ""))
    return "".join(parts)


def normalize_latex(value: str) -> str:
    normalized = value.strip()
    if (
        normalized.startswith("$$")
        and normalized.endswith("$$")
        and len(normalized) > 4
    ):
        return normalized[2:-2].strip()[:4000]
    if (
        normalized.startswith(r"\[")
        and normalized.endswith(r"\]")
        and len(normalized) > 4
    ):
        return normalized[2:-2].strip()[:4000]
    return normalized[:4000]


def _replace_single_run(
    template: dict[str, Any] | None,
    value: str,
    fallback_font: Any,
    *,
    bold: bool = False,
) -> dict[str, Any]:
    run = copy.deepcopy(template) if isinstance(template, dict) else {}
    _apply_fallback_font(run, fallback_font)
    if _is_latex_run(run):
        run["latex"] = normalize_latex(value)
        run.pop("text", None)
    else:
        run["text"] = value
    if bold:
        _apply_bold(run)
    return run


def _build_parsed_run(
    parsed_run: dict[str, str],
    template: dict[str, Any] | None,
    fallback_font: Any,
) -> dict[str, Any]:
    run = copy.deepcopy(template) if isinstance(template, dict) else {}
    _apply_fallback_font(run, fallback_font)
    if _is_latex_run(parsed_run):
        was_latex = _is_latex_run(run)
        run["type"] = "latex"
        run["latex"] = parsed_run["latex"]
        run.pop("text", None)
        if not was_latex:
            run["display_mode"] = False
    else:
        run.pop("type", None)
        run.pop("latex", None)
        run.pop("display_mode", None)
        run["text"] = parsed_run["text"]
    if parsed_run.get("_bold") is True:
        _apply_bold(run)
    return run


def _parse_markdown_bold(
    parsed_runs: list[dict[str, str]],
    original_value: str,
) -> list[dict[str, Any]]:
    """Convert Markdown strong markers into styled runs, including a leading open marker."""
    marker_locations: list[tuple[int, int]] = []
    for run_index, run in enumerate(parsed_runs):
        if _is_latex_run(run):
            continue
        text = run.get("text", "")
        marker_locations.extend(
            (run_index, match.start()) for match in _unescaped_bold_markers(text)
        )

    active_markers: set[tuple[int, int]] = set()
    for index in range(0, len(marker_locations) - 1, 2):
        opening = marker_locations[index]
        closing = marker_locations[index + 1]
        if _bold_pair_has_content(parsed_runs, opening, closing):
            active_markers.update((opening, closing))

    if (
        len(marker_locations) == 1
        and original_value.startswith("**")
        and len(original_value) > 2
    ):
        active_markers.add(marker_locations[0])

    if not active_markers:
        return [
            {**run, "_source_index": index} for index, run in enumerate(parsed_runs)
        ]

    expanded: list[dict[str, Any]] = []
    bold = False
    for run_index, run in enumerate(parsed_runs):
        if _is_latex_run(run):
            expanded.append(
                {
                    **run,
                    "_source_index": run_index,
                    "_bold": bold,
                }
            )
            continue

        text = run.get("text", "")
        cursor = 0
        for match in _unescaped_bold_markers(text):
            marker = (run_index, match.start())
            if marker not in active_markers:
                continue
            if match.start() > cursor:
                expanded.append(
                    {
                        "text": text[cursor : match.start()],
                        "_source_index": run_index,
                        "_bold": bold,
                    }
                )
            bold = not bold
            cursor = match.end()

        if cursor < len(text):
            expanded.append(
                {
                    "text": text[cursor:],
                    "_source_index": run_index,
                    "_bold": bold,
                }
            )

    return expanded


def _bold_pair_has_content(
    parsed_runs: list[dict[str, str]],
    opening: tuple[int, int],
    closing: tuple[int, int],
) -> bool:
    opening_run, opening_offset = opening
    closing_run, closing_offset = closing
    if opening_run == closing_run:
        text = parsed_runs[opening_run].get("text", "")
        return bool(text[opening_offset + 2 : closing_offset].strip())

    content = [parsed_runs[opening_run].get("text", "")[opening_offset + 2 :]]
    for run in parsed_runs[opening_run + 1 : closing_run]:
        content.append(
            run.get("latex", "") if _is_latex_run(run) else run.get("text", "")
        )
    content.append(parsed_runs[closing_run].get("text", "")[:closing_offset])
    return bool("".join(content).strip())


def _unescaped_bold_markers(value: str) -> Iterator[re.Match[str]]:
    return re.finditer(r"(?<!\\)\*\*", value)


def _apply_bold(run: dict[str, Any]) -> None:
    font = run.get("font")
    if not isinstance(font, dict):
        font = {}
        run["font"] = font
    font["bold"] = True


def _matching_template_run(
    templates: list[dict[str, Any]],
    parsed_run: dict[str, str],
    index: int,
) -> dict[str, Any] | None:
    if index < len(templates) and _is_latex_run(templates[index]) == _is_latex_run(
        parsed_run
    ):
        return templates[index]
    for template in templates:
        if _is_latex_run(template) == _is_latex_run(parsed_run):
            return template
    if index < len(templates):
        return templates[index]
    return templates[0] if templates else None


def _apply_fallback_font(run: dict[str, Any], fallback_font: Any) -> None:
    if isinstance(fallback_font, dict) and not isinstance(run.get("font"), dict):
        run["font"] = copy.deepcopy(fallback_font)


def _is_latex_run(run: dict[str, Any]) -> bool:
    return run.get("type") == "latex"

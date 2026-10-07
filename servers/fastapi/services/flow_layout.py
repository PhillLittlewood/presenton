"""
Python port of the editor's flex/grid flow-layout engine
(servers/nextjs/components/slide-editor/layout/flowLayout.ts +
wrappedFlexLayout.ts). That TypeScript module is the single source of truth
for "flex"/"list-view"/"grid"/"grid-view" child placement — it is used both
by the Konva editor and by the real HTML renderer that the video-export
base image is rasterized from (lib/template-v2-json-to-html.ts calls it and
bakes the resulting pixel box into each child's `size` before the browser
ever lays anything out), so matching it here keeps motion-clip placement
exact for slides that use these containers instead of falling back to the
static image for every image inside one.

This is a line-for-line port of the non-wrapped and wrapped flex algorithms
and the grid algorithm, with one deliberate simplification: real text
sizing (`measureNoWrapTextWidth`) needs an actual text-rendering engine.
Where the JS would measure text, this port uses the same coarse
character-count heuristic the app itself falls back to elsewhere
(`htmlElementSize`'s text branch) — close for short captions/labels, but an
approximation. Every other rule (explicit sizes, padding, gap, alignment,
grow, wrap, grid placement/spans) is exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

DECORATIVE_LINE_LENGTH = 80.0
DECORATIVE_LINE_THICKNESS = 4.0
TEXT_AVERAGE_CHAR_EM = 0.5


@dataclass(frozen=True)
class FlowBox:
    x: float
    y: float
    width: float
    height: float


# -- small readers, mirroring the JS helpers of the same name ---------------------


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _record(value: Any) -> Optional[dict]:
    return value if isinstance(value, dict) else None


def flow_layout_kind(element: dict) -> Optional[str]:
    kind = _str(element.get("type"))
    if kind in ("flex", "list-view"):
        return "flex"
    if kind in ("grid", "grid-view"):
        return "grid"
    return None


def is_flow_layout_element(element: dict) -> bool:
    return flow_layout_kind(element) is not None


def read_layout_children(item: dict) -> list[dict]:
    for key in ("children", "elements"):
        values = item.get(key)
        if isinstance(values, list) and values:
            return [v for v in values if isinstance(v, dict)]
    child = _record(item.get("item"))
    count = int(_num(item.get("count")) or 0)
    if child and count > 0:
        return [child] * count
    return []


def _read_padding(value: Any) -> dict[str, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return {"top": v, "right": v, "bottom": v, "left": v}
    record = _record(value) or {}
    x = _num(record.get("x")) or _num(record.get("horizontal"))
    y = _num(record.get("y")) or _num(record.get("vertical"))
    return {
        "top": _num(record.get("top")) if _num(record.get("top")) is not None else (y or 0.0),
        "right": _num(record.get("right")) if _num(record.get("right")) is not None else (x or 0.0),
        "bottom": _num(record.get("bottom")) if _num(record.get("bottom")) is not None else (y or 0.0),
        "left": _num(record.get("left")) if _num(record.get("left")) is not None else (x or 0.0),
    }


def _read_optional_size(value: Any) -> Optional[tuple[float, float]]:
    record = _record(value)
    if not record:
        return None
    w, h = _num(record.get("width")), _num(record.get("height"))
    if w is None or h is None:
        return None
    return (max(1.0, w), max(1.0, h))


def _layout_number(child: dict, *keys: str) -> Optional[float]:
    layout = _record(child.get("layout")) or {}
    for key in keys:
        value = _num(layout.get(key))
        if value is not None:
            return value
    return None


def _alignment_offset(alignment: Optional[str], available: float, used: float) -> float:
    free = max(0.0, available - used)
    if alignment == "center":
        return free / 2
    if alignment in ("right", "bottom", "end", "flex-end"):
        return free
    return 0.0


def _clamp_layout_size(
    size: float, child: dict, dimension: str, fallback: float = 1.0
) -> float:
    value = size if (math.isfinite(size) and size > 0) else fallback
    if dimension == "width":
        min_v = _layout_number(child, "minWidth", "min_width")
        max_v = _layout_number(child, "maxWidth", "max_width")
    else:
        min_v = _layout_number(child, "minHeight", "min_height")
        max_v = _layout_number(child, "maxHeight", "max_height")
    lo = max(min_v if min_v is not None else 1.0, value)
    return min(max_v if max_v is not None else math.inf, lo)


def _is_manual_positioned(child: dict) -> bool:
    # AI-generated / template content never sets this; it's set only by
    # interactive drag-repositioning in the editor.
    return child.get("__presenton_manual_position") is True


def _is_frameless_decorative_shape(child: dict) -> bool:
    if _read_optional_size(child.get("size")) or _record(child.get("position")):
        return False
    if _str(child.get("type")) != "vector":
        return False
    if _str(child.get("shape")) == "ellipse":
        return False
    points = child.get("points")
    return isinstance(points, list) and len(points) == 2 and child.get("closed") is not True


# -- text size approximation (see module docstring) --------------------------------


def _raw_font(item: dict) -> dict:
    font = _record(item.get("font")) or {}
    return {
        "size": _num(font.get("size")) or 16.0,
        "bold": bool(font.get("bold")),
        "line_height": _num(font.get("line_height") or font.get("lineHeight")) or 1.2,
    }


def _display_text(item: dict) -> str:
    runs = item.get("runs")
    if isinstance(runs, list) and runs:
        return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    text = item.get("text")
    return text if isinstance(text, str) else ""


def _estimate_text_width(text: str, font: dict) -> float:
    longest_line = max((len(line) for line in text.split("\n")), default=0)
    width = max(font["size"], longest_line * font["size"] * (0.56 if font["bold"] else 0.5))
    return max(font["size"], width)


def _estimate_text_height(text: str, font: dict, width: float) -> float:
    line_height = font["size"] * font["line_height"]
    avg_char_width = max(1.0, font["size"] * TEXT_AVERAGE_CHAR_EM)
    chars_per_line = max(1, int(width / avg_char_width))
    lines = sum(max(1, math.ceil(len(line) / chars_per_line)) for line in text.split("\n")) or 1
    return max(line_height, lines * line_height)


def _intrinsic_text_main_size(child: dict, direction: str, cross_size: float) -> float:
    font = _raw_font(child)
    text = _display_text(child)
    if direction == "row":
        return max(1.0, _estimate_text_width(text, font))
    explicit = _read_optional_size(child.get("size"))
    width = max(1.0, (explicit[0] if explicit else cross_size))
    return max(1.0, _estimate_text_height(text, font, width))


# -- element size (fallback used by flexBasis/childCrossSize/intrinsic sizing) -----


def _element_size(
    child: dict, fallback: Optional[tuple[float, float]] = None
) -> tuple[float, float]:
    explicit = _read_optional_size(child.get("size"))
    if explicit:
        return explicit

    kind = _str(child.get("type"))
    if kind == "group":
        children = [c for c in (child.get("children") or []) if isinstance(c, dict)]
        return _children_bounds(children)
    if kind == "container":
        padding = _read_padding(child.get("padding"))
        inner = _record(child.get("child"))
        child_size = _element_size(inner, fallback) if inner else fallback
        if child_size:
            return (
                child_size[0] + padding["left"] + padding["right"],
                child_size[1] + padding["top"] + padding["bottom"],
            )
    if kind == "text":
        if fallback:
            return fallback
        font = _raw_font(child)
        text = _display_text(child)
        longest_line = max((len(line) for line in text.split("\n")), default=0)
        width = max(font["size"], longest_line * font["size"] * (0.56 if font["bold"] else 0.5))
        avg_char_width = max(1.0, font["size"] * 0.5)
        chars_per_line = max(1, int(width / avg_char_width))
        lines = sum(
            max(1, math.ceil(len(line) / chars_per_line)) for line in text.split("\n")
        ) or 1
        return (width, max(font["size"] * font["line_height"], lines * font["size"] * font["line_height"]))
    if kind in ("flex", "list-view", "grid", "grid-view"):
        if fallback:
            return fallback
        children = read_layout_children(child)
        return intrinsic_flow_size(child, children)
    return fallback or (max(1.0, _num(_record(child.get("size") or {}).get("width")) or 1.0),
                         max(1.0, _num(_record(child.get("size") or {}).get("height")) or 1.0))


def _children_bounds(children: list[dict]) -> tuple[float, float]:
    max_right, max_bottom = 1.0, 1.0
    for child in children:
        px = _num(_record(child.get("position") or {}).get("x")) or 0.0
        py = _num(_record(child.get("position") or {}).get("y")) or 0.0
        w, h = _element_size(child)
        max_right = max(max_right, px + w)
        max_bottom = max(max_bottom, py + h)
    return (max_right, max_bottom)


def _intrinsic_flow_child_size(child: dict) -> tuple[float, float]:
    if _is_manual_positioned(child):
        box = _element_box(child)
        return (max(1.0, box.width), max(1.0, box.height))
    return _element_size(child)


def intrinsic_flow_size(parent: dict, children: list[dict]) -> tuple[float, float]:
    kind = flow_layout_kind(parent)
    if kind == "flex":
        return _intrinsic_flex_size(parent, children)
    if kind == "grid":
        return _intrinsic_grid_size(parent, children)
    return (1.0, 1.0)


def _intrinsic_flex_size(parent: dict, children: list[dict]) -> tuple[float, float]:
    padding = _read_padding(parent.get("padding"))
    if not children:
        return (max(1.0, padding["left"] + padding["right"]), max(1.0, padding["top"] + padding["bottom"]))
    is_column = _str(parent.get("direction")) != "row"
    main_gap = (
        (_num(parent.get("row_gap")) or _num(parent.get("rowGap")))
        if is_column
        else (_num(parent.get("column_gap")) or _num(parent.get("columnGap")))
    )
    main_gap = main_gap if main_gap is not None else (_num(parent.get("gap")) or 0.0)

    main = cross = 0.0
    for child in children:
        w, h = _intrinsic_flow_child_size(child)
        main += h if is_column else w
        cross = max(cross, w if is_column else h)
    main += main_gap * max(0, len(children) - 1)
    if is_column:
        return (max(1.0, padding["left"] + padding["right"] + cross), max(1.0, padding["top"] + padding["bottom"] + main))
    return (max(1.0, padding["left"] + padding["right"] + main), max(1.0, padding["top"] + padding["bottom"] + cross))


def _intrinsic_grid_size(parent: dict, children: list[dict]) -> tuple[float, float]:
    padding = _read_padding(parent.get("padding"))
    if not children:
        return (max(1.0, padding["left"] + padding["right"]), max(1.0, padding["top"] + padding["bottom"]))
    gap = _num(parent.get("gap")) or 0.0
    column_gap = _num(parent.get("column_gap")) or _num(parent.get("columnGap")) or gap
    row_gap = _num(parent.get("row_gap")) or _num(parent.get("rowGap")) or gap
    columns = max(1, int(_num(parent.get("columns")) or len(parent.get("columns") or []) or 1))
    declared_rows_raw = parent.get("rows")
    declared_rows = _num(declared_rows_raw) or (
        len(declared_rows_raw) if isinstance(declared_rows_raw, list) and declared_rows_raw else None
    )
    placements = _place_grid_children(children, columns, int(declared_rows) if declared_rows else None)
    rows = max([int(declared_rows or 1)] + [p["row"] + p["row_span"] for p in placements])

    column_widths = [1.0] * columns
    row_heights = [1.0] * rows
    for child, placement in zip(children, placements):
        w, h = _intrinsic_flow_child_size(child)
        width_share = (w - column_gap * max(0, placement["column_span"] - 1)) / placement["column_span"]
        height_share = (h - row_gap * max(0, placement["row_span"] - 1)) / placement["row_span"]
        for c in range(placement["col"], placement["col"] + placement["column_span"]):
            column_widths[c] = max(column_widths[c], width_share)
        for r in range(placement["row"], placement["row"] + placement["row_span"]):
            row_heights[r] = max(row_heights[r], height_share)

    width = padding["left"] + padding["right"] + sum(max(1.0, w) for w in column_widths) + column_gap * max(0, columns - 1)
    height = padding["top"] + padding["bottom"] + sum(max(1.0, h) for h in row_heights) + row_gap * max(0, rows - 1)
    return (max(1.0, width), max(1.0, height))


def _element_box(child: dict) -> FlowBox:
    position = _record(child.get("position")) or {}
    x, y = _num(position.get("x")) or 0.0, _num(position.get("y")) or 0.0
    w, h = _element_size(child)
    return FlowBox(x, y, w, h)


# -- grid placement (identical to placeGridChildren/gridAreaOpen/markGridArea) -----


def _place_grid_children(
    children: list[dict], columns: int, declared_rows: Optional[int]
) -> list[dict]:
    occupied: set[tuple[int, int]] = set()
    placements: list[dict] = []
    row_limit = max(1, declared_rows or math.ceil(len(children) / columns) if children else 1)

    def area_open(row: int, col: int, row_span: int, column_span: int) -> bool:
        return all(
            (r, c) not in occupied
            for r in range(row, row + row_span)
            for c in range(col, col + column_span)
        )

    for child in children:
        column_span = min(columns, max(1, int(_layout_number(child, "columnSpan", "column_span") or 1)))
        row_span = max(1, int(_layout_number(child, "rowSpan", "row_span") or 1))
        placed_row = placed_col = 0
        while True:
            placed = False
            row = 0
            while row < row_limit and not placed:
                for col in range(0, columns - column_span + 1):
                    if area_open(row, col, row_span, column_span):
                        placed, placed_row, placed_col = True, row, col
                        break
                row += 1
            if placed:
                break
            row_limit += 1
        for r in range(placed_row, placed_row + row_span):
            for c in range(placed_col, placed_col + column_span):
                occupied.add((r, c))
        placements.append(
            {"col": placed_col, "row": placed_row, "column_span": column_span, "row_span": row_span}
        )
    return placements


# -- flexBasis / childCrossSize -----------------------------------------------------


def _flex_basis(child: dict, direction: str, cross_size: float) -> float:
    dimension = "width" if direction == "row" else "height"
    explicit = _layout_number(child, "basis")
    if explicit is None:
        size = _read_optional_size(child.get("size"))
        explicit = (size[0] if dimension == "width" else size[1]) if size else None
    if explicit is not None and explicit > 0:
        return _clamp_layout_size(explicit, child, dimension)

    if _is_frameless_decorative_shape(child):
        return DECORATIVE_LINE_THICKNESS
    if _str(child.get("type")) == "text":
        return _clamp_layout_size(_intrinsic_text_main_size(child, direction, cross_size), child, dimension)

    inferred = _element_size(child)
    size = inferred[0] if direction == "row" else inferred[1]
    return _clamp_layout_size(size, child, dimension) if size > 1 else 0.0


def _child_cross_size(child: dict, direction: str, cross_size: float, align_items: str) -> float:
    dimension = "height" if direction == "row" else "width"
    layout = _record(child.get("layout")) or {}
    align_self = _str(layout.get("align_self")) or _str(layout.get("alignSelf"))
    if _is_frameless_decorative_shape(child):
        return _clamp_layout_size(min(cross_size, DECORATIVE_LINE_LENGTH), child, dimension)
    if align_items == "stretch" and align_self is None:
        return cross_size
    size = _read_optional_size(child.get("size"))
    explicit = (size[1] if dimension == "height" else size[0]) if size else None
    inferred_wh = _element_size(
        child,
        (1.0, cross_size) if direction == "row" else (cross_size, 1.0),
    )
    inferred = inferred_wh[1] if dimension == "height" else inferred_wh[0]
    return _clamp_layout_size(explicit if explicit is not None else (inferred if inferred is not None else cross_size), child, dimension, cross_size)


# -- the two public entry points: flex and grid -------------------------------------


def _layout_flex_children(parent: dict, children: list[dict], parent_box: FlowBox) -> list[Optional[FlowBox]]:
    if not children:
        return []
    padding = _read_padding(parent.get("padding"))
    is_column = _str(parent.get("direction")) != "row"
    direction = "column" if is_column else "row"
    main_gap = (
        (_num(parent.get("row_gap")) or _num(parent.get("rowGap")))
        if is_column
        else (_num(parent.get("column_gap")) or _num(parent.get("columnGap")))
    )
    main_gap = main_gap if main_gap is not None else (_num(parent.get("gap")) or 0.0)
    align = _str(parent.get("align_items")) or _str(parent.get("alignItems")) or "stretch"
    justify = _str(parent.get("justify_content")) or _str(parent.get("justifyContent")) or "flex-start"
    available_w = max(1.0, parent_box.width - padding["left"] - padding["right"])
    available_h = max(1.0, parent_box.height - padding["top"] - padding["bottom"])
    available_main = available_h if is_column else available_w
    available_cross = available_w if is_column else available_h

    if parent.get("wrap") is True:
        cross_gap = (
            (_num(parent.get("column_gap")) or _num(parent.get("columnGap")))
            if is_column
            else (_num(parent.get("row_gap")) or _num(parent.get("rowGap")))
        )
        cross_gap = cross_gap if cross_gap is not None else (_num(parent.get("gap")) or 0.0)
        return _layout_wrapped_flex_children(
            children, direction, align, justify, available_main, available_cross,
            main_gap, cross_gap, padding,
        )

    bases = [
        (_element_box(c).height if is_column else _element_box(c).width)
        if _is_manual_positioned(c)
        else _flex_basis(c, direction, available_cross)
        for c in children
    ]
    gap_total = main_gap * max(0, len(children) - 1)
    free_before_flex = max(1.0, available_main - gap_total) - sum(max(0.0, b) for b in bases)
    main_sizes = [max(0.0, b) for b in bases]
    grows = [
        0.0 if _is_manual_positioned(c) else (_layout_number(c, "grow") or (0.0 if bases[i] > 0 else 1.0))
        for i, c in enumerate(children)
    ]
    grow_total = sum(grows)

    if free_before_flex > 0 and grow_total > 0:
        main_sizes = [s + (free_before_flex * grows[i]) / grow_total for i, s in enumerate(main_sizes)]
    elif free_before_flex > 0 and justify == "stretch":
        flexible_count = max(1, sum(1 for c in children if not _is_manual_positioned(c)))
        main_sizes = [
            s if _is_manual_positioned(children[i]) else s + free_before_flex / flexible_count
            for i, s in enumerate(main_sizes)
        ]

    used_main = sum(main_sizes) + main_gap * max(0, len(children) - 1)
    cursor = _alignment_offset(justify, available_main, used_main)

    boxes: list[Optional[FlowBox]] = []
    for index, child in enumerate(children):
        raw = _element_box(child)
        if _is_manual_positioned(child):
            cursor += (raw.height if is_column else raw.width) + main_gap
            boxes.append(raw)
            continue
        main = _clamp_layout_size(main_sizes[index], child, "height" if is_column else "width")
        cross = _child_cross_size(child, direction, available_cross, align)
        layout = _record(child.get("layout")) or {}
        align_self = _str(layout.get("align_self")) or _str(layout.get("alignSelf"))
        cross_offset = _alignment_offset(align_self or align, available_cross, cross)
        if is_column:
            box = FlowBox(padding["left"] + cross_offset, padding["top"] + cursor, cross, main)
        else:
            box = FlowBox(padding["left"] + cursor, padding["top"] + cross_offset, main, cross)
        cursor += main + main_gap
        boxes.append(box)
    return boxes


def _layout_wrapped_flex_children(
    children: list[dict],
    direction: str,
    align: str,
    justify: str,
    available_main: float,
    available_cross: float,
    main_gap: float,
    cross_gap: float,
    padding: dict,
) -> list[Optional[FlowBox]]:
    is_column = direction == "column"
    lines: list[list[dict]] = []
    manual: dict[int, FlowBox] = {}

    for index, child in enumerate(children):
        if _is_manual_positioned(child):
            manual[index] = _element_box(child)
            continue
        basis = max(0.0, _flex_basis(child, direction, available_cross))
        if not lines:
            lines.append([])
        line = lines[-1]
        used = sum(e["basis"] for e in line) + main_gap * len(line)
        if line and used + basis > available_main:
            line = []
            lines.append(line)
        line.append({"basis": basis, "child": child, "index": index})

    if not lines:
        return [manual.get(i, _element_box(c)) for i, c in enumerate(children)]

    def item_align(child: dict) -> str:
        layout = _record(child.get("layout")) or {}
        return _str(layout.get("align_self")) or _str(layout.get("alignSelf")) or align

    natural_line_crosses = [
        max([1.0] + [_child_cross_size(e["child"], direction, available_cross, item_align(e["child"])) for e in line])
        for line in lines
    ]
    cross_gap_total = cross_gap * max(0, len(lines) - 1)
    used_natural_cross = sum(natural_line_crosses) + cross_gap_total
    extra_cross = max(0.0, available_cross - used_natural_cross)
    line_crosses = [lc + extra_cross / len(lines) for lc in natural_line_crosses]

    laid_out: dict[int, FlowBox] = {}
    cross_cursor = 0.0
    for line, line_cross in zip(lines, line_crosses):
        gap_total = main_gap * max(0, len(line) - 1)
        free = available_main - gap_total - sum(e["basis"] for e in line)
        main_sizes = [e["basis"] for e in line]
        grows = [_layout_number(e["child"], "grow") or (0.0 if e["basis"] > 0 else 1.0) for e in line]
        grow_total = sum(grows)
        if free > 0 and grow_total > 0:
            main_sizes = [s + (free * grows[i]) / grow_total for i, s in enumerate(main_sizes)]
        elif free > 0 and justify == "stretch":
            main_sizes = [s + free / len(line) for s in main_sizes]

        used_main = sum(main_sizes) + gap_total
        main_cursor = _alignment_offset(justify, available_main, used_main)
        for entry, main_size in zip(line, main_sizes):
            main = _clamp_layout_size(main_size, entry["child"], "height" if is_column else "width")
            alignment = item_align(entry["child"])
            cross = _child_cross_size(entry["child"], direction, line_cross, alignment)
            cross_offset = _alignment_offset(alignment, line_cross, cross)
            if is_column:
                box = FlowBox(
                    padding["left"] + cross_cursor + cross_offset, padding["top"] + main_cursor, cross, main
                )
            else:
                box = FlowBox(
                    padding["left"] + main_cursor, padding["top"] + cross_cursor + cross_offset, main, cross
                )
            laid_out[entry["index"]] = box
            main_cursor += main + main_gap
        cross_cursor += line_cross + cross_gap

    return [manual.get(i) or laid_out.get(i) or _element_box(c) for i, c in enumerate(children)]


def _layout_grid_children(parent: dict, children: list[dict], parent_box: FlowBox) -> list[Optional[FlowBox]]:
    padding = _read_padding(parent.get("padding"))
    gap = _num(parent.get("gap")) or 0.0
    column_gap = _num(parent.get("column_gap")) or _num(parent.get("columnGap")) or gap
    row_gap = _num(parent.get("row_gap")) or _num(parent.get("rowGap")) or gap
    columns_raw = parent.get("columns")
    rows_raw = parent.get("rows")
    column_count = _num(columns_raw) or (len(columns_raw) if isinstance(columns_raw, list) and columns_raw else 1)
    columns = max(1, int(column_count))
    declared_rows = _num(rows_raw)
    if declared_rows is None and isinstance(rows_raw, list) and rows_raw:
        declared_rows = float(len(rows_raw))
    placements = _place_grid_children(children, columns, int(declared_rows) if declared_rows else None)
    rows = max([int(declared_rows or 1)] + [p["row"] + p["row_span"] for p in placements])
    row_sizing_count = max(1, int(declared_rows) if declared_rows else rows)

    available_w = max(1.0, parent_box.width - padding["left"] - padding["right"])
    available_h = max(1.0, parent_box.height - padding["top"] - padding["bottom"])
    cell_w = max(1.0, (available_w - column_gap * (columns - 1)) / columns)
    cell_h = max(1.0, (available_h - row_gap * max(0, row_sizing_count - 1)) / row_sizing_count)

    boxes: list[Optional[FlowBox]] = []
    for child, placement in zip(children, placements):
        if _is_manual_positioned(child):
            boxes.append(_element_box(child))
            continue
        area_x = padding["left"] + placement["col"] * (cell_w + column_gap)
        area_y = padding["top"] + placement["row"] * (cell_h + row_gap)
        area_w = cell_w * placement["column_span"] + column_gap * (placement["column_span"] - 1)
        area_h = cell_h * placement["row_span"] + row_gap * (placement["row_span"] - 1)

        layout = _record(child.get("layout")) or {}
        justify = (
            _str(layout.get("align_self")) or _str(layout.get("alignSelf"))
            or _str(parent.get("justify_items")) or _str(parent.get("justifyItems")) or "stretch"
        )
        align = (
            _str(layout.get("align_self")) or _str(layout.get("alignSelf"))
            or _str(parent.get("align_items")) or _str(parent.get("alignItems")) or "stretch"
        )
        raw = _element_box(child)
        width = area_w if justify == "stretch" else _clamp_layout_size(raw.width, child, "width", area_w)
        height = area_h if align == "stretch" else _clamp_layout_size(raw.height, child, "height", area_h)
        box = FlowBox(
            area_x + _alignment_offset(justify, area_w, width),
            area_y + _alignment_offset(align, area_h, height),
            width,
            height,
        )
        boxes.append(box)
    return boxes


def layout_flow_children(parent: dict, children: list[dict], parent_box: FlowBox) -> list[Optional[FlowBox]]:
    """
    Pixel box (in the parent's local coordinate space) for each child of a
    flex/grid/list-view/grid-view container. Every entry is a box -- faithful
    to the JS original, which always produces *some* layout rather than
    refusing (a child it can't size exactly just collapses toward 0px, the
    same as the real renderer).
    """
    kind = flow_layout_kind(parent)
    if kind == "grid":
        return _layout_grid_children(parent, children, parent_box)
    if kind == "flex":
        return _layout_flex_children(parent, children, parent_box)
    return [None for _ in children]

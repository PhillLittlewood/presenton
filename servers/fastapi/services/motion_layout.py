"""
Locates slide images that carry a motion clip and works out where/how the
clip must be composited over the rasterized slide during video export.

The math intentionally mirrors the Konva editor (components/slide-editor in
the Next.js app):
- absolute placement follows model.ts `absoluteElementBox` /
  `layoutContainerChildren` (component origin + nested container layout),
- fit / focus / crop_scale follow the image node in surface/nodes.tsx,
- `container` elements clip their children, so the visible rect is the image
  box intersected with every container ancestor.

Only deterministic cases are composited. Images that sit under flex/grid
layout parents (positions are computed by the editor's flow-layout engine at
render time), that are rotated, or that use a `clip_path` are reported as
skipped so the export falls back to the static image for them.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

STAGE_WIDTH = 1280.0
STAGE_HEIGHT = 720.0

_FLOW_TYPES = {"flex", "list-view", "grid", "grid-view"}
_ROTATION_EPSILON = 0.01


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def intersect(self, other: "Box") -> Optional["Box"]:
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right - left <= 0.5 or bottom - top <= 0.5:
            return None
        return Box(left, top, right - left, bottom - top)


@dataclass
class ImageFitMapping:
    """How the (clip) source maps into the image element's local box."""

    # Source crop rectangle in source pixels (None = whole source).
    crop: Optional[tuple[float, float, float, float]]
    # Destination rectangle inside the element box (local coordinates).
    draw: Box


@dataclass
class MotionPlacement:
    motion_video: str
    box: Box  # absolute, stage pixels (1280x720)
    visible: Box  # box ∩ container ancestors (absolute)
    fit: str
    focus_x: float
    focus_y: float
    crop_scale: float
    flip_h: bool
    flip_v: bool
    radii: tuple[float, float, float, float]
    opacity: float
    element_name: str = ""


@dataclass
class MotionScan:
    placements: list[MotionPlacement] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# -- small readers ---------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _point(value: Any) -> tuple[float, float]:
    if isinstance(value, dict):
        return (_num(value.get("x")) or 0.0, _num(value.get("y")) or 0.0)
    return (0.0, 0.0)


def _explicit_size(value: Any) -> Optional[tuple[float, float]]:
    if isinstance(value, dict):
        w, h = _num(value.get("width")), _num(value.get("height"))
        if w is not None and h is not None:
            return (w, h)
    return None


def _padding(value: Any) -> dict[str, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return {"top": v, "right": v, "bottom": v, "left": v}
    rec = value if isinstance(value, dict) else {}
    x = _num(rec.get("x"))
    if x is None:
        x = _num(rec.get("horizontal"))
    y = _num(rec.get("y"))
    if y is None:
        y = _num(rec.get("vertical"))
    return {
        "top": _first(_num(rec.get("top")), y, 0.0),
        "right": _first(_num(rec.get("right")), x, 0.0),
        "bottom": _first(_num(rec.get("bottom")), y, 0.0),
        "left": _first(_num(rec.get("left")), x, 0.0),
    }


def _first(*values: Optional[float]) -> float:
    for v in values:
        if v is not None:
            return v
    return 0.0


def _child_items(element: dict) -> list[dict]:
    if isinstance(element.get("children"), list):
        items = element["children"]
    elif isinstance(element.get("elements"), list):
        items = element["elements"]
    elif isinstance(element.get("child"), dict):
        items = [element["child"]]
    else:
        items = []
    return [i for i in items if isinstance(i, dict)]


def _element_size(element: dict, fallback: Optional[tuple[float, float]]) -> tuple[float, float]:
    explicit = _explicit_size(element.get("size"))
    if explicit:
        return explicit
    return fallback or (1.0, 1.0)


def _local_box(element: dict, fallback: Optional[tuple[float, float]] = None) -> Box:
    x, y = _point(element.get("position"))
    w, h = _element_size(element, fallback)
    return Box(x, y, w, h)


def _alignment_offset(kind: str, container: float, size: float) -> float:
    if kind == "center":
        return (container - size) / 2
    if kind == "right" or kind == "bottom":
        return container - size
    return 0.0


def _container_child_box(parent: dict, child: dict, parent_size: tuple[float, float]) -> Box:
    """Port of layoutContainerChildren for a single non-group child."""
    if child.get("__presenton_manual_position") is True:
        return _local_box(child)
    pad = _padding(parent.get("padding"))
    content_w = max(1.0, parent_size[0] - pad["left"] - pad["right"])
    content_h = max(1.0, parent_size[1] - pad["top"] - pad["bottom"])
    px, py = _point(child.get("position"))
    explicit = _explicit_size(child.get("size"))
    if child.get("type") == "group" and explicit is None:
        width, height = content_w, content_h
    else:
        width, height = _element_size(child, (content_w, content_h))
    if child.get("type") == "group":
        return Box(pad["left"] + px, pad["top"] + py, width, height)

    alignment = parent.get("alignment") if isinstance(parent.get("alignment"), dict) else {}
    horizontal = alignment.get("horizontal") or "left"
    vertical = alignment.get("vertical") or "top"
    if horizontal in ("center", "right"):
        x = pad["left"] + _alignment_offset(horizontal, content_w, width)
    else:
        x = pad["left"] + px
    if vertical in ("middle", "bottom"):
        y = pad["top"] + _alignment_offset("center" if vertical == "middle" else "bottom", content_h, height)
    else:
        y = pad["top"] + py
    return Box(x, y, width, height)


def _radii(element: dict, width: float, height: float) -> tuple[float, float, float, float]:
    value = element.get("border_radius", element.get("borderRadius"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        vals = [float(value)] * 4
    elif isinstance(value, dict):
        radius = _num(value.get("radius"))
        if radius is not None:
            vals = [radius] * 4
        else:
            tl = _first(_num(value.get("tl")), _num(value.get("topLeft")), 0.0)
            tr = _first(_num(value.get("tr")), _num(value.get("topRight")), tl)
            br = _first(_num(value.get("br")), _num(value.get("bottomRight")), tr)
            bl = _first(_num(value.get("bl")), _num(value.get("bottomLeft")), br)
            vals = [tl, tr, br, bl]
    else:
        vals = [0.0] * 4
    max_r = max(0.0, min(width, height) / 2)
    return tuple(min(max(v, 0.0), max_r) for v in vals)  # type: ignore[return-value]


def _has_clip_path(element: dict) -> bool:
    raw = element.get("clippath") or element.get("clipPath") or element.get("clip_path")
    return isinstance(raw, str) and raw.strip() != "" and raw.strip().lower() != "none"


def _rotated(element: dict) -> bool:
    rotation = _num(element.get("rotation"))
    return rotation is not None and abs(rotation) > _ROTATION_EPSILON


# -- scan ------------------------------------------------------------------------


def find_motion_placements(ui: Any) -> MotionScan:
    """Scan a slide's Template V2 `ui` for images with a `motion_video` sidecar."""
    scan = MotionScan()
    if not isinstance(ui, dict):
        return scan

    root = [e for e in (ui.get("elements") or []) if isinstance(e, dict)]
    _walk(root, None, (STAGE_WIDTH, STAGE_HEIGHT), (0.0, 0.0), [], False, scan)

    for component in ui.get("components") or []:
        if not isinstance(component, dict):
            continue
        origin = _point(component.get("position"))
        elements = [e for e in (component.get("elements") or []) if isinstance(e, dict)]
        _walk(elements, None, (STAGE_WIDTH, STAGE_HEIGHT), origin, [], False, scan)
    return scan


def _walk(
    elements: list[dict],
    parent: Optional[dict],
    parent_size: tuple[float, float],
    origin: tuple[float, float],
    clips: list[Box],
    ancestor_rotated: bool,
    scan: MotionScan,
) -> None:
    for element in elements:
        etype = element.get("type")
        has_motion = etype == "image" and bool(element.get("motion_video"))

        if parent is not None and parent.get("type") in _FLOW_TYPES:
            # Flow-layout positions are computed by the editor at render time.
            _skip_subtree(element, "is inside a flex/grid layout", scan)
            continue

        local = (
            _container_child_box(parent, element, parent_size)
            if parent is not None and parent.get("type") == "container"
            else _local_box(element)
        )
        absolute = Box(origin[0] + local.x, origin[1] + local.y, local.width, local.height)
        rotated = ancestor_rotated or _rotated(element)

        if has_motion:
            _maybe_add(element, absolute, clips, rotated, scan)

        children = _child_items(element)
        if children:
            child_clips = clips + [absolute] if etype == "container" else clips
            _walk(
                children,
                element,
                (absolute.width, absolute.height),
                (absolute.x, absolute.y),
                child_clips,
                rotated,
                scan,
            )


def _skip_subtree(element: dict, reason: str, scan: MotionScan) -> None:
    if element.get("type") == "image" and element.get("motion_video"):
        scan.skipped.append(f"{element.get('name') or 'image'}: {reason}")
    for child in _child_items(element):
        _skip_subtree(child, reason, scan)


def _maybe_add(
    element: dict, box: Box, clips: list[Box], rotated: bool, scan: MotionScan
) -> None:
    name = str(element.get("name") or "image")
    if rotated:
        scan.skipped.append(f"{name}: rotated image")
        return
    if _has_clip_path(element):
        scan.skipped.append(f"{name}: uses a clip path")
        return

    visible: Optional[Box] = box
    for clip in clips:
        visible = visible.intersect(clip) if visible else None
    stage = Box(0, 0, STAGE_WIDTH, STAGE_HEIGHT)
    visible = visible.intersect(stage) if visible else None
    if visible is None or box.width <= 0 or box.height <= 0:
        scan.skipped.append(f"{name}: not visible on the slide")
        return

    fit = element.get("fit") if element.get("fit") in ("contain", "cover", "fill") else "contain"
    opacity = _num(element.get("opacity"))
    scan.placements.append(
        MotionPlacement(
            motion_video=str(element["motion_video"]),
            box=box,
            visible=visible,
            fit=fit,
            focus_x=min(max(_first(_num(element.get("focus_x")), 50.0), 0.0), 100.0) / 100.0,
            focus_y=min(max(_first(_num(element.get("focus_y")), 50.0), 0.0), 100.0) / 100.0,
            crop_scale=min(max(_first(_num(element.get("crop_scale")), 1.0), 0.1), 6.0),
            flip_h=element.get("flip_h") is True,
            flip_v=element.get("flip_v") is True,
            radii=_radii(element, box.width, box.height),
            opacity=1.0 if opacity is None else min(max(opacity, 0.0), 1.0),
            element_name=name,
        )
    )


# -- fit mapping (mirrors ImageNode in nodes.tsx) ----------------------------------


def compute_fit_mapping(
    fit: str,
    focus_x: float,
    focus_y: float,
    crop_scale: float,
    box_width: float,
    box_height: float,
    src_width: float,
    src_height: float,
) -> ImageFitMapping:
    natural = (src_width / src_height) if src_height else 1.0
    box_ratio = (box_width / box_height) if box_height else 1.0
    draw_w, draw_h = box_width, box_height
    off_x = off_y = 0.0
    crop: Optional[tuple[float, float, float, float]] = None

    if fit == "cover":
        if crop_scale < 1:
            if natural > box_ratio:
                draw_w = box_height * natural * crop_scale
                draw_h = box_height * crop_scale
            else:
                draw_w = box_width * crop_scale
                draw_h = (box_width / natural) * crop_scale
            off_x = (box_width - draw_w) * focus_x if draw_w <= box_width else -(draw_w - box_width) * focus_x
            off_y = (box_height - draw_h) * focus_y if draw_h <= box_height else -(draw_h - box_height) * focus_y
        elif natural > box_ratio:
            base_crop_w = src_height * box_ratio
            crop_w = min(src_width, base_crop_w / crop_scale)
            crop_h = min(src_height, src_height / crop_scale)
            crop = (max(0.0, (src_width - crop_w) * focus_x), max(0.0, (src_height - crop_h) * focus_y), crop_w, crop_h)
        else:
            base_crop_h = src_width / box_ratio
            crop_w = min(src_width, src_width / crop_scale)
            crop_h = min(src_height, base_crop_h / crop_scale)
            crop = (max(0.0, (src_width - crop_w) * focus_x), max(0.0, (src_height - crop_h) * focus_y), crop_w, crop_h)
    elif fit == "contain":
        if natural > box_ratio:
            draw_h = box_width / natural
            off_y = (box_height - draw_h) * focus_y
        else:
            draw_w = box_height * natural
            off_x = (box_width - draw_w) * focus_x

    return ImageFitMapping(crop=crop, draw=Box(off_x, off_y, draw_w, draw_h))

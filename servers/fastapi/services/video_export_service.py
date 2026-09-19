"""
Turns a presentation's slides + speaker notes into a narrated MP4.

Pipeline:
1. Export the presentation to PDF using the existing export pipeline
   (utils.export_utils.export_presentation) -- this reuses the same
   headless-Chromium rendering already used for PDF/PPTX export, so slide
   visuals stay identical to what the user sees in the editor.
2. Rasterize each PDF page to a PNG (PyMuPDF) -- one image per slide.
3. For each slide, generate narration audio from its speaker_note via
   NarrationService (ComfyUI TTS). Slides with no speaker note get a short
   silent clip instead of being skipped, so slide timing stays even.
4. Build one video segment per slide (image + its narration audio, looped
   image, matched duration), then concatenate all segments with ffmpeg's
   concat demuxer into the final MP4.

5. Slides whose image elements carry an AI-generated motion clip (see
   services/motion_video_service.py) get their segment composited instead of
   a flat loop: the clip plays over the rasterized slide at the image's
   position for the narration's duration (fading out to the static image if
   it is shorter, truncated if it is longer). Slides without a motion clip use
   the flat-loop path unchanged.

Requires the `ffmpeg` and `ffprobe` binaries on PATH (or FFMPEG_BINARY /
FFPROBE_BINARY env vars pointing at them).
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from services.motion_layout import (
    Box,
    MotionPlacement,
    STAGE_HEIGHT,
    STAGE_WIDTH,
    compute_fit_mapping,
    find_motion_placements,
)
from services.narration_service import NARRATION_SERVICE, NarrationGenerationError
from utils.asset_directory_utils import get_videos_directory
from utils.get_env import get_app_data_directory_env
from utils.video_motion_provider import is_motion_video_disabled
from utils.export_utils import export_presentation
from utils.filename_utils import safe_export_basename
from utils.video_narration_provider import (
    is_comfyui_narration_selected,
    is_video_narration_disabled,
)
from utils.get_env import (
    get_ffmpeg_binary_env,
    get_ffprobe_binary_env,
    get_video_narration_max_concurrency_env,
)
from pathvalidate import sanitize_filename

LOGGER = logging.getLogger(__name__)

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30
SILENT_SLIDE_DURATION_SECONDS = 2.5
END_PAD_SECONDS = 0.4
AUDIO_SAMPLE_RATE = 44100
MOTION_FADE_SECONDS = 0.5

# Shared encode settings so every per-slide segment is byte-compatible for
# concat-demuxer "-c copy" (same codec/pix_fmt/framerate/sample rate).
_VIDEO_ENCODE_ARGS = [
    "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
    "-r", str(VIDEO_FPS),
]
_AUDIO_ENCODE_ARGS = [
    "-c:a", "aac", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "2",
]


# Same codec/pix_fmt/framerate/sample rate as _VIDEO_ENCODE_ARGS (so segments
# stay concat-compatible) without the still-image tune, which is wrong for
# segments that contain real motion.
_MOTION_VIDEO_ENCODE_ARGS = [
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(VIDEO_FPS),
]


class VideoExportError(Exception):
    pass


class _MotionOverlay:
    """A motion clip resolved to a file + where it sits on the slide."""

    def __init__(
        self,
        placement: MotionPlacement,
        clip_path: str,
        width: int,
        height: int,
        duration: float,
    ) -> None:
        self.placement = placement
        self.clip_path = clip_path
        self.width = width
        self.height = height
        self.duration = duration


class SlideProgressCallback:
    """Optional callback the caller can supply to report progress."""

    async def __call__(self, completed: int, total: int, message: str) -> None:  # pragma: no cover
        raise NotImplementedError


class VideoExportService:
    def __init__(self) -> None:
        self.ffmpeg = get_ffmpeg_binary_env()
        self.ffprobe = get_ffprobe_binary_env()

    async def export_presentation_video(
        self,
        presentation_id: uuid.UUID,
        sql_session: AsyncSession,
        on_progress: Optional[SlideProgressCallback] = None,
        cookie_header: Optional[str] = None,
    ) -> str:
        presentation = await sql_session.get(PresentationModel, presentation_id)
        if not presentation:
            raise HTTPException(status_code=404, detail="Presentation not found")

        slides_result = await sql_session.scalars(
            select(SlideModel)
            .where(SlideModel.presentation == presentation_id)
            .order_by(SlideModel.index)
        )
        slides = list(slides_result)
        if not slides:
            raise HTTPException(
                status_code=400, detail="Presentation has no slides to export"
            )

        title = (presentation.title or "").strip() or str(presentation_id)
        safe_title = safe_export_basename(sanitize_filename(title))

        work_dir = tempfile.mkdtemp(prefix="presenton-video-")
        try:
            await self._report(on_progress, 0, len(slides), "Exporting slides to PDF")
            pdf_path = await self._export_pdf(presentation_id, title, cookie_header)

            await self._report(on_progress, 0, len(slides), "Rendering slide images")
            image_paths = self._rasterize_pdf(pdf_path, work_dir)
            if len(image_paths) != len(slides):
                raise VideoExportError(
                    f"Rendered {len(image_paths)} slide images but presentation "
                    f"has {len(slides)} slides; export may be out of sync."
                )

            narration_paths = await self._generate_all_narrations(
                slides, work_dir, on_progress
            )
            motion_overlays = await self._collect_motion_overlays(slides)

            await self._report(
                on_progress, len(slides), len(slides), "Assembling video"
            )
            segment_paths = await self._build_segments(
                image_paths, narration_paths, work_dir, motion_overlays
            )
            final_path = await self._concat_segments(segment_paths, work_dir)

            output_directory = get_videos_directory()
            output_path = os.path.join(
                output_directory, f"{safe_title}-{uuid.uuid4().hex[:8]}.mp4"
            )
            shutil.move(final_path, output_path)

            await self._report(
                on_progress, len(slides), len(slides), "Video export complete"
            )
            return output_path
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # -- step 1: PDF export --------------------------------------------------

    async def _export_pdf(
        self,
        presentation_id: uuid.UUID,
        title: str,
        cookie_header: Optional[str],
    ) -> str:
        result = await export_presentation(
            presentation_id=presentation_id,
            title=title,
            export_as="pdf",
            cookie_header=cookie_header,
        )
        return result.path

    # -- step 2: rasterize PDF pages ------------------------------------------

    def _rasterize_pdf(self, pdf_path: str, work_dir: str) -> list[str]:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise VideoExportError(
                "PyMuPDF (pymupdf) is required to rasterize slides for video "
                "export. Install it with `uv add pymupdf` in servers/fastapi."
            ) from exc

        images_dir = os.path.join(work_dir, "slides")
        os.makedirs(images_dir, exist_ok=True)

        image_paths: list[str] = []
        doc = fitz.open(pdf_path)
        try:
            for page_index, page in enumerate(doc):
                zoom_x = VIDEO_WIDTH / page.rect.width
                zoom_y = VIDEO_HEIGHT / page.rect.height
                zoom = min(zoom_x, zoom_y)
                matrix = fitz.Matrix(zoom, zoom)
                pixmap = page.get_pixmap(matrix=matrix)
                image_path = os.path.join(images_dir, f"slide_{page_index:04d}.png")
                pixmap.save(image_path)
                image_paths.append(image_path)
        finally:
            doc.close()

        return image_paths

    # -- step 3: narration -----------------------------------------------------

    async def _generate_all_narrations(
        self,
        slides: list[SlideModel],
        work_dir: str,
        on_progress: Optional[SlideProgressCallback],
    ) -> list[Optional[str]]:
        audio_dir = os.path.join(work_dir, "audio")
        os.makedirs(audio_dir, exist_ok=True)

        semaphore = asyncio.Semaphore(get_video_narration_max_concurrency_env())
        results: list[Optional[str]] = [None] * len(slides)
        completed = 0
        lock = asyncio.Lock()
        narration_disabled = is_video_narration_disabled() or not is_comfyui_narration_selected()

        async def worker(index: int, slide: SlideModel) -> None:
            nonlocal completed
            note = (slide.speaker_note or "").strip()
            async with semaphore:
                if note and not narration_disabled:
                    try:
                        path = await NARRATION_SERVICE.generate_narration_comfyui(
                            note, audio_dir
                        )
                        results[index] = path
                    except NarrationGenerationError as exc:
                        LOGGER.warning(
                            "Narration failed for slide %s, falling back to "
                            "silence: %s",
                            index,
                            exc,
                        )
                        results[index] = None
                else:
                    results[index] = None
            async with lock:
                completed += 1
                step_message = (
                    f"Rendering silent slide {completed}/{len(slides)} "
                    "(narration disabled)"
                    if narration_disabled
                    else f"Generated narration for slide {completed}/{len(slides)}"
                )
                await self._report(on_progress, completed, len(slides), step_message)

        await asyncio.gather(
            *(worker(i, slide) for i, slide in enumerate(slides))
        )
        return results

    # -- step 4: per-slide video segments ---------------------------------------

    async def _build_segments(
        self,
        image_paths: list[str],
        narration_paths: list[Optional[str]],
        work_dir: str,
        motion_overlays: Optional[list[list[_MotionOverlay]]] = None,
    ) -> list[str]:
        segments_dir = os.path.join(work_dir, "segments")
        os.makedirs(segments_dir, exist_ok=True)

        segment_paths = []
        for i, (image_path, audio_path) in enumerate(
            zip(image_paths, narration_paths)
        ):
            segment_path = os.path.join(segments_dir, f"segment_{i:04d}.mp4")
            overlays = motion_overlays[i] if motion_overlays else []
            if overlays:
                try:
                    await self._build_motion_segment(
                        image_path, audio_path, overlays, work_dir, i, segment_path
                    )
                    segment_paths.append(segment_path)
                    continue
                except Exception as exc:
                    # Same resilience as narration: log and fall back to the
                    # plain static-image segment for this slide.
                    LOGGER.warning(
                        "Motion compositing failed for slide %s, falling back "
                        "to the static image: %s",
                        i,
                        exc,
                    )
            if audio_path:
                await self._build_narrated_segment(
                    image_path, audio_path, segment_path
                )
            else:
                await self._build_silent_segment(image_path, segment_path)
            segment_paths.append(segment_path)
        return segment_paths

    async def _build_narrated_segment(
        self, image_path: str, audio_path: str, output_path: str
    ) -> None:
        command = [
            self.ffmpeg, "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-filter_complex", f"[1:a]apad=pad_dur={END_PAD_SECONDS}[a]",
            "-map", "0:v", "-map", "[a]",
            *_VIDEO_ENCODE_ARGS,
            *_AUDIO_ENCODE_ARGS,
            "-shortest",
            output_path,
        ]
        await self._run(command)

    async def _build_silent_segment(self, image_path: str, output_path: str) -> None:
        command = [
            self.ffmpeg, "-y",
            "-loop", "1", "-i", image_path,
            "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo",
            *_VIDEO_ENCODE_ARGS,
            *_AUDIO_ENCODE_ARGS,
            "-t", str(SILENT_SLIDE_DURATION_SECONDS),
            output_path,
        ]
        await self._run(command)

    # -- motion clips (only used by slides that carry one) ------------------------

    async def _collect_motion_overlays(
        self, slides: list[SlideModel]
    ) -> list[list[_MotionOverlay]]:
        """Resolve each slide's motion clips; never raises (falls back to static)."""
        results: list[list[_MotionOverlay]] = [[] for _ in slides]
        if is_motion_video_disabled():
            return results

        for index, slide in enumerate(slides):
            try:
                scan = find_motion_placements(slide.ui)
                for reason in scan.skipped:
                    LOGGER.warning(
                        "Motion clip on slide %s not composited (%s); using the "
                        "static image",
                        index,
                        reason,
                    )
                for placement in scan.placements:
                    clip_path = self._resolve_motion_clip_file(
                        placement.motion_video, slide.owner_id
                    )
                    if not clip_path:
                        LOGGER.warning(
                            "Motion clip for slide %s not found on disk: %s",
                            index,
                            placement.motion_video,
                        )
                        continue
                    width, height, duration = await self._probe_video(clip_path)
                    results[index].append(
                        _MotionOverlay(placement, clip_path, width, height, duration)
                    )
            except Exception as exc:
                LOGGER.warning(
                    "Could not prepare motion clips for slide %s: %s", index, exc
                )
                results[index] = []
        return results

    @staticmethod
    def _resolve_motion_clip_file(
        motion_video: str, owner_id: Optional[uuid.UUID]
    ) -> Optional[str]:
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(motion_video).path)
        if not path.startswith("/app_data/motion/"):
            return None
        app_data = get_app_data_directory_env()
        if not app_data:
            return None
        motion_root = os.path.realpath(os.path.join(app_data, "motion"))
        candidate = os.path.realpath(os.path.join(app_data, path[len("/app_data/"):]))
        allowed_root = (
            os.path.join(motion_root, "users", str(owner_id))
            if owner_id is not None
            else motion_root
        )
        try:
            if os.path.commonpath([candidate, allowed_root]) != allowed_root:
                return None
        except ValueError:
            return None
        return candidate if os.path.isfile(candidate) else None

    async def _probe_video(self, path: str) -> tuple[int, int, float]:
        stdout = await self._capture(
            [
                self.ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", path,
            ]
        )
        data = json.loads(stdout)
        stream = (data.get("streams") or [{}])[0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        duration = float((data.get("format") or {}).get("duration") or 0)
        if width <= 0 or height <= 0 or duration <= 0:
            raise VideoExportError(f"Could not probe motion clip: {path}")
        return width, height, duration

    async def _probe_audio_duration(self, path: str) -> float:
        stdout = await self._capture(
            [
                self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "json", path,
            ]
        )
        duration = float((json.loads(stdout).get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise VideoExportError(f"Could not probe audio duration: {path}")
        return duration

    async def _build_motion_segment(
        self,
        image_path: str,
        audio_path: Optional[str],
        overlays: list[_MotionOverlay],
        work_dir: str,
        index: int,
        output_path: str,
    ) -> None:
        from PIL import Image

        with Image.open(image_path) as base:
            base_w, base_h = base.size
        scale_x, scale_y = base_w / STAGE_WIDTH, base_h / STAGE_HEIGHT

        # Segment length: slides advance on the narration audio (clips longer
        # than it are truncated). With no narration there is no audio to follow,
        # so the clip is allowed to run out.
        if audio_path:
            total = await self._probe_audio_duration(audio_path) + END_PAD_SECONDS
        else:
            total = max(
                SILENT_SLIDE_DURATION_SECONDS, *(o.duration for o in overlays)
            )

        inputs: list[str] = ["-loop", "1", "-framerate", str(VIDEO_FPS), "-i", image_path]
        if audio_path:
            inputs += ["-i", audio_path]
        else:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo"]

        filters: list[str] = []
        last = "0:v"
        input_index = 2
        for n, overlay in enumerate(overlays):
            spec = self._overlay_geometry(overlay, scale_x, scale_y)
            mask_path = os.path.join(work_dir, f"motion_mask_{index:04d}_{n}.png")
            self._write_overlay_mask(spec, mask_path)

            clip_idx, mask_idx = input_index, input_index + 1
            input_index += 2
            inputs += [
                "-i", overlay.clip_path,
                "-loop", "1", "-framerate", str(VIDEO_FPS), "-i", mask_path,
            ]

            chain = [f"[{clip_idx}:v]fps={VIDEO_FPS},setpts=PTS-STARTPTS"]
            if spec["crop"]:
                cx, cy, cw, ch = spec["crop"]
                chain.append(f"crop={cw}:{ch}:{cx}:{cy}")
            chain.append(f"scale={spec['draw_w']}:{spec['draw_h']}:flags=lanczos")
            if overlay.placement.flip_h:
                chain.append("hflip")
            if overlay.placement.flip_v:
                chain.append("vflip")
            filters.append(",".join(chain) + f"[clip{n}]")

            vw, vh = spec["visible_w"], spec["visible_h"]
            filters.append(
                f"color=c=black:s={vw}x{vh}:r={VIDEO_FPS}:d={total:.3f}[canvas{n}]"
            )
            filters.append(
                f"[canvas{n}][clip{n}]overlay=x={spec['draw_x']}:y={spec['draw_y']}"
                f":eof_action=pass:format=auto,format=rgba[painted{n}]"
            )
            filters.append(f"[{mask_idx}:v]format=gray[mask{n}]")
            layer = f"layer{n}"
            filters.append(f"[painted{n}][mask{n}]alphamerge[{layer}]")

            if overlay.duration < total - 0.05:
                fade = min(MOTION_FADE_SECONDS, overlay.duration)
                filters.append(
                    f"[{layer}]fade=t=out:st={overlay.duration - fade:.3f}"
                    f":d={fade:.3f}:alpha=1[{layer}f]"
                )
                layer = f"{layer}f"

            out_label = f"v{n}"
            filters.append(
                f"[{last}][{layer}]overlay=x={spec['visible_x']}:y={spec['visible_y']}"
                f":eof_action=pass:format=auto[{out_label}]"
            )
            last = out_label

        filters.append(f"[{last}]format=yuv420p[vout]")
        if audio_path:
            filters.append(f"[1:a]apad=pad_dur={END_PAD_SECONDS}[aout]")
            audio_map = "[aout]"
        else:
            audio_map = "1:a"

        command = [
            self.ffmpeg, "-y",
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", audio_map,
            *_MOTION_VIDEO_ENCODE_ARGS,
            *_AUDIO_ENCODE_ARGS,
            "-t", f"{total:.3f}",
            output_path,
        ]
        await self._run(command)

    @staticmethod
    def _even(value: float) -> int:
        rounded = int(round(value))
        rounded = max(2, rounded)
        return rounded + (rounded % 2)

    def _overlay_geometry(
        self, overlay: _MotionOverlay, scale_x: float, scale_y: float
    ) -> dict:
        """Pixel geometry (in the rasterized slide's space) for one clip."""
        p = overlay.placement
        box = Box(
            p.box.x * scale_x, p.box.y * scale_y,
            p.box.width * scale_x, p.box.height * scale_y,
        )
        visible = Box(
            p.visible.x * scale_x, p.visible.y * scale_y,
            p.visible.width * scale_x, p.visible.height * scale_y,
        )
        mapping = compute_fit_mapping(
            p.fit, p.focus_x, p.focus_y, p.crop_scale,
            box.width, box.height, overlay.width, overlay.height,
        )
        draw = mapping.draw
        draw_x = draw.x
        draw_y = draw.y
        if p.flip_h:
            draw_x = box.width - draw.x - draw.width
        if p.flip_v:
            draw_y = box.height - draw.y - draw.height

        crop = None
        if mapping.crop:
            cx, cy, cw, ch = mapping.crop
            crop = (
                int(round(cx)), int(round(cy)),
                max(2, int(round(cw))), max(2, int(round(ch))),
            )
        vx, vy = int(round(visible.x)), int(round(visible.y))
        return {
            "crop": crop,
            "draw_w": max(2, int(round(draw.width))),
            "draw_h": max(2, int(round(draw.height))),
            # Position of the drawn clip inside the visible-rect canvas.
            "draw_x": int(round(box.x + draw_x - vx)),
            "draw_y": int(round(box.y + draw_y - vy)),
            "visible_x": vx,
            "visible_y": vy,
            "visible_w": self._even(visible.width),
            "visible_h": self._even(visible.height),
            "box": box,
            "radii": tuple(r * scale_x for r in p.radii),
            "opacity": p.opacity,
            "draw_rect": (
                box.x + draw_x, box.y + draw_y, draw.width, draw.height
            ),
        }

    @staticmethod
    def _write_overlay_mask(spec: dict, mask_path: str) -> None:
        """
        Alpha mask for the visible-rect canvas: the image box (with the
        element's rounded corners) ∩ the area the fitted clip actually covers
        (matters for `contain` letterboxing) ∩ container clipping, scaled by
        the element's opacity.
        """
        from PIL import Image, ImageChops, ImageDraw

        vw, vh = spec["visible_w"], spec["visible_h"]
        vx, vy = spec["visible_x"], spec["visible_y"]
        ss = 2  # supersample for smooth corners

        box: Box = spec["box"]
        bx, by = (box.x - vx) * ss, (box.y - vy) * ss
        bw, bh = box.width * ss, box.height * ss
        tl, tr, br, bl = (r * ss for r in spec["radii"])

        rounded = Image.new("L", (vw * ss, vh * ss), 0)
        d = ImageDraw.Draw(rounded)
        d.rectangle([bx, by, bx + bw - 1, by + bh - 1], fill=255)
        # Cut each rounded corner's square, then add back the quarter circle.
        for r, (x0, y0) in (
            (tl, (bx, by)),
            (tr, (bx + bw - tr, by)),
            (br, (bx + bw - br, by + bh - br)),
            (bl, (bx, by + bh - bl)),
        ):
            if r <= 0:
                continue
            d.rectangle([x0, y0, x0 + r - 1, y0 + r - 1], fill=0)
        for r, (cx, cy, quadrant) in (
            (tl, (bx + tl, by + tl, (180, 270))),
            (tr, (bx + bw - tr, by + tr, (270, 360))),
            (br, (bx + bw - br, by + bh - br, (0, 90))),
            (bl, (bx + bl, by + bh - bl, (90, 180))),
        ):
            if r <= 0:
                continue
            d.pieslice([cx - r, cy - r, cx + r, cy + r], quadrant[0], quadrant[1], fill=255)

        drawn = Image.new("L", (vw * ss, vh * ss), 0)
        dx, dy, dw, dh = spec["draw_rect"]
        ImageDraw.Draw(drawn).rectangle(
            [(dx - vx) * ss, (dy - vy) * ss, (dx - vx + dw) * ss - 1, (dy - vy + dh) * ss - 1],
            fill=255,
        )

        mask = ImageChops.multiply(rounded, drawn).resize((vw, vh), Image.LANCZOS)
        if spec["opacity"] < 1.0:
            mask = mask.point(lambda v: int(v * spec["opacity"]))
        mask.save(mask_path)

    # -- step 5: concat -----------------------------------------------------------

    async def _concat_segments(self, segment_paths: list[str], work_dir: str) -> str:
        list_path = os.path.join(work_dir, "concat_list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for path in segment_paths:
                escaped = path.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        output_path = os.path.join(work_dir, "final.mp4")
        command = [
            self.ffmpeg, "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy",
            output_path,
        ]
        await self._run(command)
        return output_path

    # -- subprocess helper ----------------------------------------------------------

    async def _run(self, command: list[str], timeout: int = 600) -> None:
        LOGGER.info("[video_export] running: %s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise VideoExportError(
                f"Command timed out after {timeout}s: {' '.join(command)}"
            ) from exc

        if process.returncode != 0:
            raise VideoExportError(
                f"Command failed (code {process.returncode}): {' '.join(command)}\n"
                f"{stderr.decode(errors='ignore')[-4000:]}"
            )

    async def _capture(self, command: list[str], timeout: int = 60) -> str:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise VideoExportError(f"Command timed out: {' '.join(command)}") from exc
        if process.returncode != 0:
            raise VideoExportError(
                f"Command failed (code {process.returncode}): {' '.join(command)}\n"
                f"{stderr.decode(errors='ignore')[-2000:]}"
            )
        return stdout.decode(errors="ignore")

    async def _report(
        self,
        on_progress: Optional[SlideProgressCallback],
        completed: int,
        total: int,
        message: str,
    ) -> None:
        if on_progress is not None:
            await on_progress(completed, total, message)


VIDEO_EXPORT_SERVICE = VideoExportService()

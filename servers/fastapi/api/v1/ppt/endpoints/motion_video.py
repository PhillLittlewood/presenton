import base64
import logging
import os
import traceback
import uuid
import zipfile
from datetime import datetime
from typing import Optional

import aiohttp
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from pathvalidate import sanitize_filename
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from enums.async_task_status import AsyncTaskStatus
from models.sql.async_task import AsyncTaskModel
from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from services.video_export_service import VideoExportService
from services.database import async_session_maker, get_async_session
from services.motion_video_service import (
    MOTION_VIDEO_SERVICE,
    MotionVideoGenerationError,
)
from utils.asset_directory_utils import (
    absolute_fastapi_asset_url,
    get_exports_directory,
    get_motion_videos_directory,
    resolve_app_path_to_filesystem,
)
from utils.filename_utils import safe_export_basename
from utils.get_env import get_app_data_directory_env
from utils.llm_calls.generate_motion_prompt import generate_motion_prompt
from utils.video_motion_provider import (
    is_comfyui_motion_selected,
    is_motion_video_configured,
    is_motion_video_disabled,
)

LOGGER = logging.getLogger(__name__)

MOTION_VIDEO_ROUTER = APIRouter(prefix="/motion-video", tags=["Motion Video"])

ASYNC_TASK_TYPE_GENERATE_MOTION_CLIP = "image.generate_motion_clip"

MAX_SOURCE_IMAGE_BYTES = 25 * 1024 * 1024


class MotionVideoStatus(BaseModel):
    enabled: bool
    configured: bool


class SuggestMotionPromptRequest(BaseModel):
    image_prompt: Optional[str] = None
    slide_context: Optional[str] = None


class SuggestMotionPromptResponse(BaseModel):
    motion_prompt: str


class GenerateMotionClipRequest(BaseModel):
    image_url: str = Field(description="URL/path of the slide image (LTX start frame)")
    motion_prompt: Optional[str] = None
    duration_seconds: Optional[int] = Field(
        default=None, ge=1, le=30, description="Clip length; the workflow default if unset"
    )
    previous_motion_video: Optional[str] = Field(
        default=None,
        description="Existing clip on this image; deleted once the new one succeeds",
    )


class DeleteMotionClipRequest(BaseModel):
    motion_video: str


def _owner_motion_dir() -> str:
    return os.path.realpath(get_motion_videos_directory())


def _resolve_owned_motion_file(motion_video: str | None, owner_dir: str) -> Optional[str]:
    """Filesystem path of an existing clip iff it lives in this owner's motion dir."""
    if not motion_video:
        return None
    path = resolve_app_path_to_filesystem(motion_video)
    if not path:
        return None
    real = os.path.realpath(path)
    try:
        if os.path.commonpath([real, owner_dir]) != owner_dir:
            return None
    except ValueError:
        return None
    return real


def _delete_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
        LOGGER.info("[motion_video] removed old clip %s", path)
    except OSError:
        pass


def _motion_file_to_app_data_url(file_path: str) -> str:
    # <app_data>/motion[/users/<id>]/<file>  ->  /app_data/motion/...
    motion_root = os.path.realpath(
        os.path.join(get_app_data_directory_env(), "motion")
    )
    relative = os.path.relpath(os.path.realpath(file_path), motion_root)
    return absolute_fastapi_asset_url(
        "/app_data/motion/" + relative.replace(os.sep, "/")
    )


async def _materialize_source_image(image_url: str, work_dir: str) -> str:
    """Return a local file path for the slide image (local, data: URI, or http[s])."""
    local = resolve_app_path_to_filesystem(image_url)
    if local:
        return local

    if image_url.startswith("data:"):
        try:
            header, encoded = image_url.split(",", 1)
            data = base64.b64decode(encoded)
        except Exception as exc:
            raise MotionVideoGenerationError("Invalid data: image URI") from exc
        ext = ".png" if "png" in header else ".jpg"
    elif image_url.startswith(("http://", "https://")):
        async with aiohttp.ClientSession(trust_env=True) as session:
            response = await session.get(
                image_url, timeout=aiohttp.ClientTimeout(total=60)
            )
            if response.status != 200:
                raise MotionVideoGenerationError(
                    f"Could not download source image ({response.status})"
                )
            data = await response.content.read(MAX_SOURCE_IMAGE_BYTES + 1)
        ext = os.path.splitext(image_url.split("?")[0])[1] or ".jpg"
    else:
        raise MotionVideoGenerationError("Source image could not be resolved")

    if len(data) > MAX_SOURCE_IMAGE_BYTES:
        raise MotionVideoGenerationError("Source image is too large")
    path = os.path.join(work_dir, f"source-{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


@MOTION_VIDEO_ROUTER.get("/status", response_model=MotionVideoStatus)
async def get_motion_video_status():
    configured = is_motion_video_configured() and is_comfyui_motion_selected()
    return MotionVideoStatus(
        enabled=configured and not is_motion_video_disabled(),
        configured=configured,
    )


@MOTION_VIDEO_ROUTER.post("/suggest-prompt", response_model=SuggestMotionPromptResponse)
async def suggest_motion_prompt(body: SuggestMotionPromptRequest):
    prompt = await generate_motion_prompt(body.image_prompt, body.slide_context)
    return SuggestMotionPromptResponse(motion_prompt=prompt)


async def _run_generate_motion_clip_task(
    task_id: str,
    source_image: str,
    motion_prompt: Optional[str],
    output_directory: str,
    previous_clip_path: Optional[str],
    duration_seconds: Optional[int] = None,
) -> None:
    async with async_session_maker() as sql_session:
        async_status = await sql_session.get(AsyncTaskModel, task_id)
        if not async_status:
            LOGGER.warning("[motion_video] task missing task_id=%s", task_id)
            return

        async def save() -> None:
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        try:
            async_status.message = "Generating motion clip (this can take a few minutes)"
            await save()

            image_path = await _materialize_source_image(source_image, output_directory)
            try:
                clip_path = await MOTION_VIDEO_SERVICE.generate_motion_clip_comfyui(
                    image_path, motion_prompt, output_directory, duration_seconds
                )
            finally:
                # Only delete source copies we created (downloaded / data: URIs).
                if os.path.basename(image_path).startswith("source-"):
                    _delete_quietly(image_path)

            # Regeneration succeeded: drop the superseded clip so it isn't orphaned.
            if previous_clip_path and os.path.realpath(previous_clip_path) != os.path.realpath(clip_path):
                _delete_quietly(previous_clip_path)

            async_status.status = AsyncTaskStatus.COMPLETED
            async_status.message = "Motion clip ready"
            async_status.data = {"motion_video": _motion_file_to_app_data_url(clip_path)}
        except Exception as exc:
            # The existing static image (and any previous clip) stays untouched.
            LOGGER.warning("[motion_video] generation failed: %s", exc)
            if not isinstance(exc, MotionVideoGenerationError):
                traceback.print_exc()
            async_status.status = AsyncTaskStatus.ERROR
            async_status.message = "Motion clip generation failed"
            async_status.error = {"detail": str(exc)}
        await save()


@MOTION_VIDEO_ROUTER.post("/generate/async", response_model=AsyncTaskModel)
async def generate_motion_clip_async(
    body: GenerateMotionClipRequest,
    background_tasks: BackgroundTasks,
    sql_session: AsyncSession = Depends(get_async_session),
):
    """
    Queue motion-clip generation. Poll GET /api/v1/async-tasks/status/{id};
    on completion `data.motion_video` holds the clip's /app_data/motion/... URL.
    """
    if is_motion_video_disabled():
        raise HTTPException(status_code=400, detail="Motion video is disabled")
    if not (is_motion_video_configured() and is_comfyui_motion_selected()):
        raise HTTPException(
            status_code=400,
            detail="Motion video is not configured. Set the ComfyUI motion server "
            "URL and workflow in Settings.",
        )

    # Resolve owner-scoped paths now, in the request context.
    owner_dir = _owner_motion_dir()
    previous_clip_path = _resolve_owned_motion_file(body.previous_motion_video, owner_dir)

    async_status = AsyncTaskModel(
        type=ASYNC_TASK_TYPE_GENERATE_MOTION_CLIP,
        status=AsyncTaskStatus.PENDING,
        message="Queued for motion clip generation",
        data={},
    )
    sql_session.add(async_status)
    await sql_session.commit()
    await sql_session.refresh(async_status)

    background_tasks.add_task(
        _run_generate_motion_clip_task,
        async_status.id,
        body.image_url,
        body.motion_prompt,
        owner_dir,
        previous_clip_path,
        body.duration_seconds,
    )
    return async_status


@MOTION_VIDEO_ROUTER.post("/delete", status_code=204)
async def delete_motion_clip(body: DeleteMotionClipRequest):
    """Delete a clip file (used when the user removes it from an image)."""
    owner_dir = _owner_motion_dir()
    _delete_quietly(_resolve_owned_motion_file(body.motion_video, owner_dir))


class ExportMotionClipsResult(BaseModel):
    count: int
    relative_path: Optional[str] = None


@MOTION_VIDEO_ROUTER.post(
    "/presentation/{id}/export-clips", response_model=ExportMotionClipsResult
)
async def export_presentation_motion_clips(
    id: uuid.UUID = Path(description="Presentation whose motion clips to bundle"),
    sql_session: AsyncSession = Depends(get_async_session),
):
    """
    PPTX companion export. Embedding video in the .pptx isn't supported by the
    bundled export runtime, so the .pptx keeps the static images and the motion
    clips are delivered as a separate zip (slide-NN-*.mp4) the user can drop
    onto the slides. Returns count=0 (no file) when the deck has no clips.
    """
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise HTTPException(status_code=404, detail="Presentation not found")
    slides = list(
        await sql_session.scalars(
            select(SlideModel)
            .where(SlideModel.presentation == id)
            .order_by(SlideModel.index)
        )
    )

    entries: list[tuple[str, str]] = []
    for position, slide in enumerate(slides, start=1):
        # Bundle every clip on the slide, including images the video export
        # can't composite; the user places them manually here anyway.
        for n, clip_url in enumerate(_iter_motion_urls(slide.ui), start=1):
            path = VideoExportService._resolve_motion_clip_file(clip_url, slide.owner_id)
            if path:
                suffix = f"-{n}" if n > 1 else ""
                entries.append((f"slide-{position:02d}{suffix}.mp4", path))

    if not entries:
        return ExportMotionClipsResult(count=0)

    title = (presentation.title or "").strip() or str(id)
    base_name = safe_export_basename(sanitize_filename(title)) or str(id)
    exports_dir = get_exports_directory()
    zip_path = os.path.join(exports_dir, f"{base_name}-motion-clips.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
        for arcname, path in entries:
            archive.write(path, arcname)

    exports_root = os.path.join(get_app_data_directory_env(), "exports")
    relative = os.path.relpath(zip_path, exports_root).replace(os.sep, "/")
    return ExportMotionClipsResult(count=len(entries), relative_path=relative)


def _iter_motion_urls(ui) -> list[str]:
    """All `motion_video` URLs anywhere in a slide's ui, in document order."""
    urls: list[str] = []

    def visit(node) -> None:
        if isinstance(node, dict):
            value = node.get("motion_video")
            if node.get("type") == "image" and isinstance(value, str) and value:
                urls.append(value)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(ui)
    return urls

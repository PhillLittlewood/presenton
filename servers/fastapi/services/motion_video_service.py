"""
Generates a short motion clip from a slide image using a ComfyUI LTX
(image-to-video) workflow.

Mirrors services/narration_service.py (submit workflow -> poll /history ->
download output), with three differences:
- the slide image is uploaded to ComfyUI first and injected into the
  workflow's image-input node,
- outputs are scanned for video file extensions instead of audio ones,
- any audio track LTX produces is discarded (the clip is re-muxed video-only)
  so it can never collide with the slide's narration.

Required environment variables:
- COMFYUI_MOTION_URL: ComfyUI server URL (falls back to COMFYUI_URL if unset)
- COMFYUI_MOTION_WORKFLOW: Workflow JSON (API format). Node-title convention,
  consistent with "Input Prompt" for image gen / narration:
    * "Load Image"   (required) - a LoadImage node; receives the uploaded slide
      image as the LTX start frame. ("Input Image" is also accepted.)
    * "Input Prompt" (optional) - a text node that receives the motion prompt.
      ("Motion Prompt" is also accepted.) If absent, the workflow's own prompt
      is used unchanged.
    * "Width" / "Height" (optional, int nodes) - set from the slide image's
      aspect ratio so the clip lines up with the image.
    * "Duration" (optional, int node) - clip length in seconds.
  Only nodes holding a literal number are written; int nodes that are wired to
  another node (subgraph inner nodes) are left alone and follow their source.
- VIDEO_MOTION_MAX_CONCURRENCY: max simultaneous motion generations
  (default 1, since image gen / TTS / LTX share one ComfyUI GPU).
"""

import asyncio
import json
import logging
import mimetypes
import os
import uuid
from typing import Optional

import aiohttp

from utils.get_env import (
    get_comfyui_motion_url_env,
    get_comfyui_motion_workflow_env,
    get_ffmpeg_binary_env,
    get_motion_video_max_concurrency_env,
)

LOGGER = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")

INPUT_IMAGE_NODE_TITLES = ("load image", "input image")
# Preference order. ComfyUI's LTX template calls its text node just "Prompt";
# that is accepted last (it often only forwards from an "Input Prompt" node).
MOTION_PROMPT_NODE_TITLES = ("input prompt", "motion prompt", "prompt")
WIDTH_NODE_TITLE = "width"
HEIGHT_NODE_TITLE = "height"
DURATION_NODE_TITLE = "duration"

# LTX works on pixel dimensions divisible by 32; the workflow also halves them
# for the base pass before the x2 latent upscale. Aim for ~720p worth of pixels
# whatever the image's aspect ratio.
_TARGET_PIXELS = 1280 * 720
_DIMENSION_MULTIPLE = 32
_MIN_DIMENSION = 256
_MAX_DIMENSION = 1920
DEFAULT_DURATION_SECONDS = 5

# LTX is slow; give it far more headroom than TTS/image generation.
DEFAULT_TIMEOUT_SECONDS = 1800


class MotionVideoGenerationError(Exception):
    pass


_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_motion_video_max_concurrency_env())
    return _semaphore


def _norm(x) -> str:
    return str(x or "").strip().lower()


def _is_link(v) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) >= 2
        and isinstance(v[0], (str, int))
        and isinstance(v[1], int)
    )


class MotionVideoService:
    async def generate_motion_clip_comfyui(
        self,
        image_path: str,
        motion_prompt: Optional[str],
        output_directory: str,
        duration_seconds: Optional[int] = None,
    ) -> str:
        """
        Generate a silent motion clip whose first frame is `image_path`.
        Returns the filesystem path of the .mp4 saved in `output_directory`.
        """
        comfyui_url = get_comfyui_motion_url_env()
        workflow_json = get_comfyui_motion_workflow_env()

        if not comfyui_url:
            raise MotionVideoGenerationError(
                "COMFYUI_MOTION_URL (or COMFYUI_URL) environment variable is not set"
            )
        if not workflow_json:
            raise MotionVideoGenerationError(
                "COMFYUI_MOTION_WORKFLOW environment variable is not set. "
                "Please provide a ComfyUI LTX image-to-video workflow JSON (API format)."
            )
        if not os.path.isfile(image_path):
            raise MotionVideoGenerationError(f"Source image not found: {image_path}")

        comfyui_url = comfyui_url.rstrip("/")
        try:
            workflow = json.loads(workflow_json)
        except json.JSONDecodeError as e:
            raise MotionVideoGenerationError(f"Invalid motion workflow JSON: {e}")

        async with _get_semaphore():
            async with aiohttp.ClientSession(trust_env=True) as session:
                uploaded_name = await self._upload_image(
                    session, comfyui_url, image_path
                )
                workflow = self._inject_image(workflow, uploaded_name)
                if motion_prompt and motion_prompt.strip():
                    workflow = self._inject_motion_prompt(
                        workflow, motion_prompt.strip()
                    )
                width, height = self._pick_generation_size(image_path)
                self._inject_int(workflow, WIDTH_NODE_TITLE, width)
                self._inject_int(workflow, HEIGHT_NODE_TITLE, height)
                if duration_seconds:
                    self._inject_int(workflow, DURATION_NODE_TITLE, duration_seconds)
                LOGGER.info(
                    "Motion workflow prepared: image=%s size=%sx%s duration=%ss",
                    uploaded_name, width, height, duration_seconds or "workflow default",
                )
                prompt_id = await self._submit_workflow(session, comfyui_url, workflow)
                status_data = await self._wait_for_completion(
                    session, comfyui_url, prompt_id
                )
                raw_path = await self._download_video(
                    session, comfyui_url, status_data, prompt_id, output_directory
                )

        return await self._strip_audio(raw_path, output_directory)

    # -- workflow injection --------------------------------------------------------

    def _build_node_index(self, workflow: dict) -> dict:
        if all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
            return workflow
        for key in ("prompt", "workflow", "nodes"):
            nested = workflow.get(key)
            if isinstance(nested, dict):
                return nested
        return workflow

    def _nodes_titled(self, workflow: dict, title: str) -> list[dict]:
        index = self._build_node_index(workflow)
        return [
            node
            for node in index.values()
            if isinstance(node, dict)
            and _norm(node.get("_meta", {}).get("title")) == title
        ]

    @staticmethod
    def _pick_generation_size(image_path: str) -> tuple[int, int]:
        """Width/height for the LTX run, matching the slide image's aspect ratio."""
        from PIL import Image

        try:
            with Image.open(image_path) as img:
                src_w, src_h = img.size
        except Exception:
            return 1280, 720
        if src_w <= 0 or src_h <= 0:
            return 1280, 720

        ratio = src_w / src_h
        if abs(ratio - 16 / 9) < 0.01:
            return 1280, 720  # the size the stock LTX workflow is built for

        width = (_TARGET_PIXELS * ratio) ** 0.5
        height = width / ratio

        def snap(value: float) -> int:
            snapped = int(round(value / _DIMENSION_MULTIPLE)) * _DIMENSION_MULTIPLE
            return max(_MIN_DIMENSION, min(_MAX_DIMENSION, snapped))

        return snap(width), snap(height)

    def _inject_int(self, workflow: dict, title: str, value: int) -> int:
        """Set every literal-valued int node with this title; returns how many."""
        changed = 0
        for node in self._nodes_titled(workflow, title):
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            current = inputs.get("value")
            # Wired inputs ([node_id, slot]) follow their source node; bools are
            # ints in Python but never a size.
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                inputs["value"] = int(value)
                changed += 1
        return changed

    def _inject_image(self, workflow: dict, uploaded_name: str) -> dict:
        nodes = [
            node
            for title in INPUT_IMAGE_NODE_TITLES
            for node in self._nodes_titled(workflow, title)
        ]
        if not nodes:
            raise MotionVideoGenerationError(
                "Could not find a node titled 'Load Image' in the motion "
                "workflow. Rename your LoadImage node to 'Load Image'."
            )
        for node in nodes:
            inputs = node.setdefault("inputs", {})
            for key in ("image", "filename", "file", "path"):
                if key in inputs and isinstance(inputs[key], str):
                    inputs[key] = uploaded_name
                    return workflow
        raise MotionVideoGenerationError(
            "Found the 'Load Image' node, but it has no writable image filename field."
        )

    def _inject_motion_prompt(self, workflow: dict, text: str) -> dict:
        index = self._build_node_index(workflow)
        preferred_keys = ("text", "value", "prompt", "string", "content", "input")
        ignore_keys = {"filename_prefix", "model", "language", "device"}
        visited: set[str] = set()

        def try_set(node_id) -> bool:
            node_id = str(node_id)
            if node_id in visited:
                return False
            visited.add(node_id)
            node = index.get(node_id)
            if not isinstance(node, dict):
                return False
            inputs = node.setdefault("inputs", {})
            for k in preferred_keys:
                if k in inputs and isinstance(inputs[k], str):
                    inputs[k] = text
                    return True
            candidates = [
                k
                for k, v in inputs.items()
                if isinstance(v, str) and k not in ignore_keys
            ]
            if len(candidates) == 1:
                inputs[candidates[0]] = text
                return True
            for v in inputs.values():
                if _is_link(v) and try_set(v[0]):
                    return True
            return False

        for title in MOTION_PROMPT_NODE_TITLES:
            for node_id, node in index.items():
                if (
                    isinstance(node, dict)
                    and _norm(node.get("_meta", {}).get("title")) == title
                ):
                    if try_set(node_id):
                        return workflow
                    raise MotionVideoGenerationError(
                        f"Found '{node.get('_meta', {}).get('title')}' node, but no "
                        "writable text field was found directly or through linked nodes."
                    )
        # The prompt node is optional, but the user typed a prompt: say why it
        # is being ignored instead of silently generating the workflow default.
        LOGGER.warning(
            "Motion workflow has no 'Input Prompt' node, so the motion prompt is "
            "ignored and the workflow's own prompt is used"
        )
        return workflow

    # -- ComfyUI upload / submit / poll / download ---------------------------------

    async def _upload_image(
        self, session: aiohttp.ClientSession, comfyui_url: str, image_path: str
    ) -> str:
        ext = os.path.splitext(image_path)[1] or ".png"
        upload_name = f"presenton-motion-{uuid.uuid4().hex}{ext}"
        content_type = mimetypes.guess_type(image_path)[0] or "application/octet-stream"

        form = aiohttp.FormData()
        with open(image_path, "rb") as f:
            form.add_field(
                "image", f.read(), filename=upload_name, content_type=content_type
            )
        form.add_field("overwrite", "true")

        response = await session.post(
            f"{comfyui_url}/upload/image",
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        )
        if response.status != 200:
            raise MotionVideoGenerationError(
                f"Failed to upload image to ComfyUI: {await response.text()}"
            )
        data = await response.json()
        name = data.get("name") or upload_name
        subfolder = data.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    async def _submit_workflow(
        self, session: aiohttp.ClientSession, comfyui_url: str, workflow: dict
    ) -> str:
        payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
        response = await session.post(
            f"{comfyui_url}/prompt",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if response.status != 200:
            raise MotionVideoGenerationError(
                f"Failed to submit motion workflow to ComfyUI: {await response.text()}"
            )
        data = await response.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise MotionVideoGenerationError("No prompt_id returned from ComfyUI")
        LOGGER.info("ComfyUI motion workflow submitted. Prompt ID: %s", prompt_id)
        return prompt_id

    async def _wait_for_completion(
        self,
        session: aiohttp.ClientSession,
        comfyui_url: str,
        prompt_id: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        poll_interval: int = 5,
    ) -> dict:
        start = asyncio.get_event_loop().time()
        while True:
            if asyncio.get_event_loop().time() - start > timeout:
                raise MotionVideoGenerationError(
                    f"ComfyUI motion workflow timed out after {timeout} seconds"
                )
            await asyncio.sleep(poll_interval)

            try:
                response = await session.get(
                    f"{comfyui_url}/history/{prompt_id}",
                    timeout=aiohttp.ClientTimeout(total=30),
                )
                if response.status != 200:
                    continue
                status_data = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue

            execution = status_data.get(prompt_id)
            if not execution:
                continue
            status = execution.get("status") or {}
            if status.get("status_str") == "error" or "error" in status:
                raise MotionVideoGenerationError(
                    f"ComfyUI motion workflow error: {status.get('error') or status}"
                )
            if status.get("completed", False) or execution.get("outputs"):
                return status_data

    async def _download_video(
        self,
        session: aiohttp.ClientSession,
        comfyui_url: str,
        status_data: dict,
        prompt_id: str,
        output_directory: str,
    ) -> str:
        outputs = (status_data.get(prompt_id) or {}).get("outputs") or {}
        if not outputs:
            raise MotionVideoGenerationError("No outputs found in ComfyUI motion response")

        # Video node packs (VHS, native SaveVideo, ...) report files under
        # different keys ("gifs", "videos", "images", ...), so scan everything
        # and prefer real outputs over temp previews.
        candidates: list[dict] = []
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            for entries in node_output.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if (
                        isinstance(entry, dict)
                        and isinstance(entry.get("filename"), str)
                        and entry["filename"].lower().endswith(VIDEO_EXTENSIONS)
                    ):
                        candidates.append(entry)
        candidates.sort(key=lambda e: 0 if e.get("type", "output") == "output" else 1)

        for entry in candidates:
            params = {
                "filename": entry["filename"],
                "type": entry.get("type", "output"),
            }
            if entry.get("subfolder"):
                params["subfolder"] = entry["subfolder"]
            response = await session.get(
                f"{comfyui_url}/view",
                params=params,
                timeout=aiohttp.ClientTimeout(total=300),
            )
            if response.status != 200:
                raise MotionVideoGenerationError(
                    f"Failed to download video from ComfyUI: {response.status}"
                )
            ext = os.path.splitext(entry["filename"])[1] or ".mp4"
            raw_path = os.path.join(output_directory, f"raw-{uuid.uuid4().hex}{ext}")
            with open(raw_path, "wb") as f:
                f.write(await response.read())
            LOGGER.info("Downloaded motion clip from ComfyUI: %s", raw_path)
            return raw_path

        raise MotionVideoGenerationError(
            "No video file found in ComfyUI motion outputs. Confirm the workflow's "
            "save-video node actually runs and produces an .mp4/.webm output."
        )

    # -- audio removal -------------------------------------------------------------

    async def _strip_audio(self, raw_path: str, output_directory: str) -> str:
        """
        Re-mux to a browser-playable, video-only H.264 MP4. `-an` drops every
        audio stream and `-map 0:v:0` keeps just the first video stream.
        """
        final_path = os.path.join(output_directory, f"{uuid.uuid4().hex}.mp4")
        command = [
            get_ffmpeg_binary_env(), "-y",
            "-i", raw_path,
            "-map", "0:v:0", "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            final_path,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            if process.returncode != 0:
                raise MotionVideoGenerationError(
                    f"ffmpeg failed to strip audio: {stderr.decode(errors='ignore')[-2000:]}"
                )
        except asyncio.TimeoutError as exc:
            raise MotionVideoGenerationError("ffmpeg timed out stripping audio") from exc
        finally:
            try:
                os.remove(raw_path)
            except OSError:
                pass
        return final_path


MOTION_VIDEO_SERVICE = MotionVideoService()

import asyncio
import json
import shutil
import subprocess

import pytest
from aiohttp import web
from PIL import Image

from services.motion_video_service import (
    MOTION_VIDEO_SERVICE,
    MotionVideoGenerationError,
    MotionVideoService,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

WORKFLOW = {
    "1": {
        "class_type": "LoadImage",
        "inputs": {"image": "placeholder.png"},
        "_meta": {"title": "Input Image"},
    },
    "2": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "default prompt", "clip": ["9", 0]},
        "_meta": {"title": "Motion Prompt"},
    },
}


def _make_clip_with_audio(path):
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=1",
            "-f", "lavfi", "-i", "sine=f=440:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )


def _stream_types(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [s["codec_type"] for s in json.loads(out)["streams"]]


def test_generates_video_only_clip_and_injects_nodes(tmp_path, monkeypatch):
    clip = tmp_path / "ltx.mp4"
    _make_clip_with_audio(clip)
    source = tmp_path / "slide.png"
    Image.new("RGB", (64, 36), "red").save(source)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seen = {}

    async def upload(request):
        form = await request.post()
        seen["upload_name"] = form["image"].filename
        return web.json_response({"name": form["image"].filename, "subfolder": "", "type": "input"})

    async def prompt(request):
        seen["workflow"] = (await request.json())["prompt"]
        return web.json_response({"prompt_id": "abc"})

    async def history(request):
        return web.json_response(
            {
                "abc": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "7": {"images": [{"filename": "preview.png", "type": "temp"}]},
                        "8": {"gifs": [{"filename": "ltx.mp4", "type": "output", "subfolder": ""}]},
                    },
                }
            }
        )

    async def view(request):
        assert request.query["filename"] == "ltx.mp4"
        return web.Response(body=clip.read_bytes())

    async def run():
        app = web.Application()
        app.add_routes(
            [
                web.post("/upload/image", upload),
                web.post("/prompt", prompt),
                web.get("/history/abc", history),
                web.get("/view", view),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv("COMFYUI_MOTION_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("COMFYUI_MOTION_WORKFLOW", json.dumps(WORKFLOW))
        real_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await real_sleep(0)

        monkeypatch.setattr("services.motion_video_service.asyncio.sleep", fast_sleep)
        try:
            return await MOTION_VIDEO_SERVICE.generate_motion_clip_comfyui(
                str(source), "slow push-in", str(out_dir)
            )
        finally:
            await runner.cleanup()

    result = asyncio.run(run())

    assert result.endswith(".mp4")
    assert _stream_types(clip) == ["video", "audio"]  # LTX output had audio ...
    assert _stream_types(result) == ["video"]  # ... the stored clip must not
    assert seen["workflow"]["1"]["inputs"]["image"] == seen["upload_name"]
    assert seen["workflow"]["2"]["inputs"]["text"] == "slow push-in"
    # Raw download is removed; only the final clip remains.
    assert [p.name for p in out_dir.iterdir()] == [result.split("/")[-1]]


def test_motion_prompt_node_is_optional():
    svc = MotionVideoService()
    workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}, "_meta": {"title": "Input Image"}}}
    assert svc._inject_motion_prompt(workflow, "anything") == workflow


def test_missing_input_image_node_is_a_clear_error():
    svc = MotionVideoService()
    with pytest.raises(MotionVideoGenerationError, match="Input Image"):
        svc._inject_image({"1": {"class_type": "LoadImage", "inputs": {"image": "x"}}}, "y.png")

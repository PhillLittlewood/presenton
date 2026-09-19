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
    with pytest.raises(MotionVideoGenerationError, match="Load Image"):
        svc._inject_image({"1": {"class_type": "LoadImage", "inputs": {"image": "x"}}}, "y.png")


# Titles exactly as used by the LTX workflow shipped with the docs: top-level
# literal nodes plus subgraph inner nodes that are wired to them.
LTX_WORKFLOW = {
    "269": {"class_type": "LoadImage", "inputs": {"image": "old.png"}, "_meta": {"title": "Load Image"}},
    "331": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "men dancing"}, "_meta": {"title": "Input Prompt"}},
    "334": {"class_type": "PrimitiveInt", "inputs": {"value": 0}, "_meta": {"title": "Width"}},
    "335": {"class_type": "PrimitiveInt", "inputs": {"value": 0}, "_meta": {"title": "Height"}},
    "336": {"class_type": "PrimitiveInt", "inputs": {"value": 5}, "_meta": {"title": "Duration"}},
    "320:312": {"class_type": "PrimitiveInt", "inputs": {"value": ["334", 0]}, "_meta": {"title": "Width"}},
    "320:299": {"class_type": "PrimitiveInt", "inputs": {"value": ["335", 0]}, "_meta": {"title": "Height"}},
    "320:301": {"class_type": "PrimitiveInt", "inputs": {"value": ["336", 0]}, "_meta": {"title": "Duration"}},
    "320:319": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": ["331", 0]}, "_meta": {"title": "Prompt"}},
}


def test_ltx_workflow_titles_are_filled_and_wired_nodes_left_alone():
    import copy

    svc = MotionVideoService()
    wf = copy.deepcopy(LTX_WORKFLOW)
    svc._inject_image(wf, "uploaded.png")
    svc._inject_motion_prompt(wf, "two men dancing")
    assert svc._inject_int(wf, "width", 1120) == 1
    assert svc._inject_int(wf, "height", 832) == 1
    assert svc._inject_int(wf, "duration", 6) == 1

    assert wf["269"]["inputs"]["image"] == "uploaded.png"
    assert wf["331"]["inputs"]["value"] == "two men dancing"
    assert [wf[k]["inputs"]["value"] for k in ("334", "335", "336")] == [1120, 832, 6]
    # Subgraph inner nodes keep pointing at their source nodes.
    assert wf["320:312"]["inputs"]["value"] == ["334", 0]
    assert wf["320:319"]["inputs"]["value"] == ["331", 0]


def test_generation_size_follows_the_image_aspect_ratio(tmp_path):
    pick = MotionVideoService._pick_generation_size

    def size_for(w, h):
        path = tmp_path / f"{w}x{h}.png"
        Image.new("RGB", (w, h)).save(path)
        return pick(str(path))

    assert size_for(1920, 1080) == (1280, 720)  # stock 16:9 size
    w, h = size_for(1000, 1000)
    assert w == h and w % 32 == 0
    w, h = size_for(900, 1600)  # portrait stays portrait
    assert h > w and w % 32 == 0 and h % 32 == 0
    assert pick(str(tmp_path / "missing.png")) == (1280, 720)

import asyncio
import json
import os
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.ppt.endpoints import motion_video as mv
from enums.async_task_status import AsyncTaskStatus
from models.sql.async_task import AsyncTaskModel
from services.database import get_async_session
from services.motion_video_service import MotionVideoGenerationError

from tests.conftest import FakeAsyncSession


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("COMFYUI_MOTION_URL", "http://comfy.test")
    monkeypatch.setenv("COMFYUI_MOTION_WORKFLOW", json.dumps({"1": {}}))
    monkeypatch.delenv("DISABLE_MOTION_VIDEO", raising=False)
    monkeypatch.delenv("MOTION_VIDEO_PROVIDER", raising=False)
    (tmp_path / "motion").mkdir()
    return tmp_path


def _client(session):
    app = FastAPI()
    app.include_router(mv.MOTION_VIDEO_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: session
    return TestClient(app)


def test_status_reflects_configuration_and_disable_flag(app_data, monkeypatch):
    client = _client(FakeAsyncSession())
    assert client.get("/motion-video/status").json() == {"enabled": True, "configured": True}

    monkeypatch.setenv("DISABLE_MOTION_VIDEO", "true")
    assert client.get("/motion-video/status").json() == {"enabled": False, "configured": True}

    monkeypatch.delenv("DISABLE_MOTION_VIDEO")
    monkeypatch.delenv("COMFYUI_MOTION_WORKFLOW")
    assert client.get("/motion-video/status").json() == {"enabled": False, "configured": False}


def test_generate_is_rejected_when_disabled_or_unconfigured(app_data, monkeypatch):
    client = _client(FakeAsyncSession())
    body = {"image_url": "/app_data/images/a.png"}

    monkeypatch.setenv("DISABLE_MOTION_VIDEO", "true")
    assert client.post("/motion-video/generate/async", json=body).status_code == 400

    monkeypatch.delenv("DISABLE_MOTION_VIDEO")
    monkeypatch.delenv("COMFYUI_MOTION_WORKFLOW")
    assert client.post("/motion-video/generate/async", json=body).status_code == 400


def test_generate_queues_an_async_task_with_its_own_type(app_data):
    session = FakeAsyncSession()
    client = _client(session)
    with patch.object(mv, "_run_generate_motion_clip_task", new=AsyncMock()) as run:
        response = client.post(
            "/motion-video/generate/async",
            json={"image_url": "/app_data/images/a.png", "motion_prompt": "push in"},
        )
    assert response.status_code == 200
    task = response.json()
    assert task["type"] == "image.generate_motion_clip"
    assert task["status"] == "pending"
    run.assert_awaited_once()
    assert run.await_args.args[2] == "push in"


def test_suggest_prompt_uses_the_stored_image_prompt(app_data):
    client = _client(FakeAsyncSession())
    with patch.object(mv, "generate_motion_prompt", new=AsyncMock(return_value="slow pan")) as gen:
        response = client.post(
            "/motion-video/suggest-prompt", json={"image_prompt": "a red barn at dawn"}
        )
    assert response.json() == {"motion_prompt": "slow pan"}
    gen.assert_awaited_once_with("a red barn at dawn", None)


def _run_task(app_data, service_side_effect, previous):
    """Run the background task against a fake session; returns the task row."""
    task = AsyncTaskModel(
        type="image.generate_motion_clip", status=AsyncTaskStatus.PENDING, data={}
    )
    session = FakeAsyncSession(get_results={task.id: task})

    @asynccontextmanager
    async def fake_maker():
        yield session

    source = app_data / "src.png"
    source.write_bytes(b"png")
    out_dir = str(app_data / "motion")
    with patch.object(mv, "async_session_maker", fake_maker), patch.object(
        mv.MOTION_VIDEO_SERVICE,
        "generate_motion_clip_comfyui",
        new=AsyncMock(side_effect=service_side_effect),
    ):
        asyncio.run(
            mv._run_generate_motion_clip_task(
                task.id, str(source), "push in", out_dir, previous
            )
        )
    return task


def test_successful_regeneration_removes_the_superseded_clip(app_data):
    old = app_data / "motion" / "old.mp4"
    new = app_data / "motion" / "new.mp4"
    old.write_bytes(b"old")
    new.write_bytes(b"new")

    task = _run_task(app_data, [str(new)], str(old))

    assert task.status == AsyncTaskStatus.COMPLETED
    assert task.data["motion_video"].endswith("/app_data/motion/new.mp4")
    assert not old.exists() and new.exists()


def test_failed_generation_keeps_the_previous_clip_and_reports_error(app_data):
    old = app_data / "motion" / "old.mp4"
    old.write_bytes(b"old")

    task = _run_task(app_data, MotionVideoGenerationError("ComfyUI exploded"), str(old))

    assert task.status == AsyncTaskStatus.ERROR
    assert "ComfyUI exploded" in task.error["detail"]
    assert old.exists()


class _SessionWithSlides(FakeAsyncSession):
    def __init__(self, presentation, slides):
        super().__init__(get_results={presentation.id: presentation})
        self._slides = slides

    async def scalars(self, *_a, **_k):
        return self._slides


def test_export_clips_zips_every_clip_in_slide_order(app_data):
    import uuid

    (app_data / "motion" / "a.mp4").write_bytes(b"A")
    (app_data / "motion" / "b.mp4").write_bytes(b"B")
    pid = uuid.uuid4()
    presentation = SimpleNamespace(id=pid, title="My Deck")
    img = lambda url: {"type": "image", "motion_video": url, "position": {"x": 0, "y": 0}}  # noqa: E731
    slides = [
        SimpleNamespace(owner_id=None, ui={"elements": [], "components": []}),
        SimpleNamespace(
            owner_id=None,
            ui={
                "elements": [img("/app_data/motion/a.mp4")],
                "components": [{"position": {"x": 0, "y": 0}, "elements": [img("/app_data/motion/b.mp4")]}],
            },
        ),
    ]
    client = _client(_SessionWithSlides(presentation, slides))

    body = client.post(f"/motion-video/presentation/{pid}/export-clips").json()

    assert body["count"] == 2
    with zipfile.ZipFile(app_data / "exports" / os.path.basename(body["relative_path"])) as z:
        assert sorted(z.namelist()) == ["slide-02-2.mp4", "slide-02.mp4"]


def test_export_clips_is_empty_without_clips(app_data):
    import uuid

    pid = uuid.uuid4()
    session = _SessionWithSlides(
        SimpleNamespace(id=pid, title="Deck"),
        [SimpleNamespace(owner_id=None, ui={"elements": [], "components": []})],
    )
    assert _client(session).post(f"/motion-video/presentation/{pid}/export-clips").json() == {
        "count": 0,
        "relative_path": None,
    }


def test_only_clips_inside_the_owner_motion_dir_can_be_deleted_or_superseded(app_data):
    clip = app_data / "motion" / "mine.mp4"
    clip.write_bytes(b"x")
    image = app_data / "images"
    image.mkdir()
    (image / "photo.png").write_bytes(b"x")
    owner_dir = mv._owner_motion_dir()

    assert mv._resolve_owned_motion_file("/app_data/motion/mine.mp4", owner_dir) == os.path.realpath(clip)
    # An image, a traversal attempt and garbage must all resolve to nothing.
    assert mv._resolve_owned_motion_file("/app_data/images/photo.png", owner_dir) is None
    assert mv._resolve_owned_motion_file("/app_data/motion/../images/photo.png", owner_dir) is None
    assert mv._resolve_owned_motion_file(None, owner_dir) is None

    _client(FakeAsyncSession()).post(
        "/motion-video/delete", json={"motion_video": "/app_data/images/photo.png"}
    )
    assert (image / "photo.png").exists()
    _client(FakeAsyncSession()).post(
        "/motion-video/delete", json={"motion_video": "/app_data/motion/mine.mp4"}
    )
    assert not clip.exists()


def _make_test_video(path, duration=1.0, with_audio=True):
    import subprocess

    command = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=160x90:r=10:d={duration}",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=f=440:d={duration}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac", "-shortest"]
    command.append(str(path))
    subprocess.run(command, check=True)


def _upload(client, path, filename="clip.mp4", previous=None):
    with open(path, "rb") as handle:
        data = {"file": (filename, handle, "video/mp4")}
        if previous is not None:
            data["previous_motion_video"] = (None, previous)
        return client.post("/motion-video/upload", files=data)


@pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed"
)
def test_upload_stores_a_video_only_clip_in_the_owner_motion_dir(app_data, tmp_path):
    import json
    import subprocess

    source = tmp_path / "clip.mp4"
    _make_test_video(source, duration=1.0, with_audio=True)

    response = _upload(_client(FakeAsyncSession()), source)
    assert response.status_code == 200
    body = response.json()
    assert "/app_data/motion/" in body["motion_video"]

    stored = app_data / "motion" / body["motion_video"].split("/app_data/motion/")[1]
    assert stored.exists()
    streams = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(stored)],
            capture_output=True, text=True, check=True,
        ).stdout
    )["streams"]
    assert [s["codec_type"] for s in streams] == ["video"]  # audio was stripped


def test_upload_replaces_the_previous_clip(app_data, tmp_path):
    old = app_data / "motion" / "old.mp4"
    old.write_bytes(b"old")
    source = tmp_path / "clip.mp4"
    _make_test_video(source, duration=0.5, with_audio=False)

    response = _upload(_client(FakeAsyncSession()), source, previous="/app_data/motion/old.mp4")
    assert response.status_code == 200
    assert not old.exists()


def test_upload_rejects_an_unsupported_extension(app_data, tmp_path):
    source = tmp_path / "clip.txt"
    source.write_text("not a video")
    response = _upload(_client(FakeAsyncSession()), source, filename="clip.txt")
    assert response.status_code == 400
    assert "Unsupported video file type" in response.json()["detail"]


def test_upload_rejects_a_file_that_is_not_actually_a_video(app_data, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"not actually a video, just bytes with the right extension")
    response = _upload(_client(FakeAsyncSession()), source)
    assert response.status_code == 400
    assert "Could not read that file as a video" in response.json()["detail"]
    # The invalid upload must not be left behind under the owner's motion dir.
    assert list((app_data / "motion").glob("upload-*")) == []


def test_upload_rejects_oversized_files(app_data, tmp_path, monkeypatch):
    monkeypatch.setattr(mv, "MAX_UPLOAD_VIDEO_BYTES", 10)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 1000)
    response = _upload(_client(FakeAsyncSession()), source)
    assert response.status_code == 400
    assert "smaller than" in response.json()["detail"]
    assert list((app_data / "motion").glob("upload-*")) == []


def test_upload_works_even_when_ai_motion_generation_is_not_configured(app_data, monkeypatch, tmp_path):
    monkeypatch.delenv("COMFYUI_MOTION_WORKFLOW")
    assert _client(FakeAsyncSession()).get("/motion-video/status").json()["configured"] is False

    source = tmp_path / "clip.mp4"
    _make_test_video(source, duration=0.3, with_audio=False)
    assert _upload(_client(FakeAsyncSession()), source).status_code == 200


@pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed"
)
def test_export_loops_a_short_clip_instead_of_fading_when_motion_loop_is_set(tmp_path):
    import subprocess

    from PIL import Image
    from services.motion_layout import find_motion_placements
    from services.video_export_service import VideoExportService, _MotionOverlay

    base = tmp_path / "base.png"
    Image.new("RGB", (640, 360), (20, 40, 80)).save(base)
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=200x150:r=12:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
    )
    narration = tmp_path / "narr.wav"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=3", str(narration)],
        check=True,
    )

    svc = VideoExportService()
    svc.ffmpeg, svc.ffprobe = "ffmpeg", "ffprobe"

    async def build(loop: bool, name: str) -> str:
        ui = {
            "elements": [
                {
                    "type": "image", "position": {"x": 60, "y": 60}, "size": {"width": 200, "height": 150},
                    "fit": "cover", "motion_video": "/app_data/motion/x.mp4", "motion_loop": loop,
                }
            ],
            "components": [],
        }
        placement = find_motion_placements(ui).placements[0]
        width, height, duration = await svc._probe_video(str(clip))
        overlay = _MotionOverlay(placement, str(clip), width, height, duration)
        output = tmp_path / name
        await svc._build_motion_segment(str(base), str(narration), [overlay], str(tmp_path), 0, str(output))
        return str(output)

    looped = asyncio.run(build(True, "looped.mp4"))
    faded = asyncio.run(build(False, "faded.mp4"))

    # Several points spread across the overlay box, so one coincidentally
    # base-colored pixel in the test pattern can't flip the result.
    sample_points = [(70, 70), (160, 90), (230, 130), (100, 180), (200, 190)]

    def pixels_at(path: str, t: float) -> list:
        frame = tmp_path / f"frame-{os.path.basename(path)}-{t}.png"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-ss", str(t), "-i", path, "-frames:v", "1", str(frame)],
            check=True,
        )
        image = Image.open(frame).convert("RGB")
        return [image.getpixel(p) for p in sample_points]

    base_color = Image.open(base).convert("RGB").getpixel(sample_points[0])

    def close_to_base(color: tuple) -> bool:
        # H.264 compression rounds colors slightly; allow a small tolerance.
        return all(abs(a - b) <= 6 for a, b in zip(color, base_color))

    # Well past the clip's 1s single-play length (narration runs to 3s+pad):
    # looping still shows motion (at least one sample differs from the flat
    # base color) there; fading has reverted to the static base everywhere.
    assert any(not close_to_base(c) for c in pixels_at(looped, 2.0))
    assert all(close_to_base(c) for c in pixels_at(faded, 2.0))

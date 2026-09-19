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

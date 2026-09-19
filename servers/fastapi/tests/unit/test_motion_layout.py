import os
import uuid

from api.v1.auth.assets import is_app_data_path_authorized
from services.motion_layout import compute_fit_mapping, find_motion_placements
from services.video_export_service import VideoExportService


def _img(**kw):
    base = {
        "type": "image",
        "position": {"x": 10, "y": 20},
        "size": {"width": 400, "height": 300},
        "motion_video": "/app_data/motion/a.mp4",
    }
    base.update(kw)
    return base


def test_root_and_component_images_get_absolute_boxes():
    ui = {
        "elements": [_img()],
        "components": [
            {"id": "c", "position": {"x": 100, "y": 50}, "elements": [_img()]},
        ],
    }
    scan = find_motion_placements(ui)
    assert [(p.box.x, p.box.y) for p in scan.placements] == [(10, 20), (110, 70)]
    assert scan.skipped == []


def test_images_without_a_clip_are_ignored():
    ui = {"elements": [_img(motion_video=None), {"type": "text"}], "components": []}
    scan = find_motion_placements(ui)
    assert scan.placements == [] and scan.skipped == []


def test_container_offsets_and_clips_children():
    container = {
        "type": "container",
        "position": {"x": 100, "y": 100},
        "size": {"width": 200, "height": 200},
        "child": _img(position={"x": -50, "y": 0}, size={"width": 300, "height": 200}),
    }
    scan = find_motion_placements({"elements": [container], "components": []})
    (p,) = scan.placements
    assert (p.box.x, p.box.y, p.box.width) == (50, 100, 300)
    # Visible area is the image box clipped by the container.
    assert (p.visible.x, p.visible.width) == (100, 200)


def test_flow_layout_rotation_and_clip_path_fall_back_to_static():
    flex = {"type": "flex", "position": {"x": 0, "y": 0}, "size": {"width": 500, "height": 500},
            "children": [_img()]}
    scan = find_motion_placements(
        {
            "elements": [flex, _img(rotation=15), _img(clip_path="circle(50%)")],
            "components": [],
        }
    )
    assert scan.placements == []
    assert len(scan.skipped) == 3


def test_fit_mapping_matches_editor_rules():
    # contain, wide source in a squarer box: letterboxed vertically, centred.
    m = compute_fit_mapping("contain", 0.5, 0.5, 1.0, 400, 400, 1600, 900)
    assert (m.draw.width, m.draw.height) == (400, 225) and m.draw.y == 87.5
    # cover, wide source: crop the sides.
    m = compute_fit_mapping("cover", 0.5, 0.5, 1.0, 400, 400, 1600, 900)
    assert m.crop == (350.0, 0.0, 900.0, 900.0)
    assert (m.draw.width, m.draw.height) == (400, 400)
    # fill stretches.
    m = compute_fit_mapping("fill", 0.5, 0.5, 1.0, 400, 300, 1600, 900)
    assert m.crop is None and (m.draw.width, m.draw.height) == (400, 300)


def test_motion_clip_resolution_is_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    owner, other = uuid.uuid4(), uuid.uuid4()
    mine = tmp_path / "motion" / "users" / str(owner)
    theirs = tmp_path / "motion" / "users" / str(other)
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)
    (mine / "a.mp4").write_bytes(b"x")
    (theirs / "b.mp4").write_bytes(b"x")

    resolve = VideoExportService._resolve_motion_clip_file
    assert resolve(f"/app_data/motion/users/{owner}/a.mp4", owner) == os.path.realpath(mine / "a.mp4")
    assert resolve(f"/app_data/motion/users/{other}/b.mp4", owner) is None
    assert resolve(f"/app_data/motion/users/{owner}/../{other}/b.mp4", owner) is None
    assert resolve("/app_data/images/x.png", owner) is None


def test_motion_assets_are_owner_scoped_in_the_browser():
    owner, other = uuid.uuid4(), uuid.uuid4()
    assert is_app_data_path_authorized(
        f"/app_data/motion/users/{owner}/a.mp4", user_id=owner, is_admin=False
    )
    assert not is_app_data_path_authorized(
        f"/app_data/motion/users/{other}/a.mp4", user_id=owner, is_admin=False
    )

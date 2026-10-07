from services.motion_layout import find_motion_placements


def img(name, w, h, **extra):
    return {"type": "image", "size": {"width": w, "height": h}, "motion_video": f"/app_data/motion/{name}.mp4",
            "name": name, **extra}


def test_two_images_in_a_flex_row_both_composite():
    # The reported bug: two videos on one slide -- previously both were
    # silently skipped because their parent was a flex container.
    ui = {
        "elements": [
            {"type": "flex", "position": {"x": 60, "y": 100}, "size": {"width": 1160, "height": 500},
             "direction": "row", "gap": 40, "children": [img("left", 560, 480), img("right", 560, 480)]},
        ],
        "components": [],
    }
    scan = find_motion_placements(ui)
    assert scan.skipped == []
    assert [p.element_name for p in scan.placements] == ["left", "right"]
    assert scan.placements[0].box.x == 60 and scan.placements[0].box.width == 560
    assert scan.placements[1].box.x == 660  # 60 + 560 + 40 gap


def test_images_in_a_grid_gallery_all_composite():
    ui = {
        "elements": [
            {"type": "grid", "position": {"x": 0, "y": 0}, "size": {"width": 1280, "height": 720},
             "columns": 2, "gap": 20,
             "children": [img("a", 1, 1), img("b", 1, 1), img("c", 1, 1), img("d", 1, 1)]},
        ],
        "components": [],
    }
    scan = find_motion_placements(ui)
    assert scan.skipped == []
    assert len(scan.placements) == 4
    xs = sorted({round(p.box.x) for p in scan.placements})
    assert len(xs) == 2  # two distinct columns


def test_nested_flex_inside_a_component_still_resolves():
    ui = {
        "elements": [],
        "components": [
            {"position": {"x": 40, "y": 40}, "elements": [
                {"type": "flex", "size": {"width": 400, "height": 200}, "direction": "row", "gap": 10,
                 "children": [img("one", 195, 200), img("two", 195, 200)]},
            ]},
        ],
    }
    scan = find_motion_placements(ui)
    assert scan.skipped == []
    assert scan.placements[0].box.x == 40  # component origin applied
    assert scan.placements[1].box.x == 40 + 195 + 10


def test_a_rotated_image_inside_a_flex_row_is_still_skipped():
    ui = {
        "elements": [
            {"type": "flex", "size": {"width": 800, "height": 300}, "direction": "row", "gap": 0,
             "children": [img("still", 400, 300), img("spinning", 400, 300, rotation=10)]},
        ],
        "components": [],
    }
    scan = find_motion_placements(ui)
    assert [p.element_name for p in scan.placements] == ["still"]
    assert len(scan.skipped) == 1 and "rotated" in scan.skipped[0]


def test_flex_container_does_not_clip_its_children():
    # Matches the real renderer: flex/grid always render with overflow:visible.
    ui = {
        "elements": [
            {"type": "flex", "position": {"x": 0, "y": 0}, "size": {"width": 100, "height": 100},
             "direction": "row", "align_items": "flex-start",
             "children": [img("overflowing", 400, 300)]},
        ],
        "components": [],
    }
    scan = find_motion_placements(ui)
    (p,) = scan.placements
    assert p.visible.width == 400  # not clamped to the 100px container

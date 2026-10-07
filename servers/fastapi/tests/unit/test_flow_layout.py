from services.flow_layout import FlowBox, layout_flow_children


def img(w, h, **extra):
    return {"type": "image", "size": {"width": w, "height": h}, **extra}


def test_flex_row_places_fixed_size_children_with_a_gap():
    parent = {"type": "flex", "direction": "row", "gap": 20}
    boxes = layout_flow_children(parent, [img(400, 300), img(400, 300)], FlowBox(0, 0, 1160, 400))
    assert boxes == [FlowBox(0, 0, 400, 400), FlowBox(420, 0, 400, 400)]


def test_exact_reproduction_of_the_two_video_bug():
    # 560 + gap 40 + 560 = 1160, matching the flex container's width.
    parent = {"type": "flex", "direction": "row", "gap": 40}
    children = [img(560, 480), img(560, 480)]
    boxes = layout_flow_children(parent, children, FlowBox(0, 0, 1160, 500))
    assert boxes[0] == FlowBox(0, 0, 560, 500)
    assert boxes[1] == FlowBox(600, 0, 560, 500)


def test_flex_column_centers_with_justify_and_align_center():
    parent = {
        "type": "flex", "direction": "column", "gap": 10,
        "justify_content": "center", "align_items": "center",
    }
    boxes = layout_flow_children(parent, [img(100, 50), img(100, 80)], FlowBox(0, 0, 400, 400))
    assert boxes == [FlowBox(150, 130, 100, 50), FlowBox(150, 190, 100, 80)]


def test_grid_divides_into_equal_stretched_cells_in_row_major_order():
    parent = {"type": "grid", "columns": 2, "gap": 10}
    boxes = layout_flow_children(parent, [img(1, 1) for _ in range(4)], FlowBox(0, 0, 500, 400))
    assert boxes == [
        FlowBox(0, 0, 245, 195), FlowBox(255, 0, 245, 195),
        FlowBox(0, 205, 245, 195), FlowBox(255, 205, 245, 195),
    ]


def test_grid_honors_explicit_column_and_row_spans():
    parent = {"type": "grid", "columns": 3, "gap": 0}
    children = [img(1, 1, layout={"column_span": 2}), img(1, 1), img(1, 1)]
    boxes = layout_flow_children(parent, children, FlowBox(0, 0, 300, 200))
    # First item spans 2 columns on row 0; the next item can't fit there and
    # moves to the remaining column, the third wraps to row 1.
    assert boxes[0] == FlowBox(0, 0, 200, 100)
    assert boxes[1] == FlowBox(200, 0, 100, 100)
    assert boxes[2] == FlowBox(0, 100, 100, 100)


def test_wrap_starts_a_new_line_when_the_next_item_does_not_fit():
    parent = {"type": "flex", "direction": "row", "wrap": True, "gap": 10}
    boxes = layout_flow_children(parent, [img(200, 100)] * 3, FlowBox(0, 0, 420, 400))
    assert [b.x for b in boxes] == [0, 210, 0]
    assert boxes[2].y > boxes[0].y  # third item wrapped to a new line


def test_grow_distributes_remaining_main_axis_space():
    parent = {"type": "flex", "direction": "row"}
    children = [img(100, 50), {"type": "image", "size": {"width": 100, "height": 50}, "layout": {"grow": 1}}]
    boxes = layout_flow_children(parent, children, FlowBox(0, 0, 500, 100))
    assert boxes[0].width == 100
    assert boxes[1].x == 100 and boxes[1].width == 400  # grows to fill the rest


def test_padding_and_stretch_cross_axis_are_applied():
    parent = {"type": "flex", "direction": "row", "padding": 20}
    boxes = layout_flow_children(parent, [img(100, 1)], FlowBox(0, 0, 400, 300))
    assert boxes[0] == FlowBox(20, 20, 100, 260)  # 300 - 20 - 20 padding, stretched


def test_decorative_divider_line_gets_its_fixed_thickness_not_zero():
    parent = {"type": "flex", "direction": "column"}
    divider = {"type": "vector", "points": [{"x": 0, "y": 0}, {"x": 100, "y": 0}], "closed": False}
    boxes = layout_flow_children(parent, [divider, img(50, 50)], FlowBox(0, 0, 200, 200))
    assert boxes[0].height == 4  # DECORATIVE_LINE_THICKNESS
    assert boxes[1].y == 4


def test_unsupported_parent_type_returns_no_boxes():
    assert layout_flow_children({"type": "container"}, [img(1, 1)], FlowBox(0, 0, 10, 10)) == [None]


def test_empty_container_returns_empty_list():
    assert layout_flow_children({"type": "flex"}, [], FlowBox(0, 0, 10, 10)) == []

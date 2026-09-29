from templates.v2.models.elements import Chart, Table, Vector


def test_table_keeps_borders_and_drops_other_conversion_extensions():
    table = Table.model_validate(
        {
            "type": "table",
            "decorative": True,
            "name": "summary",
            "flip_h": True,
            "columns": [
                {
                    "runs": [{"text": "Heading"}],
                    "borders": {
                        "bottom": {"color": "#FF9999", "width": 0}
                    },
                    "vertical_alignment": "middle",
                    "margins": {"top": 4, "right": 6, "bottom": 4, "left": 6},
                    "row_span": 2,
                    "col_span": 3,
                    "horizontal_merge": "continue",
                    "vertical_merge": "continue",
                }
            ],
            "rows": [[{"runs": [{"text": "Value"}]}]],
            "column_widths": [240],
            "row_heights": [40, 40],
            "min_columns": 1,
            "max_columns": 1,
            "min_rows": 1,
            "max_rows": 1,
        }
    )

    dumped = table.model_dump(exclude_none=True)
    assert dumped["flip_h"] is True
    assert dumped["columns"][0]["borders"]["bottom"]["width"] == 0
    assert "column_widths" not in dumped
    assert "row_heights" not in dumped
    for field in (
        "vertical_alignment",
        "margins",
        "row_span",
        "col_span",
        "horizontal_merge",
        "vertical_merge",
    ):
        assert field not in dumped["columns"][0]


def test_vector_keeps_transform_name_and_stroke_metadata():
    vector = Vector.model_validate(
        {
            "type": "vector",
            "name": "connector",
            "flip_h": True,
            "flip_v": True,
            "points": [{"x": 0, "y": 0}, {"x": 100, "y": 0}],
            "stroke": {
                "color": "#384351",
                "width": 6.22,
                "dash": [0, 12.45],
                "line_cap": "round",
                "line_join": "bevel",
                "start_marker": {"type": "oval", "length": "lg", "width": "lg"},
            },
        }
    )

    dumped = vector.model_dump(exclude_none=True)
    assert dumped["name"] == "connector"
    assert dumped["flip_h"] is True
    assert dumped["flip_v"] is True
    assert dumped["stroke"]["line_cap"] == "round"
    assert dumped["stroke"]["line_join"] == "bevel"
    assert dumped["stroke"]["start_marker"]["type"] == "oval"


def test_chart_keeps_legend_position_and_drops_series_extensions():
    chart = Chart.model_validate(
        {
            "type": "chart",
            "chart_type": "line",
            "decorative": True,
            "name": "trend",
            "flip_v": True,
            "legend_position": "right",
            "text_color": "#111827",
            "series": [
                {
                    "name": "Revenue",
                    "values": [10, 20],
                    "x_values": [2025, 2026],
                    "color": "#2563EB",
                }
            ],
        }
    )

    dumped = chart.model_dump(exclude_none=True)
    assert dumped["flip_v"] is True
    assert dumped["legend_position"] == "right"
    assert dumped["text_color"] == "#111827"
    assert dumped["series"] == [{"name": "Revenue", "values": [10.0, 20.0]}]

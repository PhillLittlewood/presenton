import type { SlideElement } from "@/components/slide-editor/types";

// FastAPI/export-core are the canonical Template V2 JSON model. These fixtures
// intentionally omit fields that those models default or mark optional. The
// editor may add fields, but every canonical value must remain assignable.
const canonicalElements = [
  {
    type: "text",
    alignment: { horizontal: "justify" },
    runs: [{ type: "text", text: "Canonical text" }],
    decorative: false,
    name: "Text",
    max_length: 120,
    min_length: 60,
  },
  {
    type: "table",
    columns: [
      {
        alignment: "justify",
        borders: { bottom: { color: "#111827", width: 1 } },
        runs: [{ text: "Heading" }],
      },
    ],
    rows: [[{ runs: [{ text: "Value" }] }]],
    decorative: true,
    name: "Table",
    max_columns: 1,
    min_columns: 1,
    max_rows: 2,
    min_rows: 1,
  },
  {
    type: "vector",
    points: [{ x: 0, y: 0 }, { x: 100, y: 0 }],
    start_marker: "circle",
    end_marker: "diamond",
  },
  {
    type: "chart",
    chart_type: "scatter",
    categories: ["A"],
    series: [{ name: "Series", values: [1] }],
    data_labels: true,
    legend_position: "right",
    text_color: "#475467",
    decorative: true,
    name: "Chart",
  },
  {
    type: "infographic",
    data: {
      type: "timeline",
      items: [{ icon: "https://example.com/icon.svg", heading: "Stage" }],
    },
    decorative: true,
    name: "Timeline",
  },
  {
    type: "infographic",
    data: {
      type: "gantt",
      columns: [{ label: "Q1" }, { label: "Q2" }],
      rows: [
        {
          label: "Work",
          items: [{
            name: "Task",
            start: { column: 0 },
            end: { column: 1 },
          }],
        },
        { label: "Empty" },
      ],
    },
    decorative: true,
    name: "Gantt",
  },
  {
    type: "infographic",
    data: {
      type: "before_after",
      items: [{ heading: "Before" }, { heading: "After" }],
    },
    decorative: true,
    name: "Before and after",
  },
  {
    type: "infographic",
    data: {
      type: "impact_effort_matrix",
      items: [
        { heading: "One" },
        { heading: "Two" },
        { heading: "Three" },
        { heading: "Four" },
      ],
    },
    decorative: true,
    name: "Matrix",
  },
  {
    type: "infographic",
    data: {
      type: "mind_map",
      items: [{ heading: "Root" }],
    },
    decorative: true,
    name: "Mind map",
  },
  { type: "flex", direction: "row", children: [], name: "Flex", max_children: 4, min_children: 2 },
  { type: "grid", columns: 2, children: [], name: "Grid", max_children: 4, min_children: 2 },
  { type: "group", children: [], name: "Group" },
] satisfies SlideElement[];

void canonicalElements;

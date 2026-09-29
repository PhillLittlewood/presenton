import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

let importer;
let textLayout;
let renderer;
let temporaryDirectory;

test.before(async () => {
  temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "presenton-renderer-fields-"),
  );
  const importerOutput = path.join(temporaryDirectory, "importer.mjs");
  const textLayoutOutput = path.join(temporaryDirectory, "text-layout.mjs");
  const rendererOutput = path.join(temporaryDirectory, "renderer.mjs");

  await Promise.all([
    build({
      entryPoints: [
        path.resolve(
          "components/slide-editor/importing/template-v2-import.ts",
        ),
      ],
      outfile: importerOutput,
      bundle: true,
      platform: "node",
      format: "esm",
      tsconfig: path.resolve("tsconfig.json"),
      logLevel: "silent",
    }),
    build({
      entryPoints: [
        path.resolve("components/slide-editor/text/template-v2-text.ts"),
      ],
      outfile: textLayoutOutput,
      bundle: true,
      platform: "node",
      format: "esm",
      tsconfig: path.resolve("tsconfig.json"),
      logLevel: "silent",
    }),
    build({
      entryPoints: [path.resolve("lib/template-v2-json-to-html.ts")],
      outfile: rendererOutput,
      bundle: true,
      platform: "node",
      format: "esm",
      tsconfig: path.resolve("tsconfig.json"),
      logLevel: "silent",
    }),
  ]);

  importer = await import(
    `${pathToFileURL(importerOutput).href}?cache=${Date.now()}`
  );
  textLayout = await import(
    `${pathToFileURL(textLayoutOutput).href}?cache=${Date.now()}`
  );
  renderer = await import(
    `${pathToFileURL(rendererOutput).href}?cache=${Date.now()}`
  );
});

test.after(async () => {
  if (temporaryDirectory) {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});

test("preserves text-list item and marker gaps when importing Template V2", () => {
  const slide = importer.adaptTemplateV2LayoutToSlide({
    id: "renderer-fields",
    elements: [
      {
        type: "text-list",
        marker: "bullet",
        gap: 9,
        marker_gap: 6,
        items: [[{ text: "One" }], [{ text: "Two" }]],
      },
    ],
  });

  assert.equal(slide.elements[0].gap, 9);
  assert.equal(slide.elements[0].marker_gap, 6);
});

test("preserves the supported PPTX JSON metadata when importing Template V2", () => {
  const slide = importer.adaptTemplateV2LayoutToSlide({
    id: "pptx-json-fields",
    elements: [
      {
        type: "text",
        flip_h: true,
        runs: [{ text: "Mirrored" }],
      },
      {
        type: "table",
        columns: [
          {
            runs: [{ text: "Heading" }],
            borders: {
              bottom: { color: "#FF9999", width: 0 },
            },
            row_span: 2,
          },
        ],
        rows: [[{ runs: [{ text: "Value" }] }]],
        column_widths: [120],
      },
      {
        type: "vector",
        name: "connector",
        flip_v: true,
        points: [{ x: 0, y: 0 }, { x: 100, y: 0 }],
        stroke: {
          color: "#384351",
          width: 19.56,
          dash: [0, 12.45],
          line_cap: "round",
          line_join: "bevel",
          start_marker: { type: "oval", length: "lg", width: "lg" },
        },
      },
      {
        type: "chart",
        chart_type: "bubble",
        text_color: "#475467",
        legend_position: "right",
        series: [
          {
            name: "Revenue",
            values: [10, 20],
            x_values: [2025, 2026],
            color: "#2563EB",
          },
        ],
      },
    ],
  });

  assert.equal(slide.elements[0].flip_h, true);
  assert.deepEqual(slide.elements[1].columns[0].borders, {
    bottom: { color: "#FF9999", opacity: 1, width: 0 },
  });
  assert.equal(slide.elements[1].columns[0].row_span, undefined);
  assert.equal(slide.elements[1].column_widths, undefined);
  assert.equal(slide.elements[2].name, "connector");
  assert.equal(slide.elements[2].flip_v, true);
  assert.deepEqual(slide.elements[2].stroke.start_marker, {
    type: "oval",
    length: "lg",
    width: "lg",
  });
  assert.equal(slide.elements[2].stroke.line_cap, "round");
  assert.equal(slide.elements[2].stroke.line_join, "bevel");
  assert.equal(slide.elements[2].stroke.width, 19.56);
  assert.equal(slide.elements[3].legend_position, "right");
  assert.equal(slide.elements[3].text_color, "#475467");
  assert.equal(slide.elements[3].chart_type, "scatter");
  assert.deepEqual(slide.elements[3].series, [
    { name: "Revenue", values: [10, 20] },
  ]);
});

test("accepts canonical FastAPI and export-core defaults", () => {
  const slide = importer.adaptTemplateV2LayoutToSlide({
    id: "canonical-model",
    elements: [
      {
        type: "text",
        alignment: { horizontal: "justify" },
        runs: [{ text: "Justified paragraph" }],
      },
      {
        type: "table",
        columns: [{ alignment: "justify", runs: [{ text: "Heading" }] }],
        rows: [[{ runs: [{ text: "Value" }] }]],
      },
      {
        type: "chart",
        chart_type: "bar",
        categories: ["A"],
        series: [{ name: "Series", values: [7] }],
        data_labels: true,
      },
      {
        type: "infographic",
        data: {
          type: "timeline",
          items: [
            { icon: "https://example.com/icon.svg", heading: "Stage" },
          ],
        },
      },
      {
        type: "group",
        children: [],
      },
    ],
  });

  assert.equal(slide.elements[0].alignment.horizontal, "justify");
  assert.equal(slide.elements[1].columns[0].alignment, "justify");
  assert.deepEqual(slide.elements[2].data, [
    { label: "A", value: 7, color: "7F22FE" },
  ]);
  assert.equal(slide.elements[2].data_labels, "top");
  assert.deepEqual(slide.elements[3].data.items[0].icon, {
    url: "https://example.com/icon.svg",
    color: "FFFFFF",
  });
  assert.deepEqual(slide.elements[4].position, { x: 0, y: 0 });
  assert.deepEqual(slide.elements[4].size, { width: 1, height: 1 });
});

test("adds text-list gap only between items in the canvas layout", () => {
  const baseElement = {
    type: "text-list",
    marker: "none",
    font: { family: "Arial", size: 10, line_height: 1 },
    items: [[{ text: "One" }], [{ text: "Two" }]],
  };
  const withoutGap = textLayout.layoutTextListRenderItems(
    baseElement,
    200,
    100,
  );
  const withGap = textLayout.layoutTextListRenderItems(
    { ...baseElement, gap: 7 },
    200,
    100,
  );

  assert.equal(withGap.contentHeight - withoutGap.contentHeight, 7);
  assert.equal(withGap.tokens[0].y, withoutGap.tokens[0].y);
  assert.equal(withGap.tokens[1].y - withoutGap.tokens[1].y, 7);
});

test("uses marker_gap between the marker and item text in canvas layout", () => {
  const { tokens } = textLayout.layoutTextListRenderItems(
    {
      type: "text-list",
      marker: "bullet",
      marker_gap: 8,
      font: { family: "Arial", size: 10, line_height: 1 },
      items: [[{ text: "One two three" }]],
    },
    40,
    100,
  );

  const marker = tokens[0];
  const contentTokens = tokens.slice(1).filter((token) => token.text.trim());
  const firstLineToken = contentTokens[0];
  const wrappedLineToken = contentTokens.find(
    (token) => token.y > firstLineToken.y,
  );

  assert.equal(firstLineToken.x - (marker.x + marker.width), 8);
  assert.equal(wrappedLineToken.x, firstLineToken.x);
});

test("honors text-list item and marker gaps in HTML", () => {
  const listHtml = renderer.templateV2UiToHtmlFragment(
    {
      elements: [
      {
        type: "text-list",
        size: { width: 240, height: 100 },
        marker: "bullet",
        gap: 11,
        marker_gap: 13,
        items: [[{ text: "First" }], [{ text: "Second" }]],
      },
      ],
    },
    { width: 240, height: 100 },
  );

  assert.match(listHtml, /column-gap:13px/);
  assert.match(listHtml, /<span aria-hidden="true">•<\/span>/);
  assert.match(listHtml, /<li style="margin-top:11px;/);
});

import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

let renderer;
let temporaryDirectory;

test.before(async () => {
  temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "presenton-json-to-html-metadata-"),
  );
  const outputFile = path.join(temporaryDirectory, "renderer.mjs");

  await build({
    entryPoints: [path.resolve("lib/template-v2-json-to-html.ts")],
    outfile: outputFile,
    bundle: true,
    platform: "node",
    format: "esm",
    tsconfig: path.resolve("tsconfig.json"),
    logLevel: "silent",
  });

  renderer = await import(
    `${pathToFileURL(outputFile).href}?cache=${Date.now()}`
  );
});

test.after(async () => {
  if (temporaryDirectory) {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});

test("renders PPTX table borders, vector metadata, flips, and chart legend position", () => {
  const html = renderer.templateV2UiToHtmlFragment(
    {
      elements: [
      {
        type: "table",
        flip_v: true,
        size: { width: 200, height: 80 },
        columns: [
          {
            runs: [{ text: "Header" }],
            borders: {
              top: { color: "#112233", width: 2, dash: [4, 2] },
              bottom: { color: "#FF9999", width: 0 },
            },
          },
        ],
        rows: [],
      },
      {
        type: "vector",
        name: "connector",
        flip_h: true,
        points: [{ x: 0, y: 100 }, { x: 160, y: 100 }],
        stroke: {
          color: "#384351",
          width: 6,
          dash: [0, 12],
          line_cap: "round",
          line_join: "bevel",
          start_marker: { type: "oval", length: "lg", width: "lg" },
        },
      },
      {
        type: "chart",
        chart_type: "line",
        text_color: "#475467",
        legend_position: "left",
        size: { width: 320, height: 180 },
        series: [{ name: "Revenue", values: [10, 20] }],
        categories: ["2025", "2026"],
      },
      ],
    },
    { width: 640, height: 360 },
  );

  assert.match(html, /transform:scaleY\(-1\)/);
  assert.match(html, /border-top:2px dashed #112233/);
  assert.match(html, /border-bottom:0px solid #FF9999/);
  assert.match(html, /transform:scaleX\(-1\)/);
  assert.match(html, /stroke-dasharray="0 12"/);
  assert.match(html, /stroke-linecap="round"/);
  assert.match(html, /stroke-linejoin="bevel"/);
  assert.match(html, /marker-start="url\(#vector-marker-/);
  assert.match(html, /markerWidth="30" markerHeight="30"/);
  assert.match(html, /&quot;position&quot;:&quot;left&quot;/);
  assert.match(html, /&quot;color&quot;:&quot;#475467&quot;/);
});

test("allocates unsized flex children like the Konva renderer", () => {
  const html = renderer.templateV2UiToHtmlFragment(
    {
      elements: [
        {
          type: "flex",
          position: { x: 0, y: 0 },
          size: { width: 280.83, height: 133.62 },
          direction: "row",
          align_items: "flex-start",
          gap: 17.25,
          children: [
            {
              type: "image",
              data: "/static/images/replaceable_template_image.png",
              fit: "cover",
            },
            {
              type: "flex",
              direction: "column",
              align_items: "flex-start",
              gap: 20.31,
              children: [
                {
                  type: "text",
                  size: { width: 136.34, height: 38.85 },
                  runs: [{ text: "Understanding Behavioral" }],
                  font: { size: 16, family: "Montserrat Bold" },
                },
                {
                  type: "text",
                  size: { width: 126.5, height: 63.01 },
                  runs: [{ text: "Description" }],
                  font: { size: 10.67, family: "Montserrat" },
                },
              ],
            },
          ],
        },
      ],
    },
    { width: 1280, height: 720 },
  );

  assert.match(
    html,
    /width:127\.2\d*px;height:133\.62px;[^>]*object-fit:cover/,
  );
  assert.match(
    html,
    /width:136\.34px;height:133\.62px;[^>]*display:flex;flex-direction:column/,
  );
});

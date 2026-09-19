import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

let mod;
let dir;

test.before(async () => {
  dir = await mkdtemp(path.join(tmpdir(), "presenton-motion-duration-"));
  const outfile = path.join(dir, "motion-duration.mjs");
  await build({
    entryPoints: [path.resolve("components/slide-editor/images/motion-duration.ts")],
    outfile,
    bundle: true,
    platform: "node",
    format: "esm",
    logLevel: "silent",
  });
  mod = await import(pathToFileURL(outfile).href);
});

test.after(async () => {
  if (dir) await rm(dir, { recursive: true, force: true });
});

const words = (n) => Array.from({ length: n }, () => "word").join(" ");

test("no script means no suggestion", () => {
  assert.equal(mod.suggestMotionDuration(undefined), null);
  assert.equal(mod.suggestMotionDuration("   \n "), null);
});

test("narration time is estimated at ~150 words per minute", () => {
  const s = mod.suggestMotionDuration(words(50));
  assert.equal(s.units, 50);
  assert.equal(s.narrationSeconds, 20);
  assert.equal(s.seconds, 20);
});

test("suggestion is clamped to the allowed clip length", () => {
  const short = mod.suggestMotionDuration(words(2));
  assert.equal(short.seconds, mod.MIN_MOTION_SECONDS);
  assert.equal(short.capped, false);

  const long = mod.suggestMotionDuration(words(200));
  assert.equal(long.seconds, mod.MAX_MOTION_SECONDS);
  assert.equal(long.capped, true);
});

test("CJK scripts are counted by character", () => {
  const s = mod.suggestMotionDuration("这是一个很长的演讲稿内容".repeat(2));
  assert.ok(s.units >= 20);
  assert.ok(s.narrationSeconds > 4 && s.narrationSeconds < 8);
});

import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

let communityConfig;
let temporaryDirectory;

test.before(async () => {
  temporaryDirectory = await mkdtemp(path.join(tmpdir(), "presenton-community-config-"));
  const entryFile = path.join(temporaryDirectory, "entry.ts");
  const outputFile = path.join(temporaryDirectory, "bundle.mjs");
  await writeFile(
    entryFile,
    `export { isCommunityEnabled } from ${JSON.stringify(
      path.resolve("utils/community.ts")
    )};`
  );
  await build({
    entryPoints: [entryFile],
    outfile: outputFile,
    bundle: true,
    platform: "node",
    format: "esm",
    tsconfig: path.resolve("tsconfig.json"),
    logLevel: "silent",
  });
  communityConfig = await import(
    `${pathToFileURL(outputFile).href}?cache=${Date.now()}`
  );
});

test.after(async () => {
  if (temporaryDirectory) {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});

test("community defaults to enabled", () => {
  assert.equal(communityConfig.isCommunityEnabled(undefined), true);
  assert.equal(communityConfig.isCommunityEnabled(""), true);
});

test("community accepts common disabled values", () => {
  for (const value of ["false", "FALSE", "0", "no", "off"]) {
    assert.equal(communityConfig.isCommunityEnabled(value), false);
  }
});

import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

let api;
let temporaryDirectory;

test.before(async () => {
  temporaryDirectory = await mkdtemp(path.join(tmpdir(), "presenton-blank-"));
  const outputFile = path.join(temporaryDirectory, "bundle.mjs");
  await build({
    entryPoints: [path.resolve("app/(presentation-generator)/services/api/presentation-generation.ts")],
    outfile: outputFile,
    bundle: true,
    platform: "node",
    format: "esm",
    tsconfig: path.resolve("tsconfig.json"),
    logLevel: "silent",
  });
  ({ PresentationGenerationApi: api } = await import(pathToFileURL(outputFile).href));
});

test.after(async () => {
  await rm(temporaryDirectory, { recursive: true, force: true });
});

for (const templateId of [undefined, "built-in-template", "custom-template"]) {
  test(`blank creation sends ${templateId ?? "no template"}`, async (t) => {
    const presentation = { id: "presentation-id", n_slides: 1 };
    const fetch = t.mock.method(globalThis, "fetch", async () =>
      Response.json(presentation, { status: 201 }),
    );

    assert.deepEqual(await api.createBlankPresentation(templateId), presentation);
    assert.equal(fetch.mock.callCount(), 1);
    const [url, options] = fetch.mock.calls[0].arguments;
    assert.ok(url.endsWith("/api/v1/ppt/presentation/create/blank"));
    assert.equal(options.method, "POST");
    assert.equal(options.headers["Content-Type"], "application/json");
    assert.equal(
      options.body,
      templateId ? JSON.stringify({ template_id: templateId }) : undefined,
    );
  });
}

test("invalid templates surface the backend error so the user can retry", async (t) => {
  t.mock.method(console, "error", () => {});
  t.mock.method(globalThis, "fetch", async () =>
    Response.json({ detail: "Template not found. Please use a valid template." }, { status: 400 }),
  );

  await assert.rejects(api.createBlankPresentation("missing"), /Template not found/);
});

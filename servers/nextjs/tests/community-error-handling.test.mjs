import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

let communityErrors;
let temporaryDirectory;

test.before(async () => {
  temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "presenton-community-errors-")
  );
  const entryFile = path.join(temporaryDirectory, "entry.ts");
  const outputFile = path.join(temporaryDirectory, "bundle.mjs");
  await writeFile(
    entryFile,
    [
      `export { ApiResponseError, ApiResponseHandler } from ${JSON.stringify(
        path.resolve("app/(presentation-generator)/services/api/api-error-handler.ts")
      )};`,
      `export { CommunityPresentationApi, getCommunityErrorState } from ${JSON.stringify(
        path.resolve("app/(presentation-generator)/services/api/community.ts")
      )};`,
    ].join("\n")
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
  communityErrors = await import(
    `${pathToFileURL(outputFile).href}?cache=${Date.now()}`
  );
});

test.after(async () => {
  if (temporaryDirectory) {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});

test("API response errors preserve backend message and retry metadata", async () => {
  const response = new Response(
    JSON.stringify({
      detail: {
        code: "community_service_timeout",
        message: "The Community service did not respond within 30 seconds.",
        retryable: true,
      },
    }),
    {
      status: 504,
      headers: { "Content-Type": "application/json" },
    }
  );

  await assert.rejects(
    communityErrors.ApiResponseHandler.handleResponse(
      response,
      "Could not load Community"
    ),
    (error) => {
      assert.ok(error instanceof communityErrors.ApiResponseError);
      assert.equal(
        error.message,
        "The Community service did not respond within 30 seconds."
      );
      assert.equal(error.status, 504);
      assert.equal(error.code, "community_service_timeout");
      assert.equal(error.retryable, true);
      return true;
    }
  );
});

test("Community requests turn browser network failures into actionable errors", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new TypeError("fetch failed");
  };

  try {
    await assert.rejects(
      communityErrors.CommunityPresentationApi.list(),
      (error) => {
        assert.ok(error instanceof communityErrors.ApiResponseError);
        assert.match(error.message, /Could not reach the Presenton server/);
        assert.equal(error.code, "community_backend_unreachable");
        assert.equal(error.retryable, true);
        return true;
      }
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Community UI state respects non-retryable backend errors", () => {
  const error = new communityErrors.ApiResponseError(
    "Community is disabled for this deployment.",
    {
      status: 404,
      code: "community_disabled",
      retryable: false,
    }
  );

  assert.deepEqual(
    communityErrors.getCommunityErrorState(error, "Fallback"),
    {
      message: "Community is disabled for this deployment.",
      retryable: false,
    }
  );
});

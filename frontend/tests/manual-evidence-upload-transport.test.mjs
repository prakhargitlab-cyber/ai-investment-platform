// Runtime defect closure (Defect A, clipboard-paste PNG upload corruption).
//
// The other manual-evidence frontend tests (manual-evidence-upload-ui.test.mjs)
// only assert on the SOURCE TEXT of investment-workspace.tsx/portfolio-api.ts
// (regex/string matches) -- they cannot catch a real runtime regression where
// a File/Blob gets serialized as text or object metadata instead of staying
// a binary object inside the FormData body. This file actually TRANSPILES
// and EXECUTES app/lib/portfolio-api.ts's real uploadEvidence()/request()
// functions against a mocked fetch, using real binary payloads (via Node's
// global File/Blob/FormData, available without jsdom), and asserts the exact
// bytes survive into the FormData entry fetch() would send.
//
// Root cause found for the real browser HTTP 500 investigated alongside this
// test: NOT in this frontend code (traced here and found byte-correct), and
// NOT in research-engine's Python code (also traced and found byte-correct).
// It was services/api-gateway's PortfolioRouteController.routeResearch(),
// which read the raw multipart request body as a UTF-8 String
// (StreamUtils.copyToString) before proxying it onward -- corrupting any
// byte sequence in a binary file part that isn't valid UTF-8. That is fixed
// separately in PortfolioRouteController.java (routeResearch now uses the
// existing byte-preserving forwardBytes(), already used by the portfolio
// import routes) and covered by a new Java integration test,
// ResearchMultipartByteIntegrityHttpIntegrationTest. This file instead
// proves the FRONTEND half of the transport chain (clipboard File and
// Browse-selected File alike) was never the problem, and remains correct.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

function transpile(relativePath) {
  const source = readFileSync(new URL(relativePath, import.meta.url), "utf8");
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText;
}

// app/config.ts has no imports of its own, so it loads with the same
// two-argument (module, exports) sandbox already used elsewhere in this
// test suite for manual-evidence-capability.ts.
const configModule = { exports: {} };
new Function("module", "exports", transpile("../app/config.ts"))(configModule, configModule.exports);

// app/lib/portfolio-api.ts imports "../config" -- supply a minimal `require`
// shim that resolves exactly that one specifier to the already-transpiled
// config module, rather than pulling in a bundler/ts-node dependency just
// for this test.
function fakeRequire(specifier) {
  if (specifier === "../config") return configModule.exports;
  throw new Error(`manual-evidence-upload-transport test: unexpected require("${specifier}")`);
}
const apiModule = { exports: {} };
new Function("module", "exports", "require", transpile("../app/lib/portfolio-api.ts"))(
  apiModule, apiModule.exports, fakeRequire
);
const { portfolioApi } = apiModule.exports;
assert.equal(typeof portfolioApi.uploadEvidence, "function");

const GLOBAL_INSTRUMENT_ID = "f86f7c59-85a6-40f2-a643-481c3ec2f53a";

// A real PNG signature plus arbitrary high-bit bytes -- not merely a mocked
// filename/MIME pair. 0x89, 0x8A, 0xFF etc. are not valid standalone UTF-8
// continuation bytes, so a regression that round-trips this payload through
// text (JSON.stringify(file), String(file), a UTF-8 decode/encode hop, or
// appending file.name/metadata instead of the binary object) would corrupt
// or lose these exact bytes, and this test would catch that.
function realPngBytes(seed) {
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const body = Buffer.alloc(64);
  for (let i = 0; i < body.length; i++) body[i] = (seed + i * 37) % 256;
  return Buffer.concat([signature, body]);
}

async function captureUploadedFormDataEntry(file) {
  const originalFetch = globalThis.fetch;
  let capturedInit = null;
  globalThis.fetch = async (_url, init) => {
    capturedInit = init;
    return new Response(JSON.stringify({ draftId: "00000000-0000-0000-0000-000000000000" }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };
  try {
    await portfolioApi.uploadEvidence(GLOBAL_INSTRUMENT_ID, "SHAREHOLDING", file);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.ok(capturedInit, "uploadEvidence must call fetch");
  assert.ok(capturedInit.body instanceof FormData, "uploadEvidence must send a FormData body");
  // Content-Type must be left for fetch to set (with its own multipart
  // boundary) -- a manually-set boundary is exactly the kind of mistake
  // that corrupts real browser multipart bodies.
  assert.ok(
    !capturedInit.headers || !("Content-Type" in capturedInit.headers),
    "uploadEvidence must not manually set Content-Type for a FormData body"
  );
  return capturedInit.body.get("file");
}

test("clipboard-pasted PNG (Snipping Tool paste shape) keeps its exact binary bytes through FormData", async () => {
  const bytes = realPngBytes(11);
  // Chrome's clipboard DataTransferItem.getAsFile() for a pasted screenshot
  // typically yields a File literally named "image.png" -- reproduce that
  // shape rather than a Browse-style filename.
  const clipboardFile = new File([bytes], "image.png", { type: "image/png" });

  const sentEntry = await captureUploadedFormDataEntry(clipboardFile);

  assert.ok(sentEntry instanceof Blob, "the FormData 'file' entry must still be a Blob/File, not a string");
  assert.notEqual(typeof sentEntry, "string");
  assert.equal(sentEntry.size, bytes.length);
  assert.equal(sentEntry.type, "image/png");
  const received = Buffer.from(await sentEntry.arrayBuffer());
  assert.ok(received.equals(bytes), "FormData must carry the exact original PNG bytes, unchanged");
  assert.deepEqual([...received.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
});

test("a normal Browse-selected PNG also keeps its exact binary bytes through FormData (clipboard fix does not break Browse)", async () => {
  const bytes = realPngBytes(197);
  const browsedFile = new File([bytes], "gokul-agro-shareholding.png", { type: "image/png" });

  const sentEntry = await captureUploadedFormDataEntry(browsedFile);

  assert.ok(sentEntry instanceof Blob);
  assert.equal(sentEntry.size, bytes.length);
  const received = Buffer.from(await sentEntry.arrayBuffer());
  assert.ok(received.equals(bytes), "Browse-selected file bytes must survive FormData exactly, same as clipboard paste");
  assert.deepEqual([...received.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
});

test("uploadEvidence targets the real draft endpoint with the exact query contract used by the real browser request", async () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl = null;
  globalThis.fetch = async (url, init) => {
    capturedUrl = url;
    return new Response(JSON.stringify({ draftId: "x" }), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    await portfolioApi.uploadEvidence(GLOBAL_INSTRUMENT_ID, "SHAREHOLDING", new File([realPngBytes(1)], "image.png", { type: "image/png" }));
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(
    capturedUrl,
    `/api/v1/research/evidence/draft?global_instrument_id=${GLOBAL_INSTRUMENT_ID}&evidence_type=SHAREHOLDING`
  );
});

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import ts from "typescript";
import { webcrypto } from "node:crypto";
const source = fs.readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8").replace('import { frontendConfig } from "../config";', 'const frontendConfig = { apiBaseUrl: "" };');
const mod = { exports: {} };
new Function("module", "exports", ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(mod, mod.exports);
const { createCorrelationId } = mod.exports;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
test("correlation ID uses native randomUUID with the Crypto receiver", () => {
  const crypto = { randomUUID() { assert.equal(this, crypto); return "native-id"; } };
  assert.equal(createCorrelationId(crypto), "native-id");
});
test("HTTP fallback sets UUID v4 version and variant while retaining random bytes", () => {
  const crypto = { getRandomValues(bytes) { assert.equal(this, crypto); assert.equal(bytes.length, 16); return bytes.fill(255); } };
  assert.equal(createCorrelationId(crypto), "ffffffff-ffff-4fff-bfff-ffffffffffff");
});
test("HTTP fallback produces distinct well-formed IDs with secure random values", () => {
  const crypto = { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) };
  const ids = Array.from({ length: 100 }, () => createCorrelationId(crypto));
  ids.forEach(id => assert.match(id, uuid));
  assert.equal(new Set(ids).size, 100);
});
test("missing secure randomness fails instead of falling back to Math.random", () => {
  assert.throws(() => createCorrelationId({}), /Secure randomness is unavailable/);
});

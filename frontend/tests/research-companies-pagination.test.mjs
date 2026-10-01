import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Post-commit review of 8eaed27: GET /api/v1/research/companies pagination
// contract. The backend (ai/research-engine/app/main.py) binds its page-size
// query parameter as `page_size` (FastAPI's Query() default is the literal
// Python parameter name; no camelCase alias is configured anywhere in that
// app). researchApi.listCompanies() was sending `pageSize` instead, which
// FastAPI silently ignores (page_size has a default, so this never raised a
// 422 -- it just always fell back to the backend's default of 50 regardless
// of what the caller asked for). No other caller in the repository invokes
// this endpoint, and no test previously exercised the custom-pageSize path,
// so nothing was silently broken for an existing consumer -- but the
// function as committed did not do what its own call signature promised.
const apiSource = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");

test("listCompanies sends the page-size query parameter as page_size, matching the backend's FastAPI Query() binding", () => {
  const start = apiSource.indexOf("listCompanies:");
  assert.notEqual(start, -1, "listCompanies not found in portfolio-api.ts");
  const end = apiSource.indexOf("getSummary:", start);
  const body = apiSource.slice(start, end);

  assert.match(body, /params\.set\("page_size", String\(pageSize\)\)/);
  // Guard against reintroducing the camelCase request-side mismatch. The
  // response envelope's "pageSize" field (used further down, in the return
  // type) is a different, correct thing -- this only checks the query string
  // construction for the *request*.
  assert.doesNotMatch(body, /params\.set\("pageSize"/);
});

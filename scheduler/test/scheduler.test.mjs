import assert from "node:assert/strict";
import test from "node:test";

import { dispatchWorkflow } from "../src/index.mjs";

test("dispatches the translation workflow on main", async () => {
  let request;
  await dispatchWorkflow(
    { GITHUB_WORKFLOW_TOKEN: "test-token" },
    async (url, options) => {
      request = { url, options };
      return { ok: true, status: 204 };
    },
  );

  assert.equal(
    request.url,
    "https://api.github.com/repos/toshiki-cyber/trump-truth-translator/actions/workflows/translate.yml/dispatches",
  );
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers.Authorization, "Bearer test-token");
  assert.equal(request.options.headers["X-GitHub-Api-Version"], "2026-03-10");
  assert.equal(request.options.body, '{"ref":"main"}');
});

test("fails when GitHub rejects the dispatch", async () => {
  await assert.rejects(
    dispatchWorkflow(
      { GITHUB_WORKFLOW_TOKEN: "test-token" },
      async () => ({ ok: false, status: 401 }),
    ),
    /401/,
  );
});

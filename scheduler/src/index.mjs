const WORKFLOW_DISPATCH_URL =
  "https://api.github.com/repos/toshiki-cyber/trump-truth-translator/actions/workflows/translate.yml/dispatches";

export async function dispatchWorkflow(env, fetchImpl = fetch) {
  const response = await fetchImpl(WORKFLOW_DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_WORKFLOW_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "trump-truth-translator-scheduler",
      "X-GitHub-Api-Version": "2026-03-10",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch failed: ${response.status}`);
  }

  console.log(JSON.stringify({ event: "workflow_dispatched", status: response.status }));
}

export default {
  async scheduled(_controller, env, ctx) {
    console.log(JSON.stringify({ event: "cron_started" }));
    ctx.waitUntil(
      dispatchWorkflow(env).catch((error) => {
        console.error(
          JSON.stringify({
            event: "workflow_dispatch_failed",
            error: error instanceof Error ? error.message : String(error),
          }),
        );
        throw error;
      }),
    );
  },
};

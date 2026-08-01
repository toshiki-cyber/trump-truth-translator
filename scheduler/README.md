# External scheduler

This Cloudflare Worker dispatches the existing `translate.yml` workflow every
five minutes. During the initial validation period, GitHub Actions' own
schedule remains enabled as a backup.

## One-time setup

1. Create a fine-grained GitHub personal access token restricted to this
   repository with **Actions: Write** permission.
2. Authenticate Wrangler with the intended Cloudflare account.
3. From this directory, set the token as a Worker secret:

   ```bash
   npx wrangler secret put GITHUB_WORKFLOW_TOKEN
   ```

4. Deploy the Worker:

   ```bash
   npx wrangler deploy
   ```

The token must never be added to this repository or printed in logs.

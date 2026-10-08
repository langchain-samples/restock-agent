# Link through the sandbox proxy

Restock uses Link CLI's device login and a user-owned `link-session` Connection.
Ordinary wallet commands send a placeholder token. LangSmith's native sandbox
proxy asks the app for the real header before forwarding each allowed request.
The app checks the signed request and the tool's permission before returning it.

The application handles login, renewal, saved sessions, and private payment-token
retrieval. Login and renewal temporarily use the CLI auth file; ordinary wallet
calls do not receive that file. This route does not use MDA-managed OAuth and
does not require registering an OAuth application.

## Configure a deployed app

For a new app, start with the [README quickstart](../README.md#quickstart).
Before changing an existing app's settings, finish or cancel any unpaid draft
through the normal conversation. Preserve submitted or uncertain orders.

From the repository directory:

```bash
uv sync --frozen
uv run --no-sync python scripts/setup.py --slack
uv run --no-sync python scripts/configure_proxy.py --url https://YOUR-DEPLOYMENT-HOST
```

Use this same app's public deployment origin, not its Studio link or the LangSmith
API URL. The script saves `RESTOCK_PROXY_URL`, creates or preserves
`RESTOCK_PROXY_SIGNING_KEY`, and selects `RESTOCK_LINK_TRANSPORT=proxy`. Keep
`LINK_SESSION_BACKEND=connection`. Existing office details, saved sessions,
notification settings, and deployment metadata are preserved.

For a first test, set `RESTOCK_MODE=link-test` privately in `.local/app/.env`:

```bash
uv run --no-sync python scripts/preflight.py
uv run mda deploy .local/app --name restock
```

In a new conversation, follow the [README prompts](../README.md#5-try-it-in-slack).
Confirm saved login reuse (or first login), Slack review, Link **test** approval,
and the final test confirmation. No merchant order should be submitted. Test a
second signed-in user and a later run after session expiry before wider use.
Existing live orders should be checked in their original conversation with the
original mode; do not convert one into a test order.

## Local validation

These checks make no real wallet or merchant calls:

```bash
uv sync --frozen
npm ci --ignore-scripts
uv run --no-sync python scripts/verify.py
```

They execute the real CLI and compiled MDA graph with fake Link, Zinc, Agent Auth,
and a proxy that implements the documented signed callback contract. They test
renewal, user separation, exact request matching, human approval, private token
cleanup, and no automatic replay after callback/provider failures. They do not
prove cloud routing, actual LangSmith signing, or real hosted session renewal.

For an interactive local conversation, set `RESTOCK_MODE=rehearsal`, run setup,
and use `uv run mda dev .local/app`. No callback configuration is needed in rehearsal.

A real proxy test from a local app also needs a stable public HTTPS callback URL,
a configured MDA deployment identity, and a resolved Connections principal.
`mda dev --tunnel` exposes a public URL, but a tunnel alone does not establish
those identities. Do not copy session JSON into local settings to bypass this.
Use the deployment flow above to check the actual cloud proxy in Link test mode.

## Configuration

The callback route is `/channels/link_proxy/events` and ships in both Studio and
Slack exports. It is separate from the optional Zinc notification callback.
`RESTOCK_PROXY_SIGNING_KEY` signs short-lived operation permissions; it is not a
Link credential. The callback also verifies LangSmith's request signature against
`<LANGSMITH_ENDPOINT>/.well-known/jwks.json` (the US endpoint by default).

The proxy config uses `full_request=true`, with no cached credentials. Do not add
a static `api.link.com` header rule: static rules take precedence and would bypass
the per-operation callback. An unreachable or rejected callback stops the command.
Fix the cause and reconcile an existing request before attempting another purchase.
An unexpected 401 is not automatically replayed; normal expiry is handled by the
separate read-only renewal step.

An optional `RESTOCK_LINK_TRANSPORT=session` setting authenticates each CLI command
with a temporary session file. It uses the same saved Connection. Select it
explicitly and redeploy between orders, after checking any uncertain outcome.
The app never switches transport automatically after a proxy failure.

This sample uses process-local locks and Store get/put. They are not distributed
transactions. Multiple workers need atomic permission claims and session-renewal
leases before production use. See [architecture](architecture.md).

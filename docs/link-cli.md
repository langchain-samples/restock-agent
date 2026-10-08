# Use Link CLI with Managed Deep Agents

Use [Link CLI](https://docs.stripe.com/agentic-commerce/link-cli) in a
[Managed Deep Agents sandbox](https://docs.langchain.com/langsmith/python/managed-deep-agents-sandboxes)
to connect a user's Link wallet and request approval for payments. A user-owned
Connection saves their CLI session. For ordinary wallet commands, the native
sandbox proxy adds the real authorization header while the CLI uses a placeholder.

This guide explains the pattern and the source you can reuse in another Python
MDA application. Restock is the working example; its Zinc merchant adapter and
shopping workflow are separate from the login and proxy code. For a complete
deployment, follow the [README quickstart](../README.md#quickstart).

The user signs in through Link CLI's device-login URL. This route does not require
you to register a separate OAuth application, and it does not use MDA-managed OAuth.

> These helpers are application source, not a standalone integration package.
> They use internal MDA APIs. Keep `managed-deepagents==0.8.1` and
> `@stripe/link-cli@0.22.0` pinned, and review the code and tests before upgrading.

## How login and wallet commands work

1. Your tool checks the current caller's saved session. If sign-in is needed,
   it returns a Link URL for the user to open.
2. After the user signs in, `link_finish_login` completes the device flow and
   saves the session in their user-owned Connection. Later conversations reuse it.
3. For a wallet command, the application permits one specific operation for that
   caller, sandbox, and session. Link CLI runs with a placeholder access token.
4. LangSmith's native proxy sends a signed request to the application's callback.
   The callback verifies it, reads the caller's Connection, and returns the real
   header directly to the proxy. It does not return credentials to the model.
5. When the access token is near expiry, Link CLI uses the saved refresh token
   to obtain a replacement. The helper saves the updated session before ordinary
   proxy commands continue.

Login and renewal still use a temporary CLI auth file in the sandbox. The helper
collects session changes and deletes that file, including on error paths. Ordinary
wallet commands do not receive the saved session file. A cleanup failure is an
error, not a successful operation. Signing in connects the wallet; each purchase
still needs its own approval in Link.

The Connection holds the CLI's session data, not just an API token. The callback
extracts the current access token before supplying the header to the proxy.

## Find the implementation

| File | Responsibility |
| --- | --- |
| [`restock/link_session.py`](../restock/link_session.py) | Device login, user-owned Connection storage, renewal capture, and temporary-file cleanup. |
| [`restock/proxy_wallet.py`](../restock/proxy_wallet.py) | Run allowed CLI commands with placeholders; renew an expired session before use. |
| [`restock/link_proxy.py`](../restock/link_proxy.py) | Match exact CLI operations and verify signed callback requests and permissions. |
| [`restock/proxy_runtime.py`](../restock/proxy_runtime.py) | Resolve the sandbox, current caller, Connection, and LangSmith signing keys. |
| [`restock/proxy_config.py`](../restock/proxy_config.py), [`channels/link_proxy.py`](../channels/link_proxy.py) | Configure the native proxy and serve its HTTP callback. |
| [`sandbox/__init__.py`](../sandbox/__init__.py), [`sandbox/setup.sh`](../sandbox/setup.sh) | Declare the sandbox and install the pinned CLI in its snapshot. |
| [`restock/wallet.py`](../restock/wallet.py), [`tools/restock.py`](../tools/restock.py) | Example payment operations, human review, and public tool results. |
| [`restock/tool_policy.py`](../restock/tool_policy.py) | Restrict the model to application tools and its instruction file. |

MDA supplies the caller identity. Studio uses the signed-in LangSmith user; the
native Slack channel resolves the message sender. A custom interface needs
[MDA identity](https://docs.langchain.com/langsmith/python/managed-deep-agents-identity)
configured for each person. A shared identity means a shared wallet session.
Never accept the credential owner as a model-supplied argument or use the sandbox
creator's identity in place of the requester.

## Configure the proxy

Start with a deployed MDA app that includes the callback and sandbox files above.
The sandbox image needs Node.js and npm. Its network settings allow HTTPS to
`api.link.com`, `login.link.com`, and `registry.npmjs.org`. The registry is used
to install the CLI and remains allowed by this sample's runtime configuration.

The callback must be reachable at the app's public HTTPS deployment URL. From
this repository, configure a runnable copy with:

```bash
uv run --no-sync python scripts/configure_proxy.py \
  --project .local/app \
  --url https://YOUR-DEPLOYMENT-HOST
```

Use your app's deployment origin, not its Studio URL or the LangSmith API URL.
The script saves `RESTOCK_PROXY_URL`, creates or preserves a private
`RESTOCK_PROXY_SIGNING_KEY`, and selects `RESTOCK_LINK_TRANSPORT=proxy`.
Restart or redeploy the app to activate these settings. The README covers the
first deployment and the sample's test/live modes.

The proxy uses `full_request=true`, so every matching request is checked and
returned credentials are not cached. The callback verifies LangSmith's signature
and the tool's permission before returning a header. Do not add a static header
rule for `api.link.com`: static rules take precedence and would bypass this check.
See the official [callback contract](https://docs.langchain.com/langsmith/sandbox-auth-proxy#callback-contract)
and the sample's [proxy setup guide](sandbox-proxy.md).

No manual `mda connections create` command is needed for Link. The helper creates
the session Connection during login. Calling the public `connections.get` accessor
on a missing secret would prompt for that secret; it would not start CLI device
login. Never ask users to paste session JSON or tokens.

## Reuse the pattern in another agent

Use this repository as a starting point, keeping the source modules and pinned
[dependencies](../pyproject.toml) together. Replace the shopping tools and instructions with your
application's workflow. If you extract individual helpers, follow their imports:
for example, `proxy_runtime.py` uses the shared `ServerStore` adapter in
`notification_runtime.py`. Copying only `link_session.py` does not add proxy support.

Register the helper's `link_login`, `link_finish_login`, and `link_logout` tools.
Show the returned login URL and call `link_finish_login` after the user completes
sign-in. Preserve an existing pending login instead of creating another one.

Write narrow tools for the Link operations your application needs. For example,
this read-only tool uses the proxy helpers from this repository after login:

```python
from langchain.tools import ToolRuntime, tool

from restock.proxy_wallet import ProxyRunner
from restock.storage import caller_for


@tool
async def check_link_wallet(runtime: ToolRuntime) -> dict:
    """Check whether the connected Link wallet has a payment method."""
    result = await ProxyRunner(runtime, caller_for(runtime)).run(["payment-methods", "list"])
    methods = result if isinstance(result, list) else result.get("payment_details", [])
    return {"has_payment_method": bool(methods)}
```

Register it alongside the login tools in your agent. It returns a public summary,
not payment-method details or credentials. It is an example built on this source,
not an API supplied by the MDA SDK.

`request_operation` in `link_proxy.py` permits only the commands used by this
sample. Add matching validation and tests when supporting another operation.
The payment path currently supports USD shared payment tokens; other credential
types, currencies, and merchant checkout methods need their own implementation.
`CliWallet` shows payment handling, but its order format is specific to Restock.

Keep model tool access restricted, as Restock does with `RestockToolPolicy`.
Adapt the allowed tools and instruction-file path for your application. Do not
expose unrestricted shell, file, or delegated-agent access to private session or
payment files. Keep wallet operations sequential; Restock also sets
`model_kwargs={"parallel_tool_calls": False}` on its OpenAI model.

The `LINK_CLI_TOOLS` collection includes a general `link_cli` tool that
uses the session-file path. It does not use `ProxyRunner` or enforce your purchase
workflow. Restock deliberately does not register it.

## Add application payments

Your application still supplies the merchant integration and purchase checks:

1. Build the proposed order and establish the amount the user will approve.
   For a Machine Payments Protocol (MPP) merchant, obtain its payment challenge
   for that exact request.
2. Require human review in application code before creating a Link spend request.
   Restock uses a LangGraph interrupt; approval is not a model tool argument.
3. Request Link approval for the reviewed amount and merchant or payment network.
   Show the approval URL before waiting for the user.
4. Read fresh approval from Link and check it against the saved order. Then
   retrieve the required payment credential inside the tool.
5. Submit to the merchant using its supported payment interface. Reuse one
   request ID (an idempotency key) and check uncertain outcomes instead of
   submitting another order.
6. Report a placed order only when the merchant confirms it. Link approval alone
   does not prove a purchase, shipment, or refund.

Restock retrieves its shared payment token into a private temporary file, reads
it inside the tool, and deletes it. CLI 0.22.0's `--output-file` handles cards only,
so this token path redirects command output to the private file. Return only a
public result to the model. Another merchant may require a different credential
and checkout implementation; Link login does not provide universal checkout.

## Configuration

Set these in the runnable project's private environment before deployment.
The `RESTOCK_` names belong to this sample's helpers, not the MDA SDK.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LINK_SESSION_CONNECTION` | `link-session` | Connection slug for each caller's session. Use a different slug for an independent application. |
| `LINK_CLIENT_NAME` | `Managed Deep Agent` | Name shown during CLI sign-in. |
| `LINK_SESSION_BACKEND` | `connection` | Required for proxy mode. The optional session transport also supports `store` for local development. |
| `RESTOCK_LINK_TRANSPORT` | `proxy` | Use the native proxy. `session` selects temporary session-file authentication for each command; failures never switch automatically. |
| `RESTOCK_PROXY_URL` | Unset | This app's public HTTPS origin serving the callback. |
| `RESTOCK_PROXY_SIGNING_KEY` | Unset | Private signing secret for application permissions, generated by `configure_proxy.py`. It is not a Link credential. |
| `RESTOCK_MODE` | `rehearsal` | Restock's operator-controlled mode: `rehearsal`, `link-test`, or `live`. |

`LINK_TEST_MODE` controls only the general `link_cli` tool. Restock's wallet
uses `RESTOCK_MODE` and adds `--test` unless the mode is `live`. An application
calling `ProxyRunner` directly must enforce its own test/live policy and approvals.

## Verify login, renewal, and payments

Start with the [Link-test flow](../README.md#5-try-it-in-slack). Confirm first login,
reuse in another conversation, human review, Link approval, and a test completion
with no merchant purchase. Test product searches may still incur merchant API fees.

**Session renewal** means replacing an expired access token using the saved
refresh token, without a new sign-in. To check it, let the saved access token
expire, then run an authenticated Link operation as the same user. In Restock,
repeat the Link-test checkout through approval. Checking an already-completed
test order or asking whether you are connected can return saved information
without making a Link API call, so neither proves renewal.

Confirm internally that the saved token expiry advanced, the refreshed session
was persisted, and the subsequent proxy request succeeded. Do not print tokens
or edit a real session to force expiry. Also test another user to confirm that
they connect their own wallet. An invalid or revoked refresh token can require
a new sign-in; the agent should report that rather than retry a payment.

The renewal check uses `payment-methods list`. Avoid using `user-info retrieve`
for this check with CLI 0.22.0: its profile parser can reject an address that omits
the optional second line, even after renewal succeeds.

Test approval stops before payment-token retrieval and merchant submission.
Validate those separately with an intentional live purchase. If the approval
wait times out, continue checking the same order instead of creating another.
See [troubleshooting](troubleshooting.md) for uncertain payment or order outcomes.

The sample uses process-local locks and Store reads/writes. Concurrent production
use needs atomic permission claims and session-renewal leases across workers.
Confirm hosted CLI login use with Stripe for your product. See
[architecture](architecture.md) for the payment checks and
[Contributing](../CONTRIBUTING.md) for tests with fake providers.

Keep the repository's [license](../LICENSE) and [source attribution](../README.md)
with reused code.

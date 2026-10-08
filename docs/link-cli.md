# Use Link CLI with Managed Deep Agents

Restock runs [Link CLI](https://docs.stripe.com/agentic-commerce/link-cli) in an
[MDA sandbox](https://docs.langchain.com/langsmith/managed-deep-agents-sandboxes).
Each person signs in to their own wallet. A user-owned connection stores their
CLI session so later conversations can reuse it.

This guide explains the Link-specific implementation and what to reuse in another
Python MDA project. For accounts, installation, deployment, and a complete shopping
conversation, follow the [README quickstart](../README.md#quickstart).

> The session helper is experimental application code. It uses internal MDA APIs,
> so keep `managed-deepagents==0.8.1` and `@stripe/link-cli@0.22.0` pinned when
> reusing it. Review the helper and its tests before upgrading.

## Find the implementation

| File | Responsibility |
| --- | --- |
| [`restock/link_session.py`](../restock/link_session.py) | Sign-in, per-user connection storage, session renewal capture, and temporary-file cleanup. |
| [`restock/wallet.py`](../restock/wallet.py) | Narrow CLI operations for payment methods, spend requests, and private payment-token retrieval. |
| [`tools/restock.py`](../tools/restock.py) | Model-facing tools, sign-in prompts, and continuation of the saved shopping request. |
| [`sandbox/__init__.py`](../sandbox/__init__.py) | Sandbox declaration and network allow list. |
| [`sandbox/setup.sh`](../sandbox/setup.sh) | Install the pinned CLI when MDA builds the sandbox snapshot. |
| [`agent.py`](../agent.py) | Register Restock tools and disable parallel model tool calls. |

## Connect a user's wallet

Restock checks the user's Link login while preparing an order. If sign-in is
needed, the agent shows a URL. The user opens it, signs in to Link, and replies
**Done** in the same conversation. Restock completes sign-in and continues the
saved order through payment review.

No manual `mda connections create` command is required for Link. The helper
creates the `link-session` connection and attaches the current caller's session
as a user-owned secret. Link CLI handles sign-in and renewal; the helper handles
persistence. Signing in does not approve a payment.

MDA supplies the caller identity. Studio uses the signed-in LangSmith user;
the native Slack channel resolves the message sender. For a custom interface,
configure [MDA identity](https://docs.langchain.com/langsmith/managed-deep-agents-identity)
for each person. A shared authenticated identity means a shared wallet session.
Never accept the credential owner as a model-supplied tool argument.

## How a command uses the session

The helper's `Session` context manager:

1. Acquires a process-local lock for the caller and loads their saved session.
2. Writes the session to a temporary auth file in the sandbox.
3. Runs Link CLI with `LINK_AUTH_FILE` pointing to that file.
4. Saves updated session data if the CLI renewed it.
5. Attempts file deletion and releases the lock, including when saving fails.

If the backend cannot delete the file, cleanup tries a shell deletion. A failed
fallback raises an error. Treat that error as an incomplete cleanup, not a
successful operation.

`CliWallet.run` in [`restock/wallet.py`](../restock/wallet.py) uses this pattern:

```python
async def run(self, *args: str) -> Any:
    async with Session(self.runtime, self.caller) as session:
        if session.saved is None:
            raise RestockError("link_login_required")
        code, result, _ = await session.cli(*args)
        if code != 0 or result is None:
            raise RestockError("link_command_failed")
        return result
```

This is a method excerpt from the sample, not a standalone tool. Restock passes
MDA's tool runtime and the caller resolved by `caller_for(runtime)` into
`CliWallet`. Its service layer returns a public order summary to the model.
Payment-token retrieval uses a separate private path in `CliWallet.token`.

The helper reads missing sessions without interrupting so it can start CLI
sign-in. Replacing that check with `connections.get("link-session", {"type": "user"})`
would request a missing secret through MDA's authorization interrupt. That
lookup does not start Link CLI sign-in. Do not ask users to paste session data.

## Reuse the session helper in another agent

The session layer does not depend on Zinc or the office-shopping workflow:

1. Copy [`restock/link_session.py`](../restock/link_session.py) into your project.
   Preserve its package path or update your imports. It depends on LangChain and
   MDA; use the relevant dependencies from [`pyproject.toml`](../pyproject.toml),
   keeping MDA pinned.
2. Merge the sandbox declaration and setup script into your project. The sandbox
   image must include Node.js and npm. Permit HTTPS access to `api.link.com`,
   `login.link.com`, and `registry.npmjs.org`. This sample's allow list also applies
   at runtime, so npm registry access remains allowed after installation.
3. Register the helper's `link_login`, `link_finish_login`, and `link_logout`
   tools alongside your application's tools. Show the returned sign-in URL and
   call `link_finish_login` after the user completes sign-in.
4. Write narrow application tools that use `Session` for Link commands. Validate
   the amount and merchant in application code, and return only the fields the
   model needs. Use `CliWallet` as a reference; its order format and payment-token
   handling are specific to Restock's checkout flow.
5. Merge the relevant approval rules from [`instructions.md`](../instructions.md)
   into your agent's instructions. Start with test spend requests and require
   the user's approval in Link before checkout.

The generic helper also exports `LINK_CLI_TOOLS`, including a general `link_cli`
tool. Restock registers `RESTOCK_TOOLS` instead and routes payments through its
reviewed order workflow. Adding the generic CLI tool to Restock would let the
model request operations outside that workflow.

Restock's OpenAI model sets `model_kwargs={"parallel_tool_calls": False}`. Keep
Link interactions sequential, using your provider's equivalent setting. The
helper's lock also serializes calls across conversations within one process.
Neither setting coordinates separate server processes; concurrent production
use needs a distributed lock or lease around session refresh and storage.

## Configuration

Set configuration in the runnable project's environment before deployment.
For Restock, that is `.local/app/.env`; follow the README when redeploying.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LINK_SESSION_CONNECTION` | `link-session` | Connection slug for each caller's CLI session. Choose a separate slug for an independent application. |
| `LINK_CLIENT_NAME` | `Managed Deep Agent` | Client name passed to Link CLI during sign-in. |
| `LINK_SESSION_BACKEND` | `connection` | Use `store` for local development to keep sessions in the caller-scoped LangGraph Store. |
| `RESTOCK_MODE` | `rehearsal` | Restock's operator-controlled mode: `rehearsal`, `link-test`, or `live`. |

`LINK_TEST_MODE` applies only to the generic helper's `link_cli` tool. It does
not control Restock's `CliWallet`. Restock uses `RESTOCK_MODE` and adds `--test`
to spend requests unless that mode is `live`. If you reuse `Session` directly,
your own application must enforce its test/live policy.

## Verify sign-in and test approval

Follow the [Slack test flow](../README.md#5-try-it-in-slack) with
`RESTOCK_MODE=link-test`. Expected behavior:

- The first checkout asks you to sign in to Link. Replying **Done** resumes the
  saved shopping request rather than starting a new one.
- A later conversation for the same caller reuses a valid saved login. Each new
  purchase still needs its own review and Link payment approval.
- After test approval, Restock confirms that nothing was purchased. Test requests
  issue no payment credential; Zinc product searches still use your Zinc balance.
- In `rehearsal` mode, products and approvals are fictional and no wallet is accessed.

If approval polling ends, continue in the same thread with **I approved it.
Can you check this order?** Use [troubleshooting](troubleshooting.md) for failed
sign-in, connection errors, or uncertain payment and order status.

## Before using live payments

Link approval is not merchant order confirmation. A checkout tool must verify
fresh approval and bind it to the reviewed order and amount before using a
payment credential. Keep card details or payment tokens inside the tool and
return only a public result. Restock uses a shared payment token for Zinc; other
merchants can require a different credential type and checkout implementation.

Restock's `CliWallet.token` retrieves the token into a private temporary file,
reads it inside the tool, and deletes it. Link CLI 0.22.0's `--output-file` handles
cards only, so the token path redirects CLI output to a file. Do not substitute
the generic CLI tool for this private handoff. Sandbox file and shell tools can
read files there; a file path alone is not an isolation boundary.

Confirm hosted CLI sign-in use with Stripe before relying on it for a public
product. Preserve caller identity, approval checks, idempotency, and recovery
when adapting the sample. For Restock's complete payment flow and production
limits, see [architecture](architecture.md). For its fake-provider checks, see
[Contributing](../CONTRIBUTING.md).

Keep the repository's [license](../LICENSE) and [source attribution](../README.md)
with any code you reuse.

"""Link CLI in the sandbox with the CLI's own device login, persisted per user.

`link-cli auth login` uses Stripe's built-in public client, so no OAuth client
registration is needed. The CLI keeps tokens in an auth file and refreshes them
itself. A sandbox belongs to one conversation and is deleted when idle, so these
tools keep each signed-in user's auth file in a user-owned MDA Connection (an
opaque secret under the slug in LINK_SESSION_CONNECTION, default "link-session")
and place it in the sandbox only for the duration of a command. Optional reads,
writes and rotations use Agent Auth under MDA's resolved caller. Tokens never
enter tool results.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import secrets
import shlex
import time
import weakref
from typing import Any

from langchain.tools import ToolRuntime, tool
from managed_deepagents import CredentialStoreError

NAMESPACE = "link_cli_auth"
AUTH_DIR = "/tmp/mda-link"
OUTPUT_DIR = "/workspace"
CLI = "link-cli"
FINISH_POLL_SECONDS = 3
FINISH_POLL_ATTEMPTS = 15
BLOCKED_COMMANDS = {"auth", "onboard", "demo", "serve", "mcp", "skills", "completions", "mpp"}
# --approve and --approval-detail approve on the user's behalf; only the Link app may approve.
BLOCKED_FLAGS = {
    "--auth",
    "--mcp",
    "--update",
    "--llms",
    "--llms-full",
    "--approve",
    "--approval-detail",
}
SENSITIVE_INCLUDES = ("card", "shared_payment_token", "link_pay_token")
REDACT_KEYS = {"access_token", "refresh_token", "credentials_path", "number", "cvc"}


def client_name() -> str:
    return os.environ.get("LINK_CLIENT_NAME", "Managed Deep Agent")


def test_mode() -> bool:
    """Spend requests must carry --test unless the operator sets LINK_TEST_MODE=0."""
    value = os.environ.get("LINK_TEST_MODE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


# Identity, storage and sandbox plumbing --------------------------------------------
def _caller(runtime: ToolRuntime) -> str | None:
    info = getattr(runtime, "server_info", None)
    principal = getattr(info, "principal", None)
    principal_id = getattr(principal, "id", None)
    if principal_id:
        kind = getattr(principal, "kind", "user")
        return hashlib.sha256(f"{kind}:{principal_id}".encode()).hexdigest()
    configurable = (getattr(runtime, "config", None) or {}).get("configurable", {})
    user = configurable.get("langgraph_auth_user")
    identity = getattr(user, "identity", None) or (
        user.get("identity") if isinstance(user, dict) else None
    )
    return hashlib.sha256(f"user:{identity}".encode()).hexdigest() if identity else None


EMPTY_SESSION = json.dumps({"auth": None, "pendingDeviceAuth": None})


def has_session(value: str | None) -> bool:
    """True when the auth file holds tokens or a login in progress."""
    if not value:
        return False
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and bool(data.get("auth") or data.get("pendingDeviceAuth"))


class ConnectionSessions:
    """Each user's CLI auth file as a user-owned secret in an MDA Connection.

    Optional reads use MDA's internal Agent Auth client. The public accessor
    interrupts for a missing secret, but this helper must start CLI device login
    instead. Reads retain the same caller ownership checks. Writes use Agent
    Auth's connections API, the same endpoint MDA documents for storing a
    user-owned API key, addressed to the principal MDA itself resolves for the run:
    ``POST /v1/agent-auth/connections`` creates the slot and attaches the caller's
    secret; an existing secret is replaced with ``PATCH /v1/agent-auth/credentials/{id}``.
    """

    def __init__(
        self, slug: str | None = None, *, agent_auth=None, display_name="Link CLI session"
    ) -> None:
        self.slug = slug or os.environ.get("LINK_SESSION_CONNECTION", "link-session")
        self._agent_auth = agent_auth  # tests inject a client factory
        self.display_name = display_name

    async def load(self, runtime: ToolRuntime) -> str | None:
        value = await asyncio.to_thread(self._read, runtime)
        return value if has_session(value) else None

    def _read(self, runtime: ToolRuntime) -> str | None:
        client = self._client(runtime)
        connection = client.find_workspace_connection(self.slug)
        if connection is None:
            return None
        if connection.kind != "secret":
            raise CredentialStoreError("Link session Connection must be an opaque secret")
        # Missing caller grant means no login yet. Permission and service errors
        # propagate; never fall back to another user or an agent-owned credential.
        return client.read_user_secret_for_connection(connection)

    def _client(self, runtime: ToolRuntime):
        if self._agent_auth is not None:
            return self._agent_auth(runtime)
        # MDA's own accessor resolves config and principal this way for connections.get.
        from managed_deepagents import _connections as mda_connections
        from managed_deepagents._agent_auth import AgentAuth, AgentAuthContext
        from managed_deepagents._identity_runtime import (
            build_runtime_identity,
            resolve_runtime_configurable,
        )

        config = mda_connections._resolve_agent_auth_config(None)
        identity = build_runtime_identity(resolve_runtime_configurable(runtime))
        principal_id = mda_connections.resolve_credential_principal_id(identity, config)
        return AgentAuth(
            AgentAuthContext(
                base_url=config.base_url,
                principal_id=principal_id,
                agent_id=config.deployment_id,
                api_key=config.api_key,
                workspace_id=config.workspace_id,
            )
        )

    def _write(self, runtime: ToolRuntime, value: str) -> None:
        client = self._client(runtime)
        owner = client.context.principal_id
        status, body = client._request(
            "POST",
            "/v1/agent-auth/connections",
            body={
                "slug": self.slug,
                "display_name": self.display_name,
                "credential": {
                    "kind": "secret",
                    "owner_type": "user",
                    "owner_id": owner,
                    "value": value,
                },
            },
        )
        if status == 409 or status == 400:
            row = client.find_workspace_connection(self.slug)
            credential_id = (
                client._credential_id_for_owner(row.connection_id, "user", owner) if row else None
            )
            if credential_id is None:
                raise CredentialStoreError(
                    "Link session slot exists but the caller's credential was not found",
                    status,
                    body,
                )
            status, body = client._request(
                "PATCH", f"/v1/agent-auth/credentials/{credential_id}", body={"secret": value}
            )
        if status >= 400:
            raise CredentialStoreError("Agent Auth rejected the Link session write", status, {})
        if client.read_user_secret(self.slug) != value:
            raise CredentialStoreError("Link session write could not be verified")

    async def save(self, runtime: ToolRuntime, value: str) -> None:
        await asyncio.to_thread(self._write, runtime, value)

    async def clear(self, runtime: ToolRuntime) -> None:
        await asyncio.to_thread(self._write, runtime, EMPTY_SESSION)


class StoreSessions:
    """Fallback for local `mda dev`: the per-user LangGraph Store instead of a Connection."""

    async def load(self, runtime: ToolRuntime) -> str | None:
        store = getattr(runtime, "store", None)
        if store is None:
            return None
        item = await store.aget((NAMESPACE, _caller(runtime)), "auth")
        value = item.value.get("auth") if item is not None else None
        return value if has_session(value) else None

    async def save(self, runtime: ToolRuntime, value: str) -> None:
        store = getattr(runtime, "store", None)
        if store is not None:
            await store.aput(
                (NAMESPACE, _caller(runtime)),
                "auth",
                {"auth": value, "updated_at": int(time.time())},
            )

    async def clear(self, runtime: ToolRuntime) -> None:
        store = getattr(runtime, "store", None)
        if store is not None:
            await store.adelete((NAMESPACE, _caller(runtime)), "auth")


def default_sessions():
    if os.environ.get("LINK_SESSION_BACKEND", "connection").lower() == "store":
        return StoreSessions()
    return ConnectionSessions()


sessions = default_sessions()

# Two commands for one user at once would both refresh with the same refresh token: Link
# rejects the reused one, and whichever command saves last decides the stored login.
# Sessions for one user therefore run one at a time within this process.
_user_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _user_lock(caller: str) -> asyncio.Lock:
    locks = _user_locks.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(caller, asyncio.Lock())


class Session:
    """One tool call's view of the sandbox: place the auth file, run, collect, remove."""

    def __init__(self, runtime: ToolRuntime, caller: str) -> None:
        self.runtime = runtime
        self.caller = caller
        self.backend = getattr(runtime, "backend", None)
        self.path = f"{AUTH_DIR}/{secrets.token_hex(8)}.json"
        self.saved: str | None = None
        self.lock = _user_lock(caller)

    async def execute(self, command: str, *, timeout: int = 120) -> tuple[int | None, str]:
        result = await self.backend.aexecute(command, timeout=timeout)
        return result.exit_code, result.output or ""

    async def remove_private_file(self, path: str) -> None:
        try:
            result = await self.backend.adelete(path)
            if not result.error or result.error == "file_not_found":
                return
        except Exception:
            pass
        # Some backends return an error instead of raising. Do not silently leave
        # a session or payment file behind when that operation fails.
        code, _ = await self.execute("rm -f -- " + shlex.quote(path))
        if code != 0:
            raise CredentialStoreError("Private sandbox file cleanup failed")

    async def __aenter__(self) -> Session:
        await self.lock.acquire()
        try:
            self.saved = await sessions.load(self.runtime)
            await self.execute(
                f"mkdir -p {shlex.quote(AUTH_DIR)} && chmod 700 {shlex.quote(AUTH_DIR)}"
            )
            if self.saved is not None:
                written = await self.backend.awrite(self.path, self.saved)
                if written.error:
                    raise RuntimeError(f"could not place the Link auth file: {written.error}")
        except BaseException:
            try:
                await self.remove_private_file(self.path)
            finally:
                self.lock.release()
            raise
        return self

    async def cli(self, *args: str, timeout: int = 120) -> tuple[int | None, Any, str]:
        command = " ".join(
            [
                f"LINK_AUTH_FILE={shlex.quote(self.path)}",
                CLI,
                *(shlex.quote(a) for a in args),
                "--format",
                "json",
            ]
        )
        code, output = await self.execute(command, timeout=timeout)
        return code, parse_json(output), output

    async def __aexit__(self, *_exc: object) -> None:
        try:
            code, output = await self.execute(f"cat {shlex.quote(self.path)} 2>/dev/null")
            current = output.strip() if code == 0 and output.strip() else None
            if current and _valid_auth_json(current) and current != self.saved:
                await sessions.save(self.runtime, current)
        finally:
            try:
                await self.remove_private_file(self.path)
            finally:
                self.lock.release()


def _valid_auth_json(text: str) -> bool:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and ("auth" in data or "pendingDeviceAuth" in data)


def parse_json(text: str) -> Any:
    """The first JSON document in CLI output, exactly as the CLI printed it."""
    for start in sorted(i for i, char in enumerate(text) if char in "{["):
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            continue
    return None


def one(value: Any) -> Any:
    """Some CLI commands wrap a single object in a one-element list; unwrap for checks."""
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    return value


def final_status(value: Any) -> dict[str, Any] | None:
    """`auth status --interval N` prints one entry per attempt; the last one is the outcome."""
    if isinstance(value, list):
        entries = [entry for entry in value if isinstance(entry, dict)]
        return entries[-1] if entries else None
    return value if isinstance(value, dict) else None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if k in REDACT_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def storage_error(error: Exception) -> dict[str, Any]:
    """A safe tool result for a failed Connection read or write."""
    return {
        "status": "error",
        "message": "Saving or reading the Link session in MDA Connections failed. Try again after checking the Connection service.",
    }


def _preconditions(runtime: ToolRuntime) -> tuple[str, None] | tuple[None, dict[str, Any]]:
    if getattr(runtime, "backend", None) is None:
        return None, {
            "status": "sandbox_required",
            "message": "This agent needs its sandbox to run link-cli.",
        }
    caller = _caller(runtime)
    if caller is None:
        return None, {"status": "sign_in_required", "message": "This agent needs a signed-in user."}
    return caller, None


def _split_flags(args: list[str]) -> list[str]:
    """`--flag=value` as `--flag value`, the way the CLI reads it, so checks see both forms."""
    split: list[str] = []
    for arg in args:
        split.extend(arg.split("=", 1) if arg.startswith("--") and "=" in arg else [arg])
    return split


def _values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def validate_command(command: str) -> tuple[list[str] | None, str | None]:
    try:
        args = shlex.split(command)
    except ValueError as error:
        return None, f"Could not parse the command: {error}"
    if args and args[0] == CLI:
        args = args[1:]
    if not args:
        return None, "Give a link-cli command such as 'user-info retrieve'."
    if args[0] in BLOCKED_COMMANDS and not (args[0] == "auth" and args[1:2] == ["status"]):
        return None, f"'{args[0]}' is handled by link_login and link_logout, not link_cli."
    checked = _split_flags(args)
    if any(a in BLOCKED_FLAGS or a.startswith("--format") for a in checked):
        return None, (
            "Do not pass --auth, --approve, --approval-detail, --mcp, --update, --llms or "
            "--format. The tool sets the output format, and only the user approves, in Link."
        )
    output_files = _values(checked, "--output-file")
    if any(not posixpath.normpath(path).startswith(f"{OUTPUT_DIR}/") for path in output_files):
        return None, f"--output-file must point under {OUTPUT_DIR}/."
    includes = " ".join(_values(checked, "--include"))
    if any(word in includes for word in SENSITIVE_INCLUDES) and not output_files:
        return None, "Retrieving a card or token requires --output-file so it stays out of chat."
    if args[0] == "spend-request" and "create" in args and test_mode():
        if "--test" not in args or any(a.startswith(("--test=", "--no-test")) for a in args):
            return None, (
                "Test mode is on: spend-request create needs --test. The operator sets "
                "LINK_TEST_MODE=0 to allow real payments."
            )
    return args, None


# Tools -------------------------------------------------------------------------------
@tool(parse_docstring=True)
async def link_login(runtime: ToolRuntime) -> dict[str, Any]:
    """Check whether the user is signed in to Link, and start the sign-in if not.

    Returns connected, or login_required with a verification_url to show the user.
    After they approve in the Link app, call link_finish_login.
    """
    caller, problem = _preconditions(runtime)
    if problem:
        return problem
    try:
        return await _login(runtime, caller)
    except CredentialStoreError as error:
        return storage_error(error)


async def _login(runtime: ToolRuntime, caller: str) -> dict[str, Any]:
    async with Session(runtime, caller) as session:
        if session.saved is not None:
            code, status, _ = await session.cli("auth", "status")
            status = one(status)
            if code == 0 and isinstance(status, dict) and status.get("authenticated"):
                return {
                    "status": "connected",
                    "scope": status.get("scope"),
                    "message": "Link is connected for this user.",
                }
            if (
                code == 0
                and isinstance(status, dict)
                and status.get("pending")
                and status.get("verification_url")
            ):
                return {
                    "status": "login_required",
                    "verification_url": status["verification_url"],
                    "phrase": status.get("phrase"),
                    "message": "Use this existing login link. After approval, call link_finish_login.",
                }
        code, started, raw = await session.cli("auth", "login", "--client-name", client_name())
        started = one(started)
        if code != 0 or not isinstance(started, dict) or not started.get("verification_url"):
            return {
                "status": "error",
                "message": "link-cli auth login failed.",
                "output": "Link login could not be started.",
            }
        return {
            "status": "login_required",
            "verification_url": started["verification_url"],
            "phrase": started.get("phrase"),
            "message": "Show the URL to the user. After they approve in the Link app, call link_finish_login.",
        }


@tool(parse_docstring=True)
async def link_finish_login(runtime: ToolRuntime) -> dict[str, Any]:
    """Complete a Link sign-in started by link_login. Waits up to 45 seconds for approval."""
    caller, problem = _preconditions(runtime)
    if problem:
        return problem
    try:
        return await _finish_login(runtime, caller)
    except CredentialStoreError as error:
        return storage_error(error)


async def _finish_login(runtime: ToolRuntime, caller: str) -> dict[str, Any]:
    async with Session(runtime, caller) as session:
        if session.saved is None:
            return {"status": "login_required", "message": "Call link_login first."}
        code, status, raw = await session.cli(
            "auth",
            "status",
            "--interval",
            str(FINISH_POLL_SECONDS),
            "--max-attempts",
            str(FINISH_POLL_ATTEMPTS),
            timeout=FINISH_POLL_SECONDS * FINISH_POLL_ATTEMPTS + 30,
        )
        status = final_status(status)
        if status is not None and status.get("authenticated"):
            return {
                "status": "connected",
                "scope": status.get("scope"),
                "message": "Link is connected for this user.",
            }
        return {
            "status": "pending",
            "message": "Not approved yet. Ask the user to finish in the Link app, then call again.",
        }


@tool(parse_docstring=True)
async def link_cli(command: str, runtime: ToolRuntime) -> dict[str, Any]:
    """Run a link-cli command as the signed-in user and return its JSON result.

    Args:
        command: The arguments after `link-cli`, for example `user-info retrieve` or
            `spend-request create --amount 525 --context "..." --merchant-name "..." --merchant-url https://... --test`.
    """
    args, error = validate_command(command)
    if error:
        return {"status": "invalid", "message": error}
    caller, problem = _preconditions(runtime)
    if problem:
        return problem
    try:
        async with Session(runtime, caller) as session:
            if session.saved is None:
                return {"status": "login_required", "message": "Call link_login first."}
            code, parsed, raw = await session.cli(*args)
    except CredentialStoreError as error:
        return storage_error(error)
    if code != 0:
        return {
            "status": "error",
            "exit_code": code,
            "output": "Link CLI failed. No private command output is returned.",
        }
    return {"status": "ok", "result": redact(parsed) if parsed is not None else "No JSON result."}


@tool(parse_docstring=True)
async def link_logout(runtime: ToolRuntime) -> dict[str, Any]:
    """Sign the user out of Link and forget their saved session."""
    caller, problem = _preconditions(runtime)
    if problem:
        return problem
    try:
        async with Session(runtime, caller) as session:
            if session.saved is not None:
                await session.cli("auth", "logout")
            session.saved = None
        await sessions.clear(runtime)
    except CredentialStoreError as error:
        return storage_error(error)
    return {"status": "disconnected", "message": "Link session removed for this user."}


LINK_CLI_TOOLS = [link_login, link_finish_login, link_cli, link_logout]

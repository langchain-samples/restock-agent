"""Run narrow Link commands with placeholder credentials and native proxy injection."""

import json
import secrets
import shlex
import time

from restock import link_session
from restock.config import RestockError
from restock.link_proxy import operation, request_operation
from restock.proxy_runtime import context_for


def fresh(saved):
    try:
        auth = json.loads(saved or "{}").get("auth") or {}
        return (
            bool(auth.get("access_token")) and auth.get("expires_at", 0) > (time.time() + 60) * 1000
        )
    except (ValueError, TypeError, AttributeError):
        return False


class ProxyRunner:
    def __init__(self, runtime, caller):
        self.runtime, self.caller = runtime, caller
        self.backend = runtime.backend

    async def run(self, args, *, private=False):
        spec = request_operation(args)
        context = await context_for(self.runtime)
        saved = await link_session.sessions.load(self.runtime)
        if not saved or not json.loads(saved).get("auth"):
            raise RestockError("link_login_required")
        if not fresh(saved):
            # CLI owns refresh; never call auth login here (it may restart consent).
            async with link_session.Session(self.runtime, self.caller) as session:
                if not fresh(session.saved):
                    async with context.permissions.allow(
                        context.sandbox_id,
                        context.principal_id,
                        context.slug,
                        session.saved,
                        operation("GET", "/payment-details", lifecycle=True),
                    ):
                        # Use the wallet endpoint we actually need. CLI 0.22.0's
                        # profile parser rejects a valid address without line2.
                        code, _, _ = await session.cli("payment-methods", "list")
                        if code != 0:
                            raise RestockError("link_session_renewal_failed")
            # Session persists rotated tokens before wallet header injection starts.
        async with link_session._user_lock(self.caller):
            saved = await link_session.sessions.load(self.runtime)
            if not fresh(saved):
                raise RestockError("link_session_renewal_failed")
            async with context.permissions.allow(
                context.sandbox_id, context.principal_id, context.slug, saved, spec
            ) as placeholder:
                return await self._execute(args, placeholder, private=private)

    async def _execute(self, args, placeholder, *, private):
        # The nonexistent auth path ensures no accidental use of a sandbox login.
        path = f"{link_session.AUTH_DIR}/proxy-{secrets.token_hex(12)}.json"
        command = " ".join(
            [
                "env -u LINK_REFRESH_TOKEN",
                "LINK_ACCESS_TOKEN=" + shlex.quote(placeholder),
                "LINK_NO_REFRESH=1",
                "LINK_AUTH_FILE=" + shlex.quote(path + ".unused"),
                link_session.CLI,
                *(shlex.quote(arg) for arg in args),
                "--format json",
            ]
        )
        if private:
            command = (
                "umask 077; mkdir -p "
                + shlex.quote(link_session.AUTH_DIR)
                + " && "
                + command
                + " > "
                + shlex.quote(path)
                + " 2>/dev/null"
            )
        try:
            result = await self.backend.aexecute(command, timeout=120)
            if result.exit_code != 0:
                # No automatic replay, including 401 or callback/provider errors.
                raise RestockError("link_proxy_command_failed_check_existing_order")
            if private:
                downloads = await self.backend.adownload_files([path])
                if len(downloads) != 1 or downloads[0].error or downloads[0].content is None:
                    raise RestockError("payment_token_unavailable")
                value = link_session.parse_json(downloads[0].content.decode())
            else:
                value = link_session.parse_json(result.output or "")
            if value is None:
                raise RestockError("link_command_failed")
            return value
        finally:
            if private:
                # Reuse existing cleanup fallback, without entering a file Session.
                await link_session.Session(self.runtime, self.caller).remove_private_file(path)

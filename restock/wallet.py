"""Link device-session wallet through the sandbox CLI. Never expose payment tokens."""

from __future__ import annotations

import secrets
import shlex
from typing import Any

from restock.config import RestockError
from restock.link_session import AUTH_DIR, Session, one
from restock.service import order_fee, payment_amount

FLOW = "mda_restock_v1"


def create_args(order: dict, payment_method: str) -> list[str]:
    amount = payment_amount(order)
    fee = order_fee(order)
    context = (
        f"Restock order {order['id']}: purchase the reviewed office supplies through Zinc. "
        f"Approve the chosen upfront amount of {amount} US cents including Zinc's API fee. "
        f"The shopping budget is {order['budget_cents']} US cents, separate from this payment. "
        "The final retailer total, tax and shipping are unknown; Zinc refunds unused money. "
        "This request is bound to the items and upfront amount you reviewed with Restock."
    )
    args = [
        "spend-request",
        "create",
        "--idempotency-key",
        order["id"],
        "--credential-type",
        "shared_payment_token",
        "--network-id",
        order["challenge"]["network_id"],
        "--payment-method-id",
        payment_method,
        "--amount",
        str(amount),
        "--currency",
        "usd",
        "--request-approval",
        "--context",
        context,
        "--line-item",
        f"name:Retailer allowance (final total unknown),quantity:1,unit_amount:{amount - fee}",
        "--total",
        f"type:fee,display_text:Zinc fees,amount:{fee}",
        "--total",
        f"type:total,display_text:Chosen upfront amount,amount:{amount}",
        "--metadata",
        f"restock_order:{order['id']}",
        "--metadata",
        f"restock_fingerprint:{order['fingerprint']}",
        "--metadata",
        f"restock_flow:{FLOW}",
    ]
    if order["mode"] != "live":
        args.append("--test")
    return args


class CliWallet:
    def __init__(self, runtime, caller: str):
        self.runtime, self.caller = runtime, caller

    async def run(self, *args: str) -> Any:
        async with Session(self.runtime, self.caller) as session:
            if session.saved is None:
                raise RestockError("link_login_required")
            code, result, _ = await session.cli(*args)
            if code != 0 or result is None:
                raise RestockError("link_command_failed")
            return result

    async def create(self, order: dict) -> dict:
        methods = await self.run("payment-methods", "list")
        if isinstance(methods, dict):
            methods = methods.get("payment_details", [])
        if not isinstance(methods, list):
            raise RestockError("link_payment_method_required")
        defaults = [method for method in methods if method.get("is_default")]
        selected = (
            defaults[0] if len(defaults) == 1 else (methods[0] if len(methods) == 1 else None)
        )
        if not selected or not isinstance(selected.get("id"), str):
            raise RestockError("choose_default_payment_method_in_link")
        return one(await self.run(*create_args(order, selected["id"])))

    async def retrieve(self, request_id: str) -> dict:
        return one(await self.run("spend-request", "retrieve", request_id))

    async def history(self) -> list[dict]:
        value = await self.run("spend-request", "list", "--include-history")
        return value if isinstance(value, list) else value.get("data", [])

    async def cancel(self, request_id: str) -> dict:
        return one(await self.run("spend-request", "cancel", request_id))

    async def token(self, request_id: str) -> tuple[dict, str]:
        path = f"{AUTH_DIR}/spt-{secrets.token_hex(12)}.json"
        async with Session(self.runtime, self.caller) as session:
            if session.saved is None:
                raise RestockError("link_login_required")
            try:
                # CLI 0.22's --output-file handles cards only. Redirect JSON into
                # a private file instead, then download it internally. The token
                # never becomes command text or a sandbox execute result.
                from restock.link_session import CLI, parse_json

                command = " ".join(
                    [
                        "umask 077;",
                        f"LINK_AUTH_FILE={shlex.quote(session.path)}",
                        CLI,
                        "spend-request retrieve",
                        shlex.quote(request_id),
                        "--include shared_payment_token --format json",
                        ">",
                        shlex.quote(path),
                        "2>/dev/null",
                    ]
                )
                code, _ = await session.execute(command)
                if code != 0:
                    raise RestockError("payment_token_unavailable")
                downloads = await session.backend.adownload_files([path])
                if len(downloads) != 1 or downloads[0].error or downloads[0].content is None:
                    raise RestockError("payment_token_unavailable")
                saved = one(parse_json(downloads[0].content.decode()))
            except (ValueError, TypeError):
                raise RestockError("payment_token_unavailable") from None
            finally:
                await session.remove_private_file(path)
        if not isinstance(saved, dict) or saved.get("id") != request_id:
            raise RestockError("payment_token_mismatch")
        request = dict(saved)
        token = saved.get("shared_payment_token")
        if isinstance(token, dict):
            token = token.get("id")
        if not isinstance(token, str) or not token.startswith("spt_"):
            raise RestockError("payment_token_unavailable")
        request.pop("shared_payment_token", None)
        return request, token

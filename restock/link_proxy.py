"""Signed, short-lived permissions for the native sandbox credential callback.

Only authored tools create permissions. The model cannot select their owner,
change their scope or retrieve their credential. Store get/put is not a distributed
lock: deployments with concurrent workers need an atomic lease implementation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import weakref
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlsplit

import jwt

from restock.config import RestockError

NAMESPACE = ("restock_link_proxy",)
PLACEHOLDER = "restock-proxy-"
SIGNATURE_HEADER = "x-langsmith-signature-jwt"
MAX_BODY = 1_500_000
MAX_PERMISSION_SECONDS = 180
SAFE_REQUEST_ID = re.compile(r"lsrq_[A-Za-z0-9_-]+\Z")
_locks = weakref.WeakKeyDictionary()
_LOG = logging.getLogger(__name__)


def _lock(key):
    return _locks.setdefault(asyncio.get_running_loop(), {}).setdefault(key, asyncio.Lock())


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("non-finite number")


def decode_json(raw):
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def operation(method, path, *, query="", body=None, lifecycle=False):
    return {
        "method": method,
        "path": path,
        "query": [list(pair) for pair in sorted(parse_qsl(query, keep_blank_values=True))],
        "body_sha256": digest(encoded(body)) if body is not None else digest(b""),
        "lifecycle": lifecycle,
    }


def request_operation(args):
    """Small allowlist of CLI 0.22.0 operations used by CliWallet, not a shell parser."""
    args = list(args)
    if args == ["payment-methods", "list"]:
        return operation("GET", "/payment-details")
    if args == ["spend-request", "list", "--include-history"]:
        return operation("GET", "/spend_requests", query="include_history=true")
    if len(args) >= 3 and args[0] == "spend-request" and SAFE_REQUEST_ID.fullmatch(args[2]):
        path = "/spend_requests/" + args[2]
        if args[1] == "cancel" and len(args) == 3:
            return operation("POST", path + "/cancel")
        if args[1] == "retrieve":
            if len(args) == 3:
                return operation("GET", path)
            if args[3:] == ["--include", "shared_payment_token"]:
                return operation("GET", path, query="include=shared_payment_token")
    if args[:2] != ["spend-request", "create"]:
        raise RestockError("link_proxy_operation_not_allowed")
    # Match the pinned CLI's createParams. Reject unknown flags, repeated single
    # values and approval shortcuts rather than granting a wider API operation.
    fields = {
        "--idempotency-key": "idempotency_key",
        "--payment-method-id": "payment_details",
        "--credential-type": "credential_type",
        "--network-id": "network_id",
        "--amount": "amount",
        "--currency": "currency",
        "--context": "context",
    }
    body, i = {}, 2
    try:
        while i < len(args):
            flag = args[i]
            if flag in {"--request-approval", "--test"}:
                name = flag[2:].replace("-", "_")
                if name in body:
                    raise ValueError()
                body[name] = True
                i += 1
                continue
            value = args[i + 1]
            i += 2
            if flag in fields:
                name = fields[flag]
                if name in body:
                    raise ValueError()
                body[name] = int(value) if name == "amount" else value
            elif flag in {"--line-item", "--total", "--metadata"}:
                parts = [pair.split(":", 1) for pair in value.split(",")]
                item = _unique(parts)
                if flag == "--metadata":
                    if set(body.get("metadata", {})) & set(item):
                        raise ValueError()
                    body.setdefault("metadata", {}).update(item)
                else:
                    for name in ("quantity", "unit_amount", "amount"):
                        if name in item:
                            item[name] = int(item[name])
                    body.setdefault("line_items" if flag == "--line-item" else "totals", []).append(
                        item
                    )
            else:
                raise ValueError()
        if (
            body.get("credential_type") != "shared_payment_token"
            or body.get("currency") != "usd"
            or not body.get("request_approval")
            or type(body.get("amount")) is not int
            or body["amount"] <= 0
            or not body.get("idempotency_key")
            or not body.get("network_id")
        ):
            raise ValueError()
    except (ValueError, IndexError, TypeError):
        raise RestockError("link_proxy_operation_not_allowed") from None
    return operation("POST", "/spend_requests", body=body)


class Permissions:
    def __init__(self, store, *, signing_key, callback_url, deployment_id, workspace_id):
        self.store, self.key, self.url = store, signing_key, callback_url
        self.deployment_id, self.workspace_id = deployment_id, workspace_id

    async def consume(self, sandbox_id, signed):
        # This is atomic only inside this process. Multi-worker deployments need
        # a transactional claim instead of Store get/put (see architecture).
        async with _lock((self.workspace_id, self.deployment_id, sandbox_id)):
            current = await self.store.aget(NAMESPACE, sandbox_id)
            if not current or current.value.get("signed") != signed:
                raise RestockError("link_proxy_permission_denied")
            await self.store.aput(NAMESPACE, sandbox_id, {})

    def unpack(self, record):
        try:
            value = jwt.decode(
                record["signed"],
                self.key,
                algorithms=["HS256"],
                audience=self.url,
                options={"require": ["exp", "iat", "aud", "jti"]},
            )
            if (
                value["deployment_id"] != self.deployment_id
                or value["workspace_id"] != self.workspace_id
                or value["exp"] - value["iat"] > MAX_PERMISSION_SECONDS
            ):
                raise ValueError()
            return value
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise RestockError("link_proxy_permission_denied") from None

    @asynccontextmanager
    async def allow(self, sandbox_id, principal_id, slug, saved, spec, *, seconds=150):
        if self.store is None or not all(
            (sandbox_id, principal_id, self.deployment_id, self.workspace_id)
        ):
            raise RestockError("link_proxy_identity_unavailable")
        key = str(sandbox_id)
        old = await self.store.aget(NAMESPACE, key)
        if old and old.value.get("signed"):
            try:
                self.unpack(old.value)
            except RestockError:
                pass  # expired or invalid records cannot authorize a request
            else:
                raise RestockError("link_proxy_busy")
        now, nonce = int(time.time()), secrets.token_hex(24)
        value = {
            "iat": now,
            "exp": now + min(seconds, MAX_PERMISSION_SECONDS),
            "jti": nonce,
            "aud": self.url,
            "deployment_id": self.deployment_id,
            "workspace_id": self.workspace_id,
            "sandbox_id": sandbox_id,
            "principal_id": principal_id,
            "slug": slug,
            "session_sha256": digest(saved.encode()),
            "operation": spec,
        }
        signed = jwt.encode(value, self.key, algorithm="HS256")
        await self.store.aput(NAMESPACE, key, {"signed": signed})
        try:
            yield PLACEHOLDER + nonce
        finally:
            # Never clear a newer operation. No token material is stored here.
            current = await self.store.aget(NAMESPACE, key)
            if current and current.value.get("signed") == signed:
                await self.store.aput(NAMESPACE, key, {})


class Callback:
    def __init__(self, permissions, signing_key, read_session, *, issuer):
        self.permissions, self.signing_key, self.read_session = (
            permissions,
            signing_key,
            read_session,
        )
        self.issuer = issuer

    async def verify(self, raw, signature):
        stage = "signature_header"
        try:
            if len(raw) > MAX_BODY or not isinstance(signature, str) or len(signature) > 8192:
                raise ValueError()
            header = jwt.get_unverified_header(signature)
            if header.get("alg") != "EdDSA" or not isinstance(header.get("kid"), str):
                raise ValueError()
            stage = "signing_key"
            key = await self.signing_key(header["kid"])
            stage = "signature_claims"
            claims = jwt.decode(
                signature,
                key,
                algorithms=["EdDSA"],
                issuer=self.issuer,
                audience=self.permissions.url,
                options={
                    "require": ["iss", "aud", "sub", "exp", "body_sha256"],
                },
            )
            stage = "bound_body"
            # JWT permits either a string or an array. LangSmith's Go signer
            # uses a one-element array; require exactly this callback in either
            # form, rather than accepting unrelated additional audiences.
            if (
                claims["aud"] not in (self.permissions.url, [self.permissions.url])
                or claims["sub"] != "langsmith-sandbox-callback"
                or claims["body_sha256"] != digest(raw)
                or claims["exp"] > time.time() + 330
            ):
                raise ValueError()
            event = decode_json(raw)
            if not isinstance(event, dict):
                raise ValueError()
            return event
        except Exception as error:
            # Fixed stage and class names only: never log the JWT, body, claims,
            # exception message, credentials, or caller details.
            _LOG.warning("Link proxy verification denied at %s (%s)", stage, type(error).__name__)
            raise RestockError("link_proxy_callback_denied") from None

    async def resolve(self, raw, signature):
        stage = "verify"
        try:
            event = await self.verify(raw, signature)
            stage = "request_identity"
            identity, req = event["identity"], event["request"]
            if (
                identity["tenant_id"] != self.permissions.workspace_id
                or event["host"] != "api.link.com"
                or event["port"] != 443
                or req.get("body_truncated")
                or req["scheme"] != "https"
                or req["host"] not in {"api.link.com", "api.link.com:443"}
            ):
                raise ValueError()
            target = urlsplit(req["url"])
            if (
                target.scheme != "https"
                or target.hostname != "api.link.com"
                or target.port not in (None, 443)
                or target.username
                or target.password
                or target.fragment
                or target.path != req["path"]
                or target.query != req.get("query", "")
            ):
                raise ValueError()
            stage = "permission_lookup"
            record = await self.permissions.store.aget(NAMESPACE, str(identity["sandbox_id"]))
            grant = self.permissions.unpack(record.value if record else {})
            if grant["sandbox_id"] != identity["sandbox_id"]:
                raise ValueError()
            stage = "request_binding"
            body = base64.b64decode(req.get("body_base64", ""), validate=True)
            spec = grant["operation"]
            if body and spec["body_sha256"] == digest(b""):
                raise ValueError()
            actual = operation(
                req["method"],
                req["path"],
                query=req.get("query", ""),
                body=decode_json(body) if body else None,
                lifecycle=spec["lifecycle"],
            )
            if actual != spec:
                raise ValueError()
            stage = "session_lookup"
            saved = await self.read_session(grant)
            if not saved or digest(saved.encode()) != grant["session_sha256"]:
                raise ValueError()
            if spec["lifecycle"]:
                if spec != operation("GET", "/payment-details", lifecycle=True):
                    raise ValueError()
                await self.permissions.consume(grant["sandbox_id"], record.value["signed"])
                return {"headers": {}}
            stage = "placeholder_binding"
            headers = {name.lower(): value for name, value in req["headers"].items()}
            if headers.get("authorization") != ["Bearer " + PLACEHOLDER + grant["jti"]]:
                raise ValueError()
            auth = decode_json(saved).get("auth") or {}
            if (
                not isinstance(auth.get("access_token"), str)
                or not auth["access_token"]
                or auth.get("expires_at", 0) <= (time.time() + 30) * 1000
            ):
                raise ValueError()
            stage = "permission_claim"
            await self.permissions.consume(grant["sandbox_id"], record.value["signed"])
            return {"headers": {"Authorization": "Bearer " + auth["access_token"]}}
        except Exception as error:
            # Never include provider bodies, session data or request snapshots.
            _LOG.warning("Link proxy resolution denied at %s (%s)", stage, type(error).__name__)
            raise RestockError("link_proxy_callback_denied") from None

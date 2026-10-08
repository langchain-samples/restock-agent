"""Operator configuration for Link CLI authentication through the sandbox proxy."""

import ipaddress
import os
from urllib.parse import urlsplit

from restock.config import RestockError

CALLBACK_PATH = "/channels/link_proxy/events"
ALLOW_LIST = ["api.link.com:443", "login.link.com:443", "registry.npmjs.org:443"]


def transport(environment=None):
    env = os.environ if environment is None else environment
    value = env.get("RESTOCK_LINK_TRANSPORT", "proxy")
    if value not in {"proxy", "session"}:
        raise RestockError("invalid_link_transport")
    return value


def public_origin(value):
    try:
        parsed = urlsplit(value.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or any(char.isspace() for char in value)
            or parsed.hostname in {"localhost", "metadata.google.internal"}
            or parsed.hostname.endswith((".localhost", ".local", ".internal"))
            or "." not in parsed.hostname
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        return value.rstrip("/")
    except (ValueError, AttributeError):
        raise RestockError("link_proxy_not_configured") from None


def configuration(environment=None):
    env = os.environ if environment is None else environment
    base = public_origin(env.get("RESTOCK_PROXY_URL", ""))
    key = env.get("RESTOCK_PROXY_SIGNING_KEY", "")
    if len(key) < 32 or key.startswith(("<", "your", "${")):
        raise RestockError("link_proxy_not_configured")
    return base + CALLBACK_PATH, key


def sandbox_proxy_config(environment=None):
    env = os.environ if environment is None else environment
    result = {"access_control": {"allow_list": ALLOW_LIST}}
    if transport(env) == "proxy" and env.get("RESTOCK_PROXY_URL"):
        # Permit a first deployment/dev start to obtain its URL. Wallet operations
        # still require complete configuration and never fall back to session mode.
        url = public_origin(env["RESTOCK_PROXY_URL"]) + CALLBACK_PATH
        result["callbacks"] = [
            {"match_hosts": ["api.link.com"], "url": url, "ttl_seconds": 60, "full_request": True}
        ]
    return result

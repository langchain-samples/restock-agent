"""Only Link runs in the sandbox; Zinc HTTP calls run privately in authored tools."""

from managed_deepagents import define_sandbox

from restock.proxy_config import sandbox_proxy_config

sandbox = define_sandbox(
    idle_ttl_seconds=900,
    default_timeout=180,
    proxy_config=sandbox_proxy_config(),
)

"""Only Link runs in the sandbox; Zinc HTTP calls run privately in authored tools."""

from managed_deepagents import define_sandbox

sandbox = define_sandbox(
    idle_ttl_seconds=900,
    default_timeout=180,
    proxy_config={
        "access_control": {
            "allow_list": ["api.link.com:443", "login.link.com:443", "registry.npmjs.org:443"]
        }
    },
)

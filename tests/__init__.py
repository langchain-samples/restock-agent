"""Offline tests. Network access is blocked and inherited credentials are removed.

Loopback connections stay open so fake Link servers can run. There is no switch
that turns these tests into live account or payment calls.
"""

import os
import socket

for name in list(os.environ):
    upper = name.upper()
    if any(word in upper for word in ("API_KEY", "SECRET", "TOKEN", "PASSWORD")):
        os.environ.pop(name)
os.environ.update(
    OPENAI_API_KEY="offline-test-key",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
    LANGCHAIN_TRACING="false",
)

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guard(real):
    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else ""
        if host in ("127.0.0.1", "::1", "localhost"):  # fake servers in tests
            return real(self, address, *args, **kwargs)
        raise AssertionError("Network access is disabled in tests")

    return guarded


socket.socket.connect = _guard(_real_connect)  # type: ignore[method-assign]
socket.socket.connect_ex = _guard(_real_connect_ex)  # type: ignore[method-assign]

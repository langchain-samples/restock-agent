import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from restock.config import RestockError
from restock.proxy_config import configuration, public_origin, sandbox_proxy_config, transport
from restock.proxy_runtime import SigningKeys, context_for
from scripts.configure_proxy import configure
from scripts.export_app import export
from scripts.preflight import problems, settings_from_file
from tests.proxy_support import ENV, ORIGIN, SANDBOX


class ProxySetupTests(unittest.TestCase):
    def test_default_proxy_and_explicit_rollback(self):
        self.assertEqual(transport({}), "proxy")
        self.assertEqual(transport({"RESTOCK_LINK_TRANSPORT": "session"}), "session")
        with self.assertRaises(RestockError):
            transport({"RESTOCK_LINK_TRANSPORT": "typo"})
        for bad in (
            "",
            "http://example.com",
            "https://localhost",
            "https://127.0.0.1",
            "https://user:pass@example.com",
            "https://example.com/path",
            "https://example.com?x=y",
        ):
            with self.assertRaises(RestockError):
                public_origin(bad)
        with self.assertRaises(RestockError):
            configuration({"RESTOCK_PROXY_URL": ORIGIN})

    def test_native_callback_config_uses_every_request_and_no_static_rule(self):
        cfg = sandbox_proxy_config(ENV)
        self.assertEqual(cfg["callbacks"][0]["url"], configuration(ENV)[0])
        self.assertTrue(cfg["callbacks"][0]["full_request"])
        self.assertNotIn("rules", cfg)
        self.assertNotIn("callbacks", sandbox_proxy_config({"RESTOCK_LINK_TRANSPORT": "session"}))
        self.assertNotIn("callbacks", sandbox_proxy_config({}))

    def test_non_slack_export_contains_callback_and_configure_preserves_settings(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "app"
            with contextlib.redirect_stdout(io.StringIO()):
                export(app, root, slack=False)
            self.assertTrue((app / "channels/link_proxy.py").is_file())
            self.assertFalse((app / "channels/slack.py").exists())
            self.assertFalse((app / "channels/zinc.py").exists())
            settings = app / ".env"
            settings.write_text("UNRELATED=private-fixture\nRESTOCK_MODE=link-test\n")
            configure(app, ORIGIN)
            first = settings_from_file(settings, {})
            configure(app, "https://other.example.invalid")
            second = settings_from_file(settings, {})
            self.assertEqual(
                first["RESTOCK_PROXY_SIGNING_KEY"], second["RESTOCK_PROXY_SIGNING_KEY"]
            )
            self.assertEqual(second["RESTOCK_MODE"], "link-test")
            self.assertIn("UNRELATED=private-fixture", settings.read_text())
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            original = settings.read_bytes()
            with self.assertRaises(RestockError):
                configure(app, "http://localhost")
            self.assertEqual(settings.read_bytes(), original)

    def test_preflight_requires_proxy_only_for_wallet_modes(self):
        base = {
            "OPENAI_API_KEY": "fixture",
            "LANGSMITH_API_KEY": "fixture",
            "LANGSMITH_WORKSPACE_ID": "fixture",
        }
        self.assertEqual(problems({**base, "RESTOCK_MODE": "rehearsal"}), [])
        self.assertTrue(
            any("configure_proxy" in p for p in problems({**base, "RESTOCK_MODE": "link-test"}))
        )
        self.assertEqual(problems({**base, **ENV, "RESTOCK_MODE": "link-test"}), [])
        self.assertTrue(
            any(
                "BACKEND" in p
                for p in problems(
                    {**base, **ENV, "RESTOCK_MODE": "live", "LINK_SESSION_BACKEND": "store"}
                )
            )
        )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_resolves_uuid_not_name_and_uses_requester(self):
        from restock import link_session

        store, backend = (
            object(),
            SimpleNamespace(
                id="fixture-box", aexecute=AsyncMock(return_value=SimpleNamespace(exit_code=0))
            ),
        )
        ctx = SimpleNamespace(
            api_key="fixture-api",
            workspace_id="workspace",
            agent_id="agent",
            principal_id="requester",
        )
        sessions = SimpleNamespace(
            _client=lambda rt: SimpleNamespace(context=ctx), slug="link-session"
        )
        rt = SimpleNamespace(store=store, backend=backend)
        original = httpx.AsyncClient
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"name": "fixture-box", "id": SANDBOX})

        with (
            patch.dict(os.environ, ENV),
            patch.object(link_session, "sessions", sessions),
            patch(
                "restock.proxy_runtime.httpx.AsyncClient",
                side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
            ),
        ):
            result = await context_for(rt)
        self.assertEqual(result.sandbox_id, SANDBOX)
        self.assertEqual(result.principal_id, "requester")
        self.assertEqual(calls[0].url.path, "/v2/sandboxes/boxes/fixture-box")
        self.assertEqual(calls[0].headers["x-tenant-id"], "workspace")
        backend.aexecute.assert_awaited_once_with("true", timeout=20)

    async def test_jwks_uses_fixed_origin_cache_and_ed25519_keys(self):
        key = Ed25519PrivateKey.generate()
        jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid="fixture-key", alg="EdDSA", use="sig")
        calls = []
        original = httpx.AsyncClient

        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"keys": [jwk]})

        with patch(
            "restock.proxy_runtime.httpx.AsyncClient",
            side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
        ):
            keys = SigningKeys("https://auth.example.invalid")
            self.assertIs(await keys("fixture-key"), await keys("fixture-key"))
            with self.assertRaises(RestockError):
                await keys("other-key")
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), "https://auth.example.invalid/.well-known/jwks.json")

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401
import httpx

from restock.config import RestockError
from restock.rehearsal import OFFICE
from scripts.office_connection import OfficeUpdateError, OfficeUpdater, api_bases

WORKSPACE = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT = "00000000-0000-4000-8000-000000000002"
CONNECTION = "00000000-0000-4000-8000-000000000003"
CREDENTIAL = "00000000-0000-4000-8000-000000000004"
OTHER_CREDENTIAL = "00000000-0000-4000-8000-000000000005"
VALUES = {"LANGSMITH_API_KEY": "fictional-key", "LANGSMITH_WORKSPACE_ID": WORKSPACE}


class OfficeConnectionTests(unittest.TestCase):
    def fixture(self, *, name="restock", slug="restock-office", override=None):
        requests = []
        # Another deployment's secret must stay untouched under the same slug.
        saved = {CREDENTIAL: {"old": "fixture"}, OTHER_CREDENTIAL: {"another": "fixture"}}

        def handler(request):
            requests.append(request)
            if override:
                result = override(request)
                if result is not None:
                    return result
            path = request.url.path
            if request.method == "GET" and path == "/v2/deployments":
                self.assertEqual(request.url.params["name_contains"], name)
                body = {"resources": [{"id": DEPLOYMENT, "name": name}]}
            elif request.method == "GET" and path == f"/v2/deployments/{DEPLOYMENT}":
                body = {"id": DEPLOYMENT, "name": name, "is_managed_deep_agent": True}
            elif request.method == "GET" and path == "/v1/agent-auth/connections":
                body = {"items": [{"id": CONNECTION, "slug": slug}]}
            elif request.method == "GET" and path == f"/v1/agent-auth/connections/{CONNECTION}":
                self.assertEqual(
                    dict(request.url.params),
                    {
                        "credential_owner_type": "agent",
                        "credential_owner_id": DEPLOYMENT,
                    },
                )
                body = {
                    "id": CONNECTION,
                    "slug": slug,
                    "credential": {"kind": "secret", "credential_id": CREDENTIAL},
                }
            elif request.method == "PATCH" and path == f"/v1/agent-auth/credentials/{CREDENTIAL}":
                body = json.loads(request.content)
                self.assertEqual(set(body), {"secret"})
                saved[CREDENTIAL] = json.loads(body["secret"])
                return httpx.Response(204)
            else:
                raise AssertionError(
                    "Unexpected request; secret reads and other writes are forbidden"
                )
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler), requests, saved

    def test_update_replaces_only_the_selected_agents_office_without_reading_any_secret(self):
        transport, requests, saved = self.fixture()
        replacement = copy.deepcopy(OFFICE)
        replacement["shipping_address"].pop("address_line2", None)
        with OfficeUpdater(VALUES, transport=transport) as updater:
            updater.resolve()
            updater.replace(replacement)
        self.assertEqual(saved[CREDENTIAL], replacement)
        self.assertEqual(saved[OTHER_CREDENTIAL], {"another": "fixture"})
        self.assertEqual([r.method for r in requests], ["GET"] * 4 + ["PATCH"])
        for request in requests:
            self.assertEqual(request.headers["x-tenant-id"], WORKSPACE)
            self.assertEqual(request.headers["x-api-key"], "fictional-key")
            self.assertNotIn("/secret", request.url.path)

    def test_custom_deployment_connection_and_endpoint_are_supported(self):
        transport, requests, _ = self.fixture(name="office-b", slug="office-delivery")
        values = {
            **VALUES,
            "RESTOCK_OFFICE_CONNECTION": "office-delivery",
            "LANGSMITH_ENDPOINT": "https://example.test",
        }
        with OfficeUpdater(values, "office-b", transport=transport) as updater:
            updater.resolve()
            updater.replace(OFFICE)
        self.assertTrue(all(request.url.host == "example.test" for request in requests))

    def test_no_fuzzy_or_ambiguous_deployment_match_can_write(self):
        for resources in (
            [],
            [{"id": DEPLOYMENT, "name": "restock-backup"}],
            [{"id": DEPLOYMENT, "name": "restock"}] * 2,
        ):
            with self.subTest(resources=resources):

                def override(request):
                    if request.url.path == "/v2/deployments":
                        return httpx.Response(200, json={"resources": resources})

                transport, requests, _ = self.fixture(override=override)
                with OfficeUpdater(VALUES, transport=transport) as updater:
                    with self.assertRaisesRegex(OfficeUpdateError, "exact deployment"):
                        updater.resolve()
                self.assertTrue(all(r.method == "GET" for r in requests))

    def test_nonmanaged_or_changed_deployment_is_rejected(self):
        for change in ({"is_managed_deep_agent": False}, {"name": "another-agent"}):
            with self.subTest(change=change):

                def override(request):
                    if request.url.path == f"/v2/deployments/{DEPLOYMENT}":
                        return httpx.Response(
                            200, json={"id": DEPLOYMENT, "name": "restock", **change}
                        )

                transport, requests, _ = self.fixture(override=override)
                with OfficeUpdater(VALUES, transport=transport) as updater:
                    with self.assertRaises(OfficeUpdateError):
                        updater.resolve()
                self.assertTrue(all(r.method == "GET" for r in requests))

    def test_missing_or_wrong_owner_credential_never_creates_or_deletes(self):
        for credential in ({"kind": "secret"}, {"kind": "oauth2", "credential_id": CREDENTIAL}):
            with self.subTest(credential=credential):

                def override(request):
                    if request.url.path == f"/v1/agent-auth/connections/{CONNECTION}":
                        return httpx.Response(
                            200,
                            json={
                                "id": CONNECTION,
                                "slug": "restock-office",
                                "credential": credential,
                            },
                        )

                transport, requests, _ = self.fixture(override=override)
                with OfficeUpdater(VALUES, transport=transport) as updater:
                    with self.assertRaises(OfficeUpdateError):
                        updater.resolve()
                self.assertTrue(all(r.method == "GET" for r in requests))

    def test_connection_pagination_and_repeated_cursor_guard(self):
        for repeated in (False, True):
            with self.subTest(repeated=repeated):

                def override(request):
                    if request.url.path == "/v1/agent-auth/connections":
                        if repeated or "cursor" not in request.url.params:
                            return httpx.Response(
                                200, json={"items": [], "next_cursor": "page-two"}
                            )

                transport, requests, _ = self.fixture(override=override)
                with OfficeUpdater(VALUES, transport=transport) as updater:
                    if repeated:
                        with self.assertRaisesRegex(OfficeUpdateError, "lookup did not finish"):
                            updater.resolve()
                    else:
                        updater.resolve()
                self.assertTrue(all(r.method == "GET" for r in requests))
                pages = [r for r in requests if r.url.path == "/v1/agent-auth/connections"]
                self.assertEqual(len(pages), 2)

    def test_metadata_error_and_redirect_are_sanitized_and_never_followed(self):
        for status in (302, 401, 403, 500):
            with self.subTest(status=status):

                def override(request):
                    return httpx.Response(
                        status,
                        headers={"location": "https://other.test"},
                        json={"detail": "fictional-private-response"},
                    )

                transport, requests, _ = self.fixture(override=override)
                with OfficeUpdater(VALUES, transport=transport) as updater:
                    with self.assertRaises(OfficeUpdateError) as error:
                        updater.resolve()
                self.assertNotIn("fictional-private-response", str(error.exception))
                self.assertEqual(len(requests), 1)

    def test_failed_patch_is_not_retried_and_does_not_claim_the_old_value_remains(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):

                def override(request):
                    if request.method == "PATCH":
                        if timeout:
                            raise httpx.ReadTimeout("fictional-private-response", request=request)
                        return httpx.Response(403, json={"secret": "fictional-private-response"})

                transport, requests, _ = self.fixture(override=override)
                with OfficeUpdater(VALUES, transport=transport) as updater:
                    updater.resolve()
                    with self.assertRaisesRegex(OfficeUpdateError, "not confirmed") as error:
                        updater.replace(OFFICE)
                self.assertNotIn("fictional-private-response", str(error.exception))
                self.assertEqual(sum(r.method == "PATCH" for r in requests), 1)

    def test_invalid_office_or_unresolved_target_cannot_write(self):
        transport, requests, _ = self.fixture()
        with OfficeUpdater(VALUES, transport=transport) as updater:
            with self.assertRaises(OfficeUpdateError):
                updater.replace(OFFICE)
            updater.resolve()
            with self.assertRaises(RestockError):
                updater.replace({"shipping_address": {}})
        self.assertTrue(all(r.method == "GET" for r in requests))

    def test_project_settings_take_precedence_without_reading_old_delivery_details(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "agent.py").write_text("# fixture project")
            (project / ".env").write_text(
                f"LANGSMITH_API_KEY=fictional-project-key\nLANGSMITH_WORKSPACE_ID={WORKSPACE}\n"
                "RESTOCK_OFFICE_CONNECTION=office-delivery\n"
            )
            with patch.dict("os.environ", {"LANGSMITH_API_KEY": "fictional-shell-key"}):
                with OfficeUpdater.from_project(project) as updater:
                    self.assertEqual(updater.client.headers["x-api-key"], "fictional-project-key")
                    self.assertEqual(updater.slug, "office-delivery")

    def test_missing_project_does_not_use_an_unrelated_shell_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", VALUES):
            with self.assertRaisesRegex(OfficeUpdateError, "Project not found"):
                OfficeUpdater.from_project(Path(directory))

    def test_api_origins_and_identifiers_reject_unsafe_input(self):
        for origin in (
            "http://example.test",
            "https://user:pass@example.test",
            "https://example.test?q=x",
            "https://example.test/#x",
            "https://example.test/path",
        ):
            with self.subTest(origin=origin), self.assertRaises(OfficeUpdateError):
                api_bases(origin)
        self.assertEqual(
            api_bases("https://beta.api.smith.langchain.com/"),
            ("https://beta.api.host.langchain.com", "https://beta.api.smith.langchain.com"),
        )
        for invalid in (None, "../../another", "", True):
            with self.subTest(invalid=invalid), self.assertRaises(OfficeUpdateError):
                OfficeUpdater({**VALUES, "LANGSMITH_WORKSPACE_ID": invalid})

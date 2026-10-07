"""Replace one deployment's office secret without reading or deleting its value.

Uses the Agent Auth metadata/PATCH contract used by pinned MDA 0.8.1. This is an
operator setup utility, never an agent tool. Recheck the contract when upgrading.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from restock.storage import validate_office
from scripts.preflight import settings_from_file

SETTING_NAMES = {
    "LANGSMITH_API_KEY",
    "LANGSMITH_WORKSPACE_ID",
    "LANGSMITH_ENDPOINT",
    "RESTOCK_OFFICE_CONNECTION",
}


class OfficeUpdateError(Exception):
    """Only fixed messages may reach the terminal, never HTTP bodies or values."""


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise OfficeUpdateError("Invalid identifier in office setup or service metadata.") from None


def api_bases(endpoint):
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise OfficeUpdateError("LANGSMITH_ENDPOINT must be an HTTPS API origin.")
    platform = endpoint.rstrip("/")
    host = parsed.hostname
    if host == "api.smith.langchain.com" or host.endswith(".api.smith.langchain.com"):
        return platform.replace("api.smith.langchain.com", "api.host.langchain.com"), platform
    if host == "api.host.langchain.com" or host.endswith(".api.host.langchain.com"):
        return platform, platform.replace("api.host.langchain.com", "api.smith.langchain.com")
    # Self-hosted LangSmith may serve both APIs at the operator-configured origin.
    return platform, platform


class OfficeUpdater:
    def __init__(self, values, deployment="restock", *, transport=None):
        if not values.get("LANGSMITH_API_KEY", "").strip():
            raise OfficeUpdateError("Set LANGSMITH_API_KEY privately in the project's settings.")
        workspace = identifier(values.get("LANGSMITH_WORKSPACE_ID"))
        self.host, self.platform = api_bases(
            values.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com"
        )
        self.slug = values.get("RESTOCK_OFFICE_CONNECTION") or "restock-office"
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.slug):
            raise OfficeUpdateError("RESTOCK_OFFICE_CONNECTION must be a valid connection slug.")
        if not isinstance(deployment, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", deployment):
            raise OfficeUpdateError("Use --deployment with the exact name used for mda deploy.")
        self.deployment = deployment
        self.credential_id = None
        self.client = httpx.Client(
            headers={
                "x-api-key": values["LANGSMITH_API_KEY"],
                "x-tenant-id": workspace,
                "x-langsmith-product": "mda",
            },
            timeout=20,
            follow_redirects=False,
            transport=transport,
        )

    @classmethod
    def from_project(cls, project: Path, deployment="restock"):
        if not (project / "agent.py").is_file():
            raise OfficeUpdateError("Project not found. Run scripts/setup.py or check --project.")
        # Match the MDA CLI's project-first settings; never print their values.
        values = settings_from_file(project / ".env", {}, names=SETTING_NAMES)
        values = {name: values.get(name) or os.environ.get(name, "") for name in SETTING_NAMES}
        return cls(values, deployment)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def _get(self, origin, path, **params):
        try:
            response = self.client.get(origin + path, params=params)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (httpx.HTTPError, ValueError):
            raise OfficeUpdateError(
                "Could not read office metadata. Check sign-in, workspace, and deployment access. "
                "No office details were changed."
            ) from None

    def resolve(self):
        """Locate the exact deployment and its credential, without fetching any secret."""
        self.credential_id = None
        page = self._get(self.host, "/v2/deployments", name_contains=self.deployment)
        resources = page.get("resources")
        if not isinstance(resources, list):
            raise OfficeUpdateError("Invalid deployment metadata. No office details were changed.")
        matches = [r for r in resources if isinstance(r, dict) and r.get("name") == self.deployment]
        if len(matches) != 1:
            raise OfficeUpdateError(
                "Could not identify one exact deployment. Check --deployment and workspace settings."
            )
        owner = identifier(matches[0].get("id"))
        deployment = self._get(self.host, f"/v2/deployments/{owner}")
        if identifier(deployment.get("id")) != owner or deployment.get("name") != self.deployment:
            raise OfficeUpdateError("Deployment metadata changed. No office details were changed.")
        if any(
            deployment.get(key) is False for key in ("is_managed_deep_agent", "managed_deep_agent")
        ):
            raise OfficeUpdateError("This deployment is not a Managed Deep Agent.")
        cursor, seen = None, set()
        for _ in range(20):
            params = {"page_size": 100, **({"cursor": cursor} if cursor else {})}
            page = self._get(self.platform, "/v1/agent-auth/connections", **params)
            rows = page.get("items")
            if not isinstance(rows, list):
                raise OfficeUpdateError(
                    "Invalid Connection metadata. No office details were changed."
                )
            matches = [r for r in rows if isinstance(r, dict) and r.get("slug") == self.slug]
            if len(matches) > 1:
                raise OfficeUpdateError(
                    "Ambiguous office Connection. No office details were changed."
                )
            if matches:
                connection_id = identifier(matches[0].get("id"))
                detail = self._get(
                    self.platform,
                    f"/v1/agent-auth/connections/{connection_id}",
                    credential_owner_type="agent",
                    credential_owner_id=owner,
                )
                credential = detail.get("credential") or {}
                if (
                    detail.get("slug") != self.slug
                    or identifier(detail.get("id")) != connection_id
                    or not isinstance(credential, dict)
                    or credential.get("kind") != "secret"
                ):
                    raise OfficeUpdateError("Office Connection is not the expected secret entry.")
                if not credential.get("credential_id"):
                    raise OfficeUpdateError(
                        "This deployment has no saved office details. Run setup without --update first."
                    )
                self.credential_id = identifier(credential["credential_id"])
                return
            cursor = page.get("next_cursor")
            if cursor is None or cursor == "":
                raise OfficeUpdateError(
                    "Office Connection not found. Run setup without --update first."
                )
            if not isinstance(cursor, str) or cursor in seen:
                break
            seen.add(cursor)
        raise OfficeUpdateError("Connection lookup did not finish. No office details were changed.")

    def replace(self, value):
        value = validate_office(value)
        if self.credential_id is None:
            raise OfficeUpdateError("Resolve the office Connection before updating it.")
        try:
            response = self.client.patch(
                self.platform + f"/v1/agent-auth/credentials/{self.credential_id}",
                json={"secret": json.dumps(value)},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # A timeout may occur after saving. Do not claim the old value remains
            # or retry automatically, and never expose the provider response.
            raise OfficeUpdateError(
                "The update was not confirmed. Re-run with the same details before starting an order."
            ) from None

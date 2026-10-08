"""Configuration is controlled by the operator, never by a model tool argument."""

from __future__ import annotations

import os
from dataclasses import dataclass


class RestockError(Exception):
    """A public, fixed error code. Never wrap provider response bodies here."""


class PaymentNotRequested(RestockError):
    """A wallet check failed before attempting spend-request creation."""


@dataclass(frozen=True)
class Settings:
    mode: str = "rehearsal"
    max_budget: int = 10000
    wait_seconds: int = 120
    zinc_connection: str = "restock-zinc"
    office_connection: str = "restock-office"

    @classmethod
    def load(cls, env=None) -> Settings:
        env = os.environ if env is None else env
        mode = env.get("RESTOCK_MODE", "rehearsal")
        if mode not in {"rehearsal", "link-test", "live"}:
            raise RestockError("invalid_mode")
        try:
            maximum = int(env.get("RESTOCK_MAX_BUDGET_CENTS", "10000"))
            wait = int(env.get("RESTOCK_APPROVAL_WAIT_SECONDS", "120"))
        except ValueError:
            raise RestockError("invalid_configuration") from None
        if not 200 <= maximum <= 100000 or not 1 <= wait <= 180:
            raise RestockError("invalid_configuration")
        return cls(
            mode,
            maximum,
            wait,
            env.get("RESTOCK_ZINC_CONNECTION", "restock-zinc"),
            env.get("RESTOCK_OFFICE_CONNECTION", "restock-office"),
        )

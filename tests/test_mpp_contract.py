"""Verify our wire format against the installed provider implementation, not our mocks."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import tests  # noqa: F401
from mpp import Credential, parse_authorization, parse_www_authenticate
from mpp.errors import VerificationFailedError
from mpp.methods.stripe.intents import ChargeIntent

from restock.zinc import parse_challenge, payment_header
from tests.test_zinc import header


class StripeProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        create = AsyncMock(return_value=SimpleNamespace(id="pi_fixture", status="succeeded"))
        client = SimpleNamespace(
            v1=SimpleNamespace(payment_intents=SimpleNamespace(create_async=create))
        )
        return ChargeIntent(client=client), create

    async def test_restock_credential_passes_real_stripe_mpp_verifier(self):
        raw = header()
        parsed = parse_www_authenticate(raw)
        credential = parse_authorization(
            payment_header(parse_challenge([raw], 2500), "spt_fixture")
        )
        provider, create = self.provider()
        receipt = await provider.verify(credential, parsed.request)
        self.assertEqual(receipt.reference, "pi_fixture")
        create.assert_awaited_once()
        # This longer field belongs in the merchant's Stripe API call, not the
        # MPP credential that Restock sends to the merchant.
        self.assertEqual(create.call_args.args[0]["shared_payment_granted_token"], "spt_fixture")
        self.assertEqual(create.call_args.args[0]["amount"], 2500)

    async def test_zinc_documented_legacy_payload_is_rejected_before_payment(self):
        parsed = parse_www_authenticate(header())
        credential = Credential(
            challenge=parsed.to_echo(),
            payload={"type": "spt", "shared_payment_granted_token": "spt_fixture"},
        )
        provider, create = self.provider()
        with self.assertRaisesRegex(VerificationFailedError, "missing or malformed spt"):
            await provider.verify(credential, parsed.request)
        create.assert_not_awaited()

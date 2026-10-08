import json
import unittest

import tests  # noqa: F401
import httpx
from mpp import Challenge, format_www_authenticate, parse_authorization

from restock.config import RestockError
from restock.zinc import (
    Zinc,
    ZincSubmissionError,
    expiry,
    parse_challenge,
    payment_header,
    public_order,
    public_product,
)


def header(amount="2500", currency="usd", **kwargs):
    request = {
        "amount": amount,
        "currency": currency,
        "methodDetails": {
            "networkId": "internal_test_network",
            "paymentMethodTypes": ["card"],
        },
    }
    return format_www_authenticate(
        Challenge.create(
            secret_key="synthetic-only",
            realm="api.zinc.com",
            method="stripe",
            intent="charge",
            request=request,
            meta={"correlation": "retained"},
            **kwargs,
        ),
        realm="api.zinc.com",
    )


class ProtocolTests(unittest.TestCase):
    def test_nonfinite_expiry_is_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf"), True):
            with self.subTest(value=value), self.assertRaises(RestockError):
                expiry(value)

    def test_official_roundtrip_preserves_signed_fields(self):
        raw = header(digest="sha-256=test")
        challenge = parse_challenge([raw], 2500)
        encoded = payment_header(challenge, "spt_fixture")
        credential = parse_authorization(encoded)
        self.assertEqual(credential.payload, {"spt": "spt_fixture"})
        self.assertEqual(credential.challenge.digest, "sha-256=test")
        self.assertIsNotNone(credential.challenge.opaque)

    def test_changed_amount_currency_expiry_and_duplicate_rejected(self):
        for headers in (
            [header("2600")],
            [header(currency="eur")],
            [header(expires="2000-01-01T00:00:00Z")],
            [header(), header()],
            ["Bearer ignored"],
        ):
            with self.subTest(headers=headers), self.assertRaises(RestockError):
                parse_challenge(headers, 2500)

    def test_product_and_order_allowlist(self):
        self.assertIsNone(public_product({"url": "http://example.com"}))
        self.assertIsNone(public_product({"url": "https://user:password@example.com"}))
        item = public_product(
            {"url": "https://example.com/pens", "price": 1900, "title": "Pens", "secret": "hidden"}
        )
        self.assertNotIn("secret", item)
        self.assertEqual(
            public_order({"status": "pending", "shipping_address": "hidden"}),
            {
                "merchant_status": "pending",
                "failure_reasons": [],
                "shipping_status": [],
                "shipments": [],
                "email_updates_status": "unconfirmed",
            },
        )

    def test_unpaid_fee_discovery_still_validates_currency_amount_and_network(self):
        self.assertEqual(parse_challenge([header("2550")], None)["amount"], 2550)
        for raw in (header("0"), header("-1"), header("9999999999"), header(currency="eur")):
            with self.subTest(raw=raw), self.assertRaises(RestockError):
                parse_challenge([raw], None)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_keeps_status_and_known_code_without_private_response(self):
        for data, expected_code in (
            (
                {
                    "error": {
                        "code": "payment_verification_failed",
                        "message": "private-provider-detail",
                    }
                },
                "payment_verification_failed",
            ),
            (
                {
                    "type": "https://paymentauth.org/problems/verification-failed",
                    "detail": "private-provider-detail",
                },
                "verification-failed",
            ),
            ({"error": {"code": "private-provider-detail"}}, None),
            (None, None),
        ):
            calls = []

            def handle(req):
                calls.append(req)
                return (
                    httpx.Response(402, json=data)
                    if data is not None
                    else httpx.Response(402, text="private-provider-detail")
                )

            with self.subTest(data=data):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    with self.assertRaises(ZincSubmissionError) as raised:
                        await Zinc(client).submit(
                            {}, parse_challenge([header()], 2500), "spt_fixture"
                        )
                details = raised.exception.diagnostic
                self.assertEqual(details["http_status"], 402)
                self.assertEqual(details.get("provider_code"), expected_code)
                self.assertNotIn("private-provider-detail", json.dumps(details))
                self.assertNotIn("spt_fixture", json.dumps(details))
                self.assertEqual(len(calls), 1)

    async def test_known_id_survives_missing_tracking_key(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(201, json={"id": "known_order", "status": "pending"})
            )
        ) as client:
            with self.assertRaises(ZincSubmissionError) as raised:
                await Zinc(client).submit({}, parse_challenge([header()], 2500), "spt_fixture")
        self.assertEqual(raised.exception.merchant_order_id, "known_order")
        self.assertEqual(raised.exception.diagnostic["http_status"], 201)

    async def test_transport_timeout_has_safe_reason_and_no_retry(self):
        calls = []

        def handle(req):
            calls.append(req)
            raise httpx.ReadTimeout("private-provider-detail", request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(ZincSubmissionError) as raised:
                await Zinc(client).submit({}, parse_challenge([header()], 2500), "spt_fixture")
        self.assertEqual(raised.exception.diagnostic["reason"], "transport_timeout")
        self.assertNotIn("private-provider-detail", str(raised.exception))
        self.assertEqual(len(calls), 1)

    async def test_status_for_another_order_is_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, json={"id": "someone_else", "status": "order_placed"}
                )
            )
        ) as client:
            with self.assertRaisesRegex(RestockError, "merchant_order_mismatch"):
                await Zinc(client).status("expected_order", "synthetic-key")

    async def test_unpaid_probe_and_private_submission(self):
        seen = []

        def handle(req):
            seen.append(req)
            if "authorization" not in req.headers:
                return httpx.Response(402, headers={"WWW-Authenticate": header()})
            return httpx.Response(
                201,
                json={"id": "order_fixture", "status": "pending"},
                headers={"X-Api-Key": "fixture-private-key"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            zinc = Zinc(client)
            await zinc.availability()
            self.assertEqual(seen[-1].content, b"")
            body = {"idempotency_key": "fixed-uuid"}
            challenge = await zinc.challenge(body, 2500)
            result, key = await zinc.submit(body, challenge, "spt_fixture")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(key, "fixture-private-key")
        self.assertEqual(json.loads(seen[-1].content), body)
        self.assertNotIn("spt_fixture", seen[-1].url.query.decode())

    async def test_bodyless_rejection_is_inconclusive_but_cart_rejection_stops_payment(self):
        def handle(req):
            self.assertNotIn("authorization", req.headers)
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "unknown_payment_method",
                        "details": {"available": ["tempo", "x402"]},
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual((await Zinc(client).availability())["status"], "cart_required")
            with self.assertRaisesRegex(RestockError, "zinc_stripe_payments_unavailable"):
                await Zinc(client).challenge({}, 2500)

    async def test_bodyless_omits_stripe_but_actual_cart_returns_matching_stripe(self):
        tempo = format_www_authenticate(
            Challenge.create(
                secret_key="synthetic-only",
                realm="api.zinc.com",
                method="tempo",
                intent="charge",
                request={"amount": "2500", "currency": "usd"},
            ),
            realm="api.zinc.com",
        )

        def handle(req):
            self.assertEqual(req.url.path, "/agent/orders")
            self.assertEqual(req.url.query, b"")
            self.assertNotIn("authorization", req.headers)
            headers = [("WWW-Authenticate", tempo)]
            if req.content:
                headers.append(("WWW-Authenticate", header()))
            return httpx.Response(402, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            zinc = Zinc(client)
            self.assertEqual((await zinc.availability())["status"], "cart_required")
            challenge = await zinc.challenge({"max_price": 2400}, 2500)
            self.assertEqual(challenge["amount"], 2500)
            self.assertEqual(challenge["network_id"], "internal_test_network")

    async def test_cart_without_stripe_is_still_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(402))
        ) as client:
            with self.assertRaisesRegex(RestockError, "stripe_challenge_required"):
                await Zinc(client).challenge({"max_price": 2400}, 2500)

    async def test_duplicate_order_looks_up_existing_id(self):
        calls = []

        def handle(req):
            calls.append(req)
            if req.method == "POST":
                return httpx.Response(
                    409,
                    headers={"X-Api-Key": "fixture-key"},
                    json={"error": {"details": {"order_id": "existing_order"}}},
                )
            return httpx.Response(200, json={"id": "existing_order", "status": "order_placed"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result, _ = await Zinc(client).submit(
                {}, parse_challenge([header()], 2500), "spt_fixture"
            )
        self.assertEqual(result["id"], "existing_order")
        self.assertEqual([r.method for r in calls], ["POST", "GET"])

    async def test_redirect_is_not_followed(self):
        calls = []

        def handle(req):
            calls.append(req)
            return httpx.Response(307, headers={"Location": "https://untrusted.invalid"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=False
        ) as client:
            with self.assertRaises(RestockError):
                await Zinc(client).challenge({}, 2500)
        self.assertEqual(len(calls), 1)

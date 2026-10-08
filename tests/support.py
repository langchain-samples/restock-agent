import copy
import time
from types import SimpleNamespace

import tests  # noqa: F401
from langgraph.store.memory import InMemoryStore

from restock.config import RestockError, Settings
from restock.rehearsal import OFFICE, RehearsalZinc
from restock.service import Restock, payment_amount
from restock.storage import Repository


def runtime(caller="alice", thread="conversation", store=None):
    return SimpleNamespace(
        config={"configurable": {"thread_id": thread}},
        store=store or InMemoryStore(),
        backend=SimpleNamespace(),
        server_info=SimpleNamespace(
            principal=SimpleNamespace(id=caller, kind="person") if caller else None
        ),
        stream_writer=lambda *args: None,
    )


class Private:
    def __init__(self):
        self.value = copy.deepcopy(OFFICE)
        self.keys = {}
        self.fail_save = False

    async def office(self, slug):
        return copy.deepcopy(self.value)

    async def zinc_key(self, slug):
        return "synthetic-zinc-key"

    async def save_order_key(self, oid, key):
        if self.fail_save:
            raise RestockError("synthetic_failure")
        self.keys[oid] = key

    async def order_key(self, oid):
        return self.keys[oid]


class Wallet:
    def __init__(self):
        self.requests = {}
        self.created = 0
        self.tokens = 0
        self.fail_create = False
        self.on_token = None

    async def create(self, order):
        self.created += 1
        result = {
            "id": f"lsrq_{self.created}",
            "status": "pending_approval",
            "amount": payment_amount(order),
            "currency": "usd",
            "metadata": {"restock_order": order["id"], "restock_fingerprint": order["fingerprint"]},
            "expires_at": time.time() + 900,
            "approval_url": f"https://app.link.com/approve/{self.created}",
        }
        self.requests[result["id"]] = result
        if self.fail_create:
            raise TimeoutError("synthetic response lost")
        return copy.deepcopy(result)

    async def retrieve(self, rid):
        return copy.deepcopy(self.requests[rid])

    async def history(self):
        return copy.deepcopy(list(self.requests.values()))

    async def cancel(self, rid):
        self.requests[rid]["status"] = "canceled"
        return await self.retrieve(rid)

    async def token(self, rid):
        self.tokens += 1
        if self.on_token:
            self.on_token(self.requests[rid])
        return await self.retrieve(rid), "spt_synthetic_private"


class Merchant(RehearsalZinc):
    def __init__(self):
        self.submissions = []
        self.orders = {}
        self.lose_response = False
        self.next_status = "pending"
        self.email_fee = 0
        self.response_fields = {}

    async def challenge(self, body, amount):
        from restock.zinc import parse_challenge
        from tests.test_zinc import header

        fee = 100 + (self.email_fee if body.get("customer_notifications") else 0)
        return parse_challenge([header(str(body["max_price"] + fee))], amount)

    async def submit(self, body, challenge, token):
        self.submissions.append(copy.deepcopy(body))
        key = body["idempotency_key"]
        record = self.orders.setdefault(
            key, {"id": "zn_order_" + key, "status": self.next_status, **self.response_fields}
        )
        if self.lose_response:
            raise TimeoutError("synthetic response lost")
        return copy.deepcopy(record), "synthetic-order-key"

    async def status(self, oid, key):
        return copy.deepcopy(next(record for record in self.orders.values() if record["id"] == oid))


def make_service(mode="live", rt=None):
    return Restock(
        Repository(rt or runtime()), Private(), Merchant(), Wallet(), Settings(mode, wait_seconds=1)
    )


async def prepared(service):
    search = await service.search("black pens", 2500)
    result = await service.prepare(
        [{"product_id": search["products"][0]["product_id"], "quantity": 1}], 2500
    )
    if result["status"] in {"prepared", "payment_amount_required"}:
        await service.set_payment_amount(result["order_id"], 2500)
    return result["order_id"]


async def approved(service):
    oid = await prepared(service)
    await service.request_payment(oid)
    for request in service.wallet.requests.values():
        request["status"] = "approved"
    return oid

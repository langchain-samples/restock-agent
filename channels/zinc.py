"""Signed Zinc order events. No agent invocation and no payment capability."""

from managed_deepagents import channels

from restock.config import RestockError
from restock.notification_runtime import receiver


async def verify(request):
    try:
        await receiver().authenticate(
            request.raw_body, request.request.headers.get("x-webhook-signature")
        )
        return True
    except RestockError as error:
        if str(error) in {
            "invalid_order_event",
            "invalid_zinc_signature",
            "invalid_update_binding",
        }:
            return False
        # Missing route/secret may be a registration race. Return a temporary
        # failure via MDA instead of acknowledging and losing the event.
        raise RestockError("order_update_temporarily_unavailable") from None
    except Exception:
        raise RestockError("order_update_temporarily_unavailable") from None


async def parse(request):
    from starlette.responses import JSONResponse

    try:
        status = await receiver().receive(
            request.raw_body, request.request.headers.get("x-webhook-signature")
        )
        response = JSONResponse({"status": status})
    except Exception:
        # Never include provider responses, keys, destinations or body contents.
        response = JSONResponse({"status": "retry_later"}, status_code=503)
    return {"type": "ignore", "response": response}


channel = channels.http(provider="zinc", verify=verify, parse=parse)

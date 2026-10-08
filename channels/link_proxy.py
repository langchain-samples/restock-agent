"""Native sandbox credential callback. Never starts a model run or sends a message."""

from managed_deepagents import channels

from restock.link_proxy import SIGNATURE_HEADER
from restock.proxy_runtime import receiver


async def verify(request):
    try:
        await receiver().verify(request.raw_body, request.request.headers.get(SIGNATURE_HEADER))
        return True
    except Exception:
        return False


async def parse(request):
    # The deploy CLI imports channel definitions with authoring dependencies;
    # Starlette is supplied by the generated server runtime, not that environment.
    from starlette.responses import JSONResponse

    try:
        value = await receiver().resolve(
            request.raw_body, request.request.headers.get(SIGNATURE_HEADER)
        )
        response = JSONResponse(value, headers={"Cache-Control": "no-store"})
    except Exception:
        response = JSONResponse({"error": "credential_unavailable"}, status_code=403)
    return {"type": "ignore", "response": response}


channel = channels.http(provider="link-proxy", verify=verify, parse=parse)

"""Deliver an approval link to the current Slack conversation before polling."""

from managed_deepagents import CredentialAuthorizationRequiredError

from restock.service import payment_amount, view


async def before_approval_wait(runtime, service, order_id: str) -> dict | None:
    channel = getattr(runtime, "channel", None)
    if getattr(channel, "provider", None) != "slack":
        return None
    async with service.repo.lock():
        order = await service.repo.order(order_id)
        service.mode_check(order)
        if order["mode"] == "rehearsal" or order["status"] != "awaiting_link_approval":
            return None
        if order.get("slack_approval_posted"):
            return None
        url = order.get("approval_url")
        post = getattr(channel, "post", None)
        if url and callable(post):
            label = "TEST approval" if order["mode"] == "link-test" else "Payment approval"
            text = (
                f"{label}: approve ${payment_amount(order) / 100:.2f} in Link: {url}\n"
                f"I will check for approval for up to {service.settings.wait_seconds} seconds. "
                "If that wait ends, "
                "reply here asking me to check this same order."
            )
            if order["mode"] == "link-test":
                text += " This is a test; no merchant order will be submitted."
            else:
                text += (
                    " This is the chosen upfront payment, not a final checkout quote. "
                    "Zinc refunds any unused amount after retailer checkout."
                )
            try:
                # MDA binds post to the verified current conversation. The model
                # supplies neither a Slack destination nor arbitrary message text.
                await post({"type": "content", "content": text})
            except CredentialAuthorizationRequiredError:
                raise
            except Exception:
                # A failed notification must not hide the URL behind a long wait
                # or replace an already-created payment request.
                pass
            else:
                order["slack_approval_posted"] = True
                await service.repo.save(order)
                return None
        return {
            **view(order),
            "approval_delivery": "reply_required",
            "notice": (
                "The Slack approval message was not confirmed. End this turn with the "
                "existing Link URL and ask the user to reply after approving. Do not "
                "wait again this turn or create another payment request."
            ),
        }

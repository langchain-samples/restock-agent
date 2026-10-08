"""Public order updates. Never expose addresses, email recipients, or private keys."""

import re
from datetime import date
from urllib.parse import urlsplit

# Zinc v2 error-handling reference. Never forward provider prose or unknown
# codes: job results can include private addresses and submitted field values.
FAILURE_MESSAGES = {
    "product_not_found": "The product page could not be found.",
    "product_out_of_stock": "The product was out of stock.",
    "product_unavailable": "The product could not be purchased.",
    "invalid_product_url": "The product URL was not valid.",
    "product_variant_required": "The product needed a variant selection.",
    "product_variant_unavailable": "The selected variant was unavailable.",
    "product_quantity_unavailable": "The requested quantity was unavailable.",
    "max_price_exceeded": "The retailer total exceeded the order's retailer allowance.",
    "add_to_cart_failed": "Zinc could not add the product to the retailer's cart.",
    "cart_empty": "The retailer's cart was empty during checkout.",
    "checkout_blocked": "The retailer blocked checkout, for example with verification.",
    "checkout_failed": "The retailer checkout failed; this code gives no more specific cause.",
    "gift_option_unavailable": "The requested gift option was unavailable.",
    "shipping_address_invalid": "The retailer could not validate the shipping address.",
    "shipping_unavailable": "The retailer could not ship to the configured address.",
    "shipping_method_unavailable": "No shipping method was available.",
    "payment_declined": "The retailer declined the payment used at checkout.",
    "payment_method_invalid": "The retailer did not accept the checkout payment method.",
    "payment_failed": "Payment failed during order processing; the specific cause is unknown.",
    "login_failed": "Zinc could not sign in to the retailer account.",
    "session_expired": "The retailer session expired during checkout.",
    "account_locked": "The retailer account was locked or suspended.",
    "account_verification_required": "The retailer account required verification.",
    "retailer_unavailable": "The retailer website was unavailable.",
    "retailer_not_supported": "Zinc did not support this retailer.",
    "retailer_country_not_supported": "The retailer did not support the destination country.",
    "retailer_rate_limited": "The retailer limited Zinc's requests.",
    "quantity_limit_exceeded": "The retailer's purchase quantity limit was exceeded.",
    "order_limit_exceeded": "The retailer account's order limit was exceeded.",
}


def public_failure_reasons(raw):
    """Return only documented failure codes, with our own fixed explanations."""
    if raw.get("status") not in ("order_failed", "failed"):
        return []
    job = raw.get("job_result")
    job = job if isinstance(job, dict) else {}
    details = job.get("error_details")
    details = details if isinstance(details, dict) else {}
    codes = [details.get("code"), job.get("error_type"), raw.get("error_type")]
    items = raw.get("items")
    for item in items[:100] if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("status") == "failed":
            codes.append(item.get("error_type"))
    recognized = dict.fromkeys(
        code for code in codes if isinstance(code, str) and code in FAILURE_MESSAGES
    )
    return [{"code": code, "message": FAILURE_MESSAGES[code]} for code in recognized]


def short_text(value, maximum=100):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 ._/-]{1," + str(maximum) + "}", value):
        return value
    return None


def tracking_url(value):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\s<>|]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"17track.net", "www.17track.net", "t.17track.net"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        ):
            return value
    except ValueError:
        pass
    return None


def public_delivery(raw):
    shipments = []
    tracking = raw.get("tracking_numbers")
    for item in tracking[:20] if isinstance(tracking, list) else []:
        if not isinstance(item, dict):
            continue
        shipment = {}
        for key in ("carrier", "tracking_number"):
            if text := short_text(item.get(key)):
                shipment[key] = text
        if item.get("status") in ("pending", "in_transit", "delivered"):
            shipment["status"] = item["status"]
        if url := tracking_url(item.get("zinc_tracking_url")):
            shipment["tracking_url"] = url
        estimate = item.get("estimated_delivery_date")
        if isinstance(estimate, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", estimate):
            try:
                shipment["estimated_delivery_date"] = date.fromisoformat(estimate).isoformat()
            except ValueError:
                pass
        if shipment:
            shipments.append(shipment)
    notification = raw.get("customer_notifications")
    email_status = "unconfirmed"
    if isinstance(notification, dict):
        delivered = notification.get("delivered")
        email_status = (
            "delivered"
            if delivered is True
            else "delivery_failed"
            if delivered is False
            else "pending"
        )
    return {
        "shipping_status": [item.get("status", "unknown") for item in shipments],
        "shipments": shipments,
        "email_updates_status": email_status,
    }


def order_update(order, *, detail="summary"):
    """A grounded summary for the agent's normal Slack/Studio response."""
    reference = short_text(order.get("merchant_order_id"), 150)
    if not reference or order.get("mode") != "live":
        return None
    status = order.get("merchant_status", "unknown")
    if status == "order_placed":
        text = "The retailer confirmed your order."
    elif status in {"pending", "in_progress"}:
        text = "Your order was submitted to Zinc. Retailer confirmation is still pending."
    elif status in {"cancelled", "canceled", "cancelled_by_retailer"}:
        text = "Zinc reports that the order was canceled. This does not confirm a refund."
    elif status in {"order_failed", "failed"}:
        text = "Zinc reports that the retailer order failed. This does not confirm a refund."
    else:
        text = "Zinc has not provided a recognized order status. Do not place a replacement."
    lines = [text, f"Zinc order ID: {reference}"]
    if status in {"order_failed", "failed"}:
        reasons = order.get("failure_reasons") or []
        for reason in reasons:
            lines.append(f"Failure reason ({reason['code']}): {reason['message']}")
        if not reasons:
            lines.append(
                "No recognized failure reason is available. Ask the operator to check with Zinc."
            )
        lines.append("The refund status is unverified. Do not automatically retry this purchase.")
    shipments = (order.get("shipments") or []) if detail != "summary" else []
    for item in shipments:
        details = [item[key] for key in ("carrier", "tracking_number") if item.get(key)]
        details.append(item.get("status", "status unavailable").replace("_", " "))
        lines.append("Shipment: " + ", ".join(details))
        if item.get("estimated_delivery_date"):
            lines.append("Estimated delivery: " + item["estimated_delivery_date"])
        if item.get("tracking_url"):
            lines.append("Track: " + item["tracking_url"])
    if (
        detail != "summary"
        and not shipments
        and status in {"order_placed", "pending", "in_progress"}
    ):
        lines.append("Tracking is not available yet.")
    if detail != "summary" and order.get("email_updates_requested"):
        lines.append(
            {
                "delivered": "Zinc reports that an order-update email was delivered.",
                "delivery_failed": "Zinc reports an email delivery failure. Check this thread for order status.",
                "pending": "Zinc reports email updates pending; email delivery is not yet confirmed.",
            }.get(
                order.get("email_updates_status"),
                "Email updates were requested, but Zinc has not confirmed they are enabled.",
            )
        )
    if order.get("tracking_access_status") == "unavailable":
        lines.append(
            "The operator must recover private tracking access for this Zinc ID. "
            "Repeated checks cannot query Zinc yet. Do not place a replacement."
        )
    elif detail != "summary":
        lines.append("Ask me to check this order for later updates.")
    return "\n".join(lines)


def present_order(result, *, detail="summary"):
    """Select chat detail without changing saved state or purchase behavior."""
    if detail not in {"summary", "status", "notifications"}:
        raise ValueError("invalid_order_detail")
    public = dict(result)
    if "order_id" not in public:
        return public
    public["response_detail"] = detail
    if detail != "notifications":
        public.pop("slack_updates_status", None)
        public.pop("slack_updates_notice", None)
    # Email enrollment and fees still belong in the pre-payment disclosure.
    if detail == "summary" and public.get("status") not in {
        "prepared",
        "payment_amount_required",
        "login_required",
        "awaiting_link_approval",
    }:
        for field in (
            "email_updates_requested",
            "email_updates_status",
            "email_updates_notice",
            "shipments",
            "shipping_status",
        ):
            public.pop(field, None)
    public.pop("order_update", None)
    if update := order_update(public, detail=detail):
        public["order_update"] = update
    return public

"""Retailer-wide shipping guidance, never an invented checkout quote."""

from urllib.parse import urlsplit


def domain(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url if "://" in url else "https://" + url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        host = parsed.hostname or ""
        return host.removeprefix("www.").lower() or None
    except ValueError:
        return None


def guidance(url, check, catalog):
    """Keep observed booleans/numbers; exclude provider prose and instructions."""
    host = domain(url)
    check = check if isinstance(check, dict) else {}
    checkout = check.get("checkout")
    checkout = checkout if isinstance(checkout, dict) else {}
    declared = domain(check.get("domain"))
    # A canonical parent domain may identify a store subdomain, never a suffix lookalike.
    matches = host and declared and (host == declared or host.endswith("." + declared))
    if not matches:
        check, checkout = {}, {}
    rows = catalog.get("retailers") if isinstance(catalog, dict) else None
    row = (
        next(
            (
                item
                for item in rows or []
                if isinstance(item, dict)
                and domain(item.get("base_url")) in {host, declared if matches else host}
            ),
            {},
        )
        if isinstance(rows, list)
        else {}
    )
    threshold = row.get("free_shipping_threshold_cents")
    if type(threshold) is not int or not 0 <= threshold <= 10000000:
        threshold = None
    free = row.get("free_shipping")
    free = free if type(free) is bool else None
    if free is True and threshold is not None:
        note = (
            f"Zinc lists free shipping from ${threshold / 100:.2f} at this retailer. "
            "Below that subtotal, shipping may be added. Checkout determines eligibility."
        )
    elif free is True:
        note = "Zinc lists free shipping at this retailer; checkout determines eligibility."
    else:
        note = "Shipping may be added. No applicable free-shipping offer was confirmed."
    return {
        "retailer_domain": host,
        "orderable": check.get("orderable") if type(check.get("orderable")) is bool else None,
        "customer_account_required": (
            not checkout["guest_checkout"] if type(checkout.get("guest_checkout")) is bool else None
        ),
        "free_shipping_offered": free,
        "free_shipping_threshold_cents": threshold,
        "notice": note,
        "final_total_known": False,
    }

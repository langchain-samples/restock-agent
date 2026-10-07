# Working on Restock

Work only in this standalone project. Read README.md for setup,
CONTRIBUTING.md for checks, and docs/architecture.md for the implementation.
Keep MDA 0.8.1, Link CLI 0.22.0, and the configured model unless the owner requests
an upgrade. Use current MDA docs, not the obsolete private-preview REST API.

## Scope

- Use live Zinc products and retailer metadata. No fixed product, retailer,
  recipient, address, or office name belongs in live code.
- Support office supplies and packaged food through retailer shipping. The sample
  uses US delivery, USD, one office per deployment, and the requesting user's wallet.
- Rehearsal is the default and must stay visibly fictional. The operator controls
  the mode; the model cannot change it.
- Keep setup instructions in README.md. Store local experiments and working notes
  in `.local/`, outside the public source.

## Preserve payment and identity behavior

- Resolve the caller from MDA. Never accept a model-supplied owner or use another
  person's credentials. Keep CLI sessions and order-tracking access user-owned;
  office details and the Zinc search key are agent-owned.
- Link CLI performs device login and renewal. The helper saves its session in
  Connections and removes temporary files, including on errors. This is an
  application helper, not MDA-managed OAuth or automatic sandbox credential injection.
- Keep the shopping budget separate from the user's chosen payment amount.
  Preserve native human review, fresh Link approval, exact cart/amount/address
  binding, stable idempotency keys, and one paid submission.
- Verify fees within the chosen total. Do not invent a final tax/shipping quote,
  raise the amount, or change payment methods to bypass a failed challenge.
- Preserve pending login and carts. An uncertain payment or order requires
  reconciliation, never an automatic retry, replacement, or state reset.
- Keep failures and recovery visible. Normal confirmations are brief; requested
  status checks include tracking/email. Placement does not prove shipping or refund.
- Verify webhook signatures and use the saved Slack destination and owner.
  Events cannot choose recipients, start payments, or invoke the model.

## Private state and validation

- Preserve `.local/app/.env`, deployment metadata, notification signing keys,
  existing orders, and saved sessions. Never repeat a purchase as a test.
- Never print credentials, sessions, payment tokens, private addresses, or email
  recipients. Setup tools may consume settings by reference without displaying them.
- Automated checks use fake providers. Real login, payment, orders, Slack messages,
  deployments, and publishing require owner authorization.
- Run `sfw uv run --no-sync python scripts/verify.py` after meaningful code changes.
  For documentation-only changes, check links, packaging, and affected setup commands.
  All installs and MDA build/dev/deploy commands use `sfw`.
- Use `scripts/setup.py` to refresh an app while preserving private settings.
  Use `scripts/package_source.py` for a shareable archive; never zip the whole folder.
- Keep LICENSE and the README's source attribution. Keep hosted and concurrency limits
  accurate; fixtures do not establish live provider behavior.

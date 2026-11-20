# WhatsApp Business and Merchant Checkout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add genuinely configured WhatsApp Business sending and reviewed merchant-cart checkout to the central EDITH action flow without changing its UI.

**Architecture:** Rust owns principal-scoped proposals, immutable approvals, one execution claim and durable evidence. Trusted Python coordinators execute narrow provider adapters after exact human review; models may propose actions but cannot approve, claim or bypass policy. Start production WhatsApp sending with approved templates; keep payment completion in the merchant's checkout.

**Tech Stack:** Existing Python desktop application, Rust/Axum/SQLite hub, Meta WhatsApp Business Cloud API, Shopify Storefront API, pytest and Rust integration tests.

**Spec:** [Household blueprint](../../product-plan/household-agent-blueprint.md), with the user's current choices recorded here: **WhatsApp Business API sending** and **merchant review/checkout handoff**. Personal WhatsApp browser handoff is not an intended feature of this plan.

## Global Constraints

- Preserve the existing UI; extend trusted command handlers and backend contracts only.
- A paired device role is not proof of the human speaker's identity. Restrict these flows to the existing trusted adult/elder session; child, guest and public Telegram paths cannot execute them.
- Never read or pass access tokens, app secrets, card details, OTPs or customer payment credentials to a model or ordinary logs.
- No actual account connection, message send, checkout opening or purchase is part of implementation tests. Use injected transports and synthetic fixtures.
- No implicit retries after an ambiguous mutating HTTP request, result-write failure or process restart. Reconcile unknown outcomes before another action.
- Use explicit provider version pins: the current WhatsApp transport target is `v26.0`, not a claim that it is latest; Shopify adapter uses `2026-10`. Recheck support before deployment. [Meta versions](https://developers.facebook.com/docs/graph-api/changelog/versions/)
- Commit changes only under the parent task's repository policy; this document does not authorize commits or external effects.

## Current checkpoint and source constraints

The current WhatsApp work is a **standalone text transport foundation**, default-deny and unregistered. It becomes usable only when a trusted policy supplies recipient consent and service-window authorization. There is no live business account, production template workflow, sender verification, signed-webhook receiver or delivery verification yet. Optional phone contacts are implemented in Rust schema 6. Do not label configuration or synthetic tests as a connected service.

The existing `connectors/shopify.py` can read catalog data and prepare a cart through an injected or configured transport. It does not have a central commerce journal or trusted checkout command yet, and no production merchant has been connected.

Meta requires recipient opt-in. Its service window starts/resets on an incoming customer message or eligible call and lasts 24 hours; outside that window, outbound messages require approved templates. The first EDITH window implementation will support verified inbound **messages only**, leaving call-derived eligibility unsupported. HTTP send acceptance does not establish delivery. [Meta sending rules](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages)

Templates must be `APPROVED`; name, language, content, parameters and current status matter. Begin with one configured body-text template using bounded positional text parameters; reject other component types until explicitly implemented. Template categories and provider charges remain visible configuration, not a promise of free messaging. [Meta template rules](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview)

Shopify cart totals are estimates and can change at checkout. A prepared cart or opened checkout is not an order or payment receipt. [Shopify CartCost](https://shopify.dev/docs/api/storefront/latest/objects/CartCost)

## Review Focus

- Account switched after review: reject the stale proposal; show and approve the newly verified sender (Tasks 1–3).
- Contact permission revoked or template changed during a blocking request: recheck before dispatch and require fresh review (Tasks 1–3).
- Provider accepted but response/journal write was lost: retain `unknown`, never automatically send or create a cart again (Tasks 2–4).
- Webhook replay, wrong business number or forged timestamp: never open/extend a service window (Task 5).
- Merchant substitutes quantities, currency or checkout origin: refuse handoff; a changed quote needs fresh review (Task 4).

## Task 1: Verified business account and template policy

**Files:** Create `connectors/whatsapp_account.py`, `connectors/whatsapp_templates.py`, `tests/test_whatsapp_account.py`, `tests/test_whatsapp_templates.py`; extend `connectors/whatsapp.py` and `tests/test_whatsapp.py` after the standalone transport is frozen.

**Interfaces:** `BusinessAccount.snapshot() -> dict` returns safe verified `connection_id`, immutable `version`, `principal_id`, business/messaging account ID, `phone_number_id`, display number and `verified_at`; `credential_provider() -> str` remains worker-private. `TemplateCatalog.get(template_id, language) -> dict` returns verified current template content/status plus `content_hash`. `TemplatePolicy.authorize(binding: dict) -> bool` defaults to false and consults current trusted account, contact permission and template state.

- [ ] Write red tests: `test_unconfigured_never_connected`, `test_wrong_sender_or_principal_denied`, `test_secret_never_in_snapshot_or_error`, `test_nonapproved_or_changed_template_denied`, `test_contact_without_permission_denied`. Assert denied paths make zero send calls.
- [ ] Run the two new focused test files; confirm failures are missing behavior rather than test setup errors.
- [ ] Implement bounded fixed-origin account/template reads, protected credential storage and account-version invalidation. Reuse Gmail storage principles, not its Google-specific OAuth flow. Support one explicitly selected messaging account; reject ambiguous account attribution. Provider setup and verification are explicit, never import/startup side effects.
- [ ] Add a template send method that accepts only a trusted template snapshot and validated parameters. Recheck policy after credential lookup and immediately before POST; preserve the existing no-proxy/no-redirect/no-retry transport boundary. Do not enable free-form text yet.
- [ ] Run account/template/transport tests; expected all pass. Record synthetic verification separately from live setup availability.

### Task 1 implementation status — 20 November 2026

Account/template/token-file foundations and the read-only verification CLI are implemented and independently reviewed. `TemplateCatalog.prepare(...)` returns the exact account/template snapshot, parameters and rendered text; `TemplateSender.send_template(...)` defaults to denial without trusted policy and approval. Synthetic tests pass. Trusted consent/window storage and central sending integration remain Tasks 2, 3 and 5; no live account is connected.

## Task 2: Durable business-message proposals

**Files:** Create `hub/src/business_messages.rs`, `hub/tests/business_messages_contract.rs`; extend `hub/src/lib.rs`, `hub/src/personal_routes.rs`, `brain/hub.py`, `tests/test_hub.py`.

**Interfaces:** Add `/v1/business-messages` propose/list/get/decision/claim/complete operations, matching existing journal lifecycle. Python methods: `propose_business_message(payload, key)`, `business_messages()`, `business_message(id)`, `decide_business_message(id, decision, hash)`, `claim_business_message(id)`, `complete_business_message(id, status, evidence)`.

The immutable `contact.whatsapp_template` payload binds connection ID/version, verified business account and phone-number IDs, contact ID/version/exact E164, permission-record version, template ID/name/language/content hash, exact parameter array and rendered readback text. The hub verifies bindings against trusted stored records; ordinary model proposal arguments cannot invent verified account/template/permission metadata. Keep these records owner-scoped. Copy existing 300-second approval expiry, exact hash and single-claim invariants.

- [ ] Write red Rust contract tests for cross-principal denial, forged sender binding, stale permission/contact versions, payload hash changes, duplicate claims, restart of `executing` to `unknown`, immutable evidence and receipt idempotency.
- [ ] Run `cargo test --manifest-path hub/Cargo.toml --test business_messages_contract`; observe the missing contract failures.
- [ ] Implement states `awaiting_approval`, `approved`, `rejected`, `executing`, `accepted`, `failed`, `unknown`. Evidence binds provider, sender, exact requested recipient and provider message ID; `accepted` requires a real validated receipt and initially retains `delivery_status=unverified`. Provider pacing is separate evidence, not delivered status.
- [ ] Add Python contract tests for malformed/substituted rows; run Rust contract and focused HubClient tests until green. Never add an automatic worker replay of `executing` or `unknown`.

## Task 3: Trusted review and one send attempt

**Files:** Create `connectors/whatsapp_business_coordinator.py`, `brain/whatsapp_business_review.py` and corresponding tests; extend `main.py`, `brain/access.py`, `brain/household_tools.py` with integration tests.

**Interfaces:** `BusinessMessageCoordinator.prepare(payload, key) -> dict`; `decide_and_execute(id, hash, approve, *, can_execute) -> dict`. `BusinessMessageReview.handle(text, *, confirm, still_current) -> str | None` and `.clear()` follow `MessageReview`. Exact commands: `review business message [id]`, `send reviewed business message`, `reject reviewed business message`.

- [ ] Write red tests asserting full verified sender/contact/phone/rendered-content readback, fresh explicit confirmation outside the model, no send after identity/generation change, account switch, revoked permission or template change; no restart reuse of cached review.
- [ ] Run review/coordinator tests and observe expected failures.
- [ ] Implement capability checks before/after blocking calls, clear review cache before execution, claim once, call the template adapter once and persist its evidence once. A lost receipt produces `unknown`; response text must not say delivered/read. Keep model tools proposal-only and remote/child/guest registries unable to send.
- [ ] Run review/coordinator/access/main integration tests using fakes. A genuine test send is a separate later user-authorized action after real setup and exact recipient/content review.

## Task 4: Common journal semantics for merchant checkout

**Files:** Create `hub/src/commerce.rs`, `hub/tests/commerce_contract.rs`, `connectors/commerce_coordinator.py`, `brain/commerce_review.py` and corresponding Python tests; extend `hub/src/lib.rs`, `hub/src/personal_routes.rs`, `brain/hub.py`, `main.py`, `brain/access.py`.

**Interfaces:** `CommerceCoordinator.prepare(merchant_id, lines, quote, key) -> dict`; `decide_and_prepare_cart(id, hash, approve, *, can_execute) -> dict`; `CommerceReview.handle(text, *, confirm, still_current) -> str | None`. Reuse journal invariants from Task 2, with typed `commerce.prepare_cart` payload/evidence rather than treating commerce as a message.

- [ ] Write red tests for owner isolation, merchant-version changes, stale quote, exact variants/quantities/currency, claim races, response substitutions, unsafe checkout URLs, unknown cart creation and restart without replay.
- [ ] Run commerce contract/coordinator tests; confirm expected failures.
- [ ] Implement trusted merchant connection records with fixed Shopify domain and exact checkout-host allowlist. Bind immutable variants, quantities, catalog observation/quote and account version to approval. Keep adapter limits: at most 10 distinct lines, quantities 1–10 and total units at most 50. Re-fetch and require fresh review when the quote changes; never silently raise an approved estimate.
- [ ] After one approved cart-create attempt, journal `prepared`, `failed` or `unknown` with validated merchant evidence. Keep cart keys and checkout URLs private, outside logs/model context. The separate exact command `open reviewed checkout` rechecks the stored cart, trusted origin and session before opening; it does not click Pay or collect payment fields. Unknown browser-launch state is not automatically replayed.
- [ ] Run Shopify plus commerce and main integration tests with fake transports/openers. Report only cart prepared/checkout opened; final price, delivery details and payment remain for the user to verify at merchant checkout. Order/refund tracking requires a future authenticated order source.

### Task 4 implementation status — 20 November 2026

Implemented schema7 merchant/cart journal with separate cart and checkout claims, `CommerceCoordinator.decide_and_prepare(...)`, protected `MerchantAccount`, local model proposal tools, exact `CommerceReview` commands and existing-UI routing. Re-quotes and current-cart reads precede effects; unknown effects do not replay. Optional local merchant setup CLI and owner-only launcher selection are present. The user selected their own Shopify store; exact domain/account setup remains pending. See the current [verification report](../../product-plan/implementation-progress.md) for final suite counts and explicitly synthetic provider/browser acceptance.

## Task 5: Signed status events and later free-form eligibility

**Files:** Create `hub/src/whatsapp_events.rs`, `hub/tests/whatsapp_events_contract.rs`; extend business-message storage/routes, account policy and adapter/coordinator tests.

**Interfaces:** Dedicated bounded HTTPS event ingress verifies raw payload signatures before parsing, then writes authenticated events to the hub through a separately scoped credential. `ServiceWindowPolicy.authorize(binding, now) -> bool` is the only production policy enabling the standalone text transport. Public webhook access must not expose ordinary hub routes.

- [ ] Write red tests for wrong/missing signature, changed raw body, wrong account/phone/recipient, duplicate and out-of-order events, future timestamps, restart persistence, permission revocation and window expiry while awaiting confirmation.
- [ ] Run the event contract tests and observe failures.
- [ ] Implement constant-time `X-Hub-Signature-256` HMAC verification with the configured app secret. GET challenge/verify-token handling is a separate setup check, not POST authentication. Persist before acknowledging; deduplicate provider events. [Meta webhook verification](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/)
- [ ] Persist minimal inbound-message metadata scoped to verified business sender and exact customer; derive `window_expires_at = latest_valid_inbound_timestamp + 86400`. Reject unreasonable future timestamps; replay and delivery-status events cannot extend eligibility. Check both current permission and window immediately before send. An expired free-form proposal requires a newly reviewed approved-template proposal; never substitute silently.
- [ ] Reconcile status events only against exact stored provider message IDs/account/recipient. Preserve event history for conflicting/out-of-order delivery facts and never convert accepted to delivered without provider evidence. Run event/restart/coordinator tests to green before registering free-form execution.

## Completion evidence and limits

Run the relevant Rust contracts and Python focused suites after each task, then the consolidated repository checks once owners freeze their files. Record actual pass counts and any failing or skipped checks. Configuration, provider verification, consent records, template readiness, webhook health and last confirmed delivery state must remain distinct. This plan does not establish live connectivity, universal identity verification, exactly-once delivery, completed payments or guaranteed provider pricing.

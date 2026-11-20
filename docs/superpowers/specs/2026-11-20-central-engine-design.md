# EDITH central engine, using the existing interface

The active user goal is the complete household assistant: advanced speech, Rust coordination, RAG, home control, coding, child play/learning, elder assistance, orders/payments, contacts and recommendations. **No new UI.** Reuse `ui/overlay.py` and `ui/orb.html`; their existing transcript can present results. This goal supersedes the earlier pause for backend work, but does not authorize replacing the interface or presenting mock external actions as real.

## Approach

Reuse the Python voice/model/skill implementation and add a local Rust authority. Rewriting all inference and integration code in Rust adds migration work without improving permissions or capabilities. A Python-only extension would not deliver the requested Rust coordination. Keep the unfinished `prototype/` as historical development work; its switchable demo identities, fake devices and synthetic purchases are not the product backend.

`hub/` is the real Rust service. Python calls it through `brain/hub.py`; registered knowledge skills connect it to the existing agent and voice interface. SQLite gives local transactional storage and FTS5 retrieval without a separate database service. Exact principal scopes apply before ranking or returning text. The second implementation milestone adds optional local semantic vectors and permission-filtered hybrid fusion; lexical fallback stays explicitly labelled. Scoped code read/proposal tools are implemented, while verified isolated worker execution and broader workflows remain later stages. See the current implementation-progress document for verification rather than treating this initial design as a release claim.

## First implementation boundary

- Bind only `127.0.0.1:8788`. A local config maps independent random bearer tokens to principal IDs and adult/elder/child/guest roles. No request parameter, voice claim or profile button can change that mapping.
- Config: `{database_path, bind, principals:[{id,token,role,enabled}]}`. Reject duplicate identities/tokens and weak tokens. Never log tokens or document contents. This authenticates a paired local client, not the identity of every speaker in a room.
- `GET /health`; authenticated `GET /v1/capabilities`; `GET/POST /v1/documents`; `DELETE /v1/documents/{id}`; `POST /v1/search`.
- Documents contain title, content, owner, visibility (`private` or `household`), child suitability and version hash. Adults/elders may write; only an owner deletes. Adult household membership does not grant another adult's private notes. Children/guests receive only explicitly child-approved household documents.
- Chunk text with stable citations and source version; persistent SQLite FTS5; bound content/query/result sizes; quoted and Unicode search must work without query injection. Deletion removes retrievable chunks transactionally.
- Python's hub transport uses loopback only, no ambient HTTP proxy, no redirects, explicit timeouts and bounded JSON. Handle missing/disconnected service honestly through the existing spoken response.
- Private retrieval remains local in the model loop. Remote model calls must never receive returned document text by an accidental follow-up or fallback. Tool metadata and agent context track this requirement across the current turn/history.
- Exact affirmative parsing replaces substring approval. Mute must discard audio at capture, clear transitions and bound buffering. Desktop targeting and taught recipes must be validated before any input or image disclosure.

## Complete objective still required

1. Voice cancellation through generation, TTS and tools; language/noise evaluation; room sessions and paired identities.
2. Scoped RAG ingestion, evidence/citations, semantic/hybrid search, document updates and revocation, memory migration.
3. Central policy and durable task journal; action-bound approvals, restart/retry/unknown-outcome reconciliation; every entry point, including Telegram, follows policy.
4. Actual Home Assistant adapter, configured devices, allowed services and observed state verification.
5. Coding workspace discovery, source explanation, patches, isolated execution, meaningful tests/diffs and publication boundary.
6. Child games/learning and parent controls; elder accessibility through existing UI/audio, durable reminders and voluntary delegation.
7. Merchant carts, item/price/recipient approval, trusted payment handoff and authoritative receipts/refunds/tracking.
8. Account-scoped contacts/messages/calls, delivery status, sourced current recommendations.
9. Installation/configuration, secrets, backups, redacted diagnostics, model budgets and measured end-to-end acceptance.

A completed foundation is progress, not completion of this list. Real device/account/merchant configuration is needed for live verification. Do not purchase, send personal messages or operate appliances just to test code.

## Evidence

Use test-first regressions for authorization, private-context routing, capture privacy, recipe confinement and focus failure. Test the Rust/Python boundary over a real loopback server using synthetic documents. Run full available suites with isolated test directories and mocked device/provider effects, report unavailable native runtimes or skipped live checks. Preserve UI file hashes. No new cloud service or subscription is needed for this foundation.

Primary interface references: [rusqlite](https://github.com/rusqlite/rusqlite), [Home Assistant REST API](https://contributors.home-assistant.io/core/internals/rest-api/). Home control must call the service endpoint and then observe device state; writing a state object does not operate a device.

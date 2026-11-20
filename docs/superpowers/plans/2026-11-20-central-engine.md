# EDITH Central Engine Implementation Plan

> For agentic workers: use subagent-driven development for independent backend components, with root integration and independent review. Track evidence, not intended status.

**Goal:** implement the complete household assistant behind the existing EDITH UI, starting with real Rust authority and scoped RAG.

**Architecture:** existing Python speech/model/skills call a loopback Rust service. SQLite persists principal-owned documents and their retrieval index. Later connectors and actions share the same identity and policy authority.

**Tech stack:** Rust/Axum/Tokio/rusqlite, Python, current model/voice adapters. No frontend replacement.

**Spec:** [central-engine-design](../specs/2026-11-20-central-engine-design.md), [full household blueprint](../../product-plan/household-agent-blueprint.md).

## Global constraints

- Keep `ui/` appearance and interaction intact; no new frontend.
- Keep secrets and test/model/build data off the formatted SSD; use D: for development tooling and test state.
- Do not start microphone capture, real desktop inputs, device changes, external payments or personal messages as automated tests.
- No PC shutdown or rearming the cancelled shutdown monitor.
- Keep the whole goal active until the full objective has been implemented and verified.

## Review focus

- Cross-user documents must be excluded before retrieval/model prompting (Task 1).
- Private tool output must not reach a remote follow-up model or survive into an unscoped session (Task 2).
- Negated speech is not approval; muted audio cannot remain buffered (Task 3).
- Failed focus and malformed recipes must produce no input/crop upload (Task 4).
- Service restart, deletion and disconnection must not produce false success (Tasks 1, 2 and 5).

## Task 1 — Rust identity and persistent knowledge

Files: `hub/Cargo.toml`, `hub/src/{lib,main,config,store,api}.rs`, `hub/tests/knowledge.rs` (implementer may split focused modules).

Interfaces: config and HTTP contract in design. Search returns `{citations:[{document_id,chunk_id,title,excerpt,score,source_version}]}`; create returns document metadata with `id` and `source_version`; delete returns `{deleted:true}`.

- [x] Write/run failing integration tests for invalid credentials, private isolation, child approval, owner deletion, Unicode/quoted queries and persistence across restart.
- [x] Implement validated config, authenticated routes, bounded ingestion/chunking and SQLite scoped retrieval/deletion.
- [x] Run locked Cargo tests/build; 17 integration tests now include the journal and real-process ownership.

## Task 2 — Existing agent to Rust bridge

Files: `brain/hub.py`, `skills/knowledge.py`, `brain/agent.py`, `brain/llm.py`, `skills/__init__.py`, `main.py`; tests `tests/test_hub.py`, `tests/test_knowledge.py`, `tests/test_agent.py`.

Interfaces: `HubClient.from_env()`, `.capabilities()`, `.documents()`, `.add_document(title,content,visibility,child_allowed)`, `.search(query,limit=5)`, `.delete_document(id)`; `Skill.local_only=False`; `LLM.chat_local(messages,tools)` for scoped private context. No credentials returned in exceptions.

- [x] Add failing tests using a real synthetic loopback HTTP server and scripted models: bearer transport, redirects/proxies rejected, bounds/errors, source citations, private-context local routing over subsequent turns.
- [x] Implement transport and knowledge tools, integrate registry and existing startup; paired sessions are local from the first turn and private tool context cannot fall back to cloud.
- [x] Run focused tests and full Python suite: 399 passed, 1 Windows symlink-privilege skip; real Rust/local-model acceptance included.

## Task 3 — Approval and microphone privacy

Files: `brain/approval.py`, `voice/stt.py`, possibly `voice/tts.py`; tests `tests/test_approval.py`, `tests/test_stt.py`.

- [x] Reproduce negated-yes acceptance and muted/unbounded capture with failing synthetic tests.
- [x] Implement strict affirmative parsing, bounded queues and capture-side muted discard/transition clearing while preserving overlay's Event interface.
- [x] Integrate `main.is_yes`; verify behavior without microphone or audible output. Full TTS/tool interruption is still open.

## Task 4 — Desktop execution boundaries

Files: `skills/screen.py`, `skills/teach.py`; tests `tests/test_screen_focus.py`, `tests/test_teach.py`.

- [x] Reproduce false-positive focus and invalid/traversing recipe acceptance with failing tests.
- [x] Verify foreground before typing; validate/revalidate bounded recipes and confine assets before replay/upload; stop capture on invalid finish.
- [x] Run fake-desktop regression tests with no real input or capture.

## Task 5 — Integration and remaining domain work

- [x] Add setup/start instructions and a launcher using existing UI; verify UI files unchanged. Actual ACL initialization and configuration-only launcher check passed.
- [x] Exercise real Rust HTTP service from Python with synthetic owner/child documents, search and restart/delete checks; record evidence.
- [ ] Implement central policy/action journal, pairing/session identity and all-entry-point enforcement.
- [ ] Implement Home Assistant, isolated coding, child/elder services, commerce/payment and scoped communications with appropriate provider contracts and tests.
- [ ] Evaluate actual voice/languages, configured devices/accounts and end-to-end outcomes. Keep requirements without live evidence open.

Do not commit incomplete components simply to satisfy a plan step. Review and save working changes; any push/publication is separate from local implementation.

## Second backend milestone — 20 November 2026

- [x] Add persistent model/version-bound vectors, atomic owner-only indexing, optimistic source updates and permission-filtered hybrid retrieval. Verify source access before/after inference and before tool calls.
- [x] Install the local Nomic embedding model on D: and verify a synthetic synonym search through the actual Rust service. Keep app downloads explicit and model inference local.
- [x] Replace blocking TTS with a bounded cancellable worker; bind capture, queued agent admission, confirmation and speech output to cancellation generations. Integrate the existing mute control without UI edits.
- [x] Add fixed child games/fallback, per-game limits and elder wording. Child requests bypass the open model; local speech-rate commands work independently of inference.
- [x] Add configured project read/search/diff proposals, bounded snapshots and trusted hash-bound apply API. Exclude application and execution from model tools. Report the missing verified sandbox honestly.
- [x] Complete independent review fixes and final verification, including stale queued admission, post-mute status speech, document-envelope limits and Windows source/staging boundaries. Final full run: 582 passed, 4 environment-dependent skips; actual protected project staging creation/reopen passed.
- [ ] Finish verified isolated execution, trusted member/approval interaction, durable reminders, commerce/payment and contact connectors, live acoustics and operational packaging. Home Assistant setup remains deferred.

Current final test counts and limitations are maintained in `docs/product-plan/implementation-progress.md` and `central-engine-verification.json`, rather than replacing the first milestone's historical measurements above.

## Third backend milestone — reminders and communications

- [x] Persist owned one-time reminders, claim leases and delivery receipts; test restart, concurrent claim, cancellation, acknowledgement and expiry without live audio.
- [x] Add contact revisions and exact-recipient email proposals; bind approval hashes, single claims and unknown-outcome recovery. Extend contacts to optional email/phone, preserving older records.
- [x] Integrate local reminder/contact tools, trusted email review commands and full-readback completion before confirmation. Preserve accepted wake audio state through the shared conversation lock and exclude self-speech.
- [x] Add optional per-member Gmail OAuth/refresh, fixed sender readback and protected atomic credential persistence; keep personal credentials separate and do not connect a real account in tests.
- [x] Verify an installed local model creates the exact confirmed synthetic reminder in the real Rust service and that it survives restart.
- [x] Add a standalone Shopify catalog/cart adapter with bounded data and validated checkout links. No paid-order or completed-checkout claim.
- [x] Add a default-deny, unregistered WhatsApp Business text transport after the user selected Business API sending. Preserve configured-policy/journal/account prerequisites; do not substitute personal-app handoff.
- [x] Complete milestone 3 consolidated verification (historical; current counts are in implementation-progress.md): 995 Python tests passed, 4 environment-dependent skips, and 42 Rust tests passed. Real local RAG, exact confirmed reminder creation, restart persistence and unchanged UI sources were verified.
- [x] Implement own-store Shopify cart/checkout journal, protected configuration and exact existing-voice review; synthetic full-path acceptance passed. Live store setup is pending.
- [ ] Complete the user-selected WhatsApp Business consent/message journal and voice flow using the [next-step plan](2026-11-20-whatsapp-business-checkout.md); account/template foundations are implemented.
- [ ] Complete live voice/account/device acceptance, member authentication, isolated coding execution, broader RAG/parent controls and operational packaging. Keep the full objective active.

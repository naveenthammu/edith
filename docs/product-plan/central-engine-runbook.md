# Running the central engine with the existing EDITH interface

This backend work uses the current `ui/overlay.py` and `ui/orb.html`. No new frontend is required. It is an implementation in progress, not the completed household product.

## Two current execution modes

- With no `EDITH_HUB_CLIENT_CONFIG`, EDITH retains its personal desktop mode. Its existing host scripts, desktop automation and account integrations do not provide household isolation.
- With a paired hub client, speech/model processing starts locally. Shared guests get public information and explicitly child-approved household knowledge. Child mode runs curated offline games and fixed fallback responses, with no open model/tool calls. Adult/elder clients additionally get owned knowledge, reminders, contacts/email drafts and, when configured, project read/search/patch-proposal tools. An explicitly connected member Gmail account can submit a reviewed email through the trusted command flow. Unscoped legacy memory, macros, host scripts and personal account integrations remain excluded. Adult and elder authority is equal; age does not remove agency.

These are server-mapped client credentials, not voice authentication. A shared microphone must use the guest client. Never assume that possession of an adult token proves which person is speaking. The Telegram entry point now accepts only the configured sender in their private chat and exposes public information tools; it cannot inherit the desktop's private knowledge or host scripts.

## Local setup

Use an unsynchronized, access-restricted directory. Setup refuses existing directories and restricts access before writing tokens. Do not put the generated configs in Git, OneDrive, chat or logs.

```powershell
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.hub_admin initialize --data-dir 'D:\CodexTools\edith-household'
```

The setup creates `hub-config.json`, an owner/adult client file and a shared/guest desktop client file. They contain credentials. The SQLite database is ordinary unencrypted SQLite protected by directory permissions; disk encryption and protected backup storage remain installation decisions. It is not a substitute for OS account separation.

The current development Rust binary is `D:\CodexTools\edith-rust\central-target\debug\edith-central-hub.exe`. Its locked source/build and HTTP contract are in [hub/README](../../hub/README.md). No sample household data is seeded.

For separate backend administration:

```powershell
$env:EDITH_HUB_CONFIG='D:\CodexTools\edith-household\hub-config.json'
& 'D:\CodexTools\edith-rust\central-target\debug\edith-central-hub.exe'
```

In another terminal, import only files you choose:

```powershell
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.hub_admin import --client-config 'D:\CodexTools\edith-household\owner-client.json' --file 'D:\Selected\manual.md'
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.hub_admin search --client-config 'D:\CodexTools\edith-household\owner-client.json' --query 'router restart'
```

The importer accepts one UTF-8 `.txt` or `.md` file up to 1 MB. Default visibility is private. `--visibility household --child-allowed` permits the child/guest server identities to read a source; the current child voice mode deliberately exposes only games. PDF parsing and folder watches remain further work.

Optional semantic indexing uses the locally installed `nomic-embed-text:v1.5` model. It is installed on this development PC under the D: Ollama model directory. Index an explicitly selected document ID from the import receipt:

```powershell
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.hub_admin index --client-config 'D:\CodexTools\edith-household\owner-client.json' --document-id '<document-id>'
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.hub_admin search --client-config 'D:\CodexTools\edith-household\owner-client.json' --query 'router restart' --semantic
```

No document is implicitly crawled or uploaded to a cloud. Indexing exports owner-only chunks, embeds bounded batches locally, then commits the complete source version atomically. Vectors bind the model's installed digest, so changing a model requires reindexing. The adapter uses Nomic's query/document prefixes and disables silent truncation, following the [model card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) and [Ollama embedding API](https://docs.ollama.com/api/embed).

The Rust hub combines permission-filtered FTS5 and cosine similarity using reciprocal-rank fusion. Search explicitly falls back to keywords when embeddings are unavailable; unindexed documents remain keyword-searchable. Exact vector scoring is suitable for this small local library, not a large-scale vector-service benchmark. Updates (including title/sharing changes) advance an opaque source revision and remove old vectors. Python rechecks retained sources and the paired identity before/after model calls and before tools. A changed, deleted, revoked or unverifiable source clears the conversation context and discards an in-flight answer. The checks cannot retract content already spoken or displayed, or revoke access halfway through a blocking inference call; they prevent reuse/output at subsequent checked boundaries.

## Existing interface and local model

The launcher uses the existing interface and an installed local Ollama model. It does not install a model or configure a cloud account. Start Ollama with its model directory and cloud disabled, then use `tools/start_household.ps1`. `-CheckOnly` checks configuration without opening the UI or microphone. `-OwnerSession` deliberately chooses the adult credential; use this only in a trusted private session.

```powershell
$env:OLLAMA_MODELS='D:\CodexTools\edith-ollama\models'
$env:OLLAMA_HOST='127.0.0.1:11434'
$env:OLLAMA_NO_CLOUD='1'
& 'D:\CodexTools\edith-ollama\runtime\ollama.exe' serve
```

```powershell
.\tools\start_household.ps1 -CheckOnly
.\tools\start_household.ps1
```

The launcher parameters allow other Python/binary/data locations. The Python runtime must also have the voice/UI dependencies in `requirements.txt`; automated backend tests do not establish microphone, Windows speech or WebView readiness. First speech startup may download Vosk/Whisper assets into `EDITH_DATA_DIR`; an Ollama configuration avoids downloading a second GGUF through llama-cpp-python. Never call a missing local model an offline-ready installation.

## Speech and configured family modes

The existing mute button invalidates buffered/transcribing audio, current/queued agent turns, and current/queued TTS. Stale answers and confirmations are rejected by generation tokens. Cancellation stops later work; it cannot undo a tool already in progress or forcibly interrupt a blocked native call. This is mute-driven cancellation, not an evaluated acoustic barge-in detector.

Say `speak slower`, `speak faster` or `normal speech speed` to change the local speech rate without a model request. Native voice speed is approximate. The TTS backend also exposes repeat of the last successfully completed utterance; it is not a direct unvalidated private-history replay tool.

A configured child identity can say `start word game` or `start math game`. Questions are fixed, with five questions and at most twelve active inputs per game. Unsupported child requests get a fixed parent/guardian-oriented response. The game engine does not retain answers, but the existing console/transcript still displays inputs. Session limits are per game, not a parent-controlled daily quota. Child/elder identities are not created automatically and are not inferred from a voice. Elder mode adds patient, single-step wording; voluntary delegation remains future work.

## Durable reminders

An adult/elder private session can create, list, cancel and acknowledge its own one-time reminders. Creation requires a date/time with an explicit UTC offset and readback confirmation. Identical owner/text/UTC-time requests reuse a record, including across restart; cancelling that record does not make the same request create a new one. Choose a new time for a new reminder. Ambiguous natural-language dates should be clarified before confirmation.

The dispatcher checks for due reminders every five seconds while EDITH is running. It requires the same authenticated member, an unmuted microphone, idle speech and the conversation lock. It claims one record for 120 seconds, then records `announced` only after tracked TTS completion and fresh authorization. Cancellation or failed speech releases the record for retry. Uncertain outcomes wait for the lease rather than immediately repeating speech. The wake recognizer drops EDITH's own speech and echo tail, and carries the accepted audio's generation/speech snapshot into the first-listen handoff.

This is at-least-once announcement: a crash after speech but before its receipt can cause a later duplicate. `announced` does not prove someone heard it; `acknowledged` is a user report and does not prove a task was performed. Reminders persist while the app is closed and can announce when the private session next runs; this does not wake a sleeping PC or provide recurring schedules. Native acoustic behavior still requires local user acceptance testing.

## Member contacts and reviewed email

The model can find/add/update/delete the member's own contacts and prepare an email draft. Mutating contacts requires confirmation; duplicate names return choices rather than a silently selected recipient. The Rust journal binds the exact recipient, subject/body and contact version. Editing or deleting a contact invalidates approval/claim of an old draft. Historical receipts retain the recipient of the original attempt.

Sending uses commands handled outside the language model:

1. Ask EDITH to prepare a draft for a selected contact.
2. Say `review email`. If several drafts are pending, use `review email <eight-character ID>` from the displayed choices.
3. Say `send reviewed email`, listen to the full recipient/subject/body readback, then explicitly confirm. Say `reject reviewed email` to reject the proposal instead.

Draft approval expires five minutes after creation. The complete readback must finish before confirmation is listened for. Mute/session changes cancel admission; generation and identity are rechecked before approval, claim and provider submission, including after credential refresh. These controls still require a trusted private adult/elder session; they are not speaker authentication.

Each approved proposal permits one claimed provider attempt. A provider timeout, lost result receipt or interrupted execution is `unknown` and is not automatically resent. `accepted` means Gmail returned a message ID, not that the recipient received/read it. The adapter follows Google's [send API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send) and [MIME sending contract](https://developers.google.com/workspace/gmail/api/guides/sending). It does not send attachments, add recipients, poll inboxes or place calls.

Household Gmail setup is explicit and separate from the personal-mode credentials. `EDITH_GMAIL_ACCOUNT_CONFIG` points to the resulting protected `account.json`; its verified account must be bound to the exact paired adult/elder. Runtime startup never starts browser authorization. An invalid account disables sending while preserving local draft review. The owner launcher uses `D:\CodexTools\edith-household\gmail\account.json` when present; guest launches clear the Gmail setting. No account has been connected during implementation. Gmail is optional; the user selected WhatsApp Business as the first contact channel.

For a deliberately selected Gmail connection, start the hub, obtain a Google Desktop OAuth client configuration, and run the explicit CLI below. The parent directory must exist; setup refuses to overwrite an existing account directory. This command starts browser authorization and is not an automated test:

```powershell
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.connect_household_gmail --hub-client-config 'D:\CodexTools\edith-household\owner-client.json' --oauth-client-config 'D:\Selected\google-desktop-client.json' --account-dir 'D:\CodexTools\edith-household\gmail'
```

Setup uses Google's [installed-app OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app), PKCE, nonce and [verified ID-token claims](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token), requesting send/openid/email scopes. Only supported verified Gmail/Google Workspace identities are accepted. The account directory is restricted before credentials are written. Runtime refresh uses a fixed endpoint, bounded request, interprocess lock and atomic credential replacement with permission/account checks around blocking operations. Files only establish `configured`; they do not prove a current provider connection or successful delivery.

The fixed verified sender is named both at review and confirmation. Changing/restarting the account connection requires a new review; approval is not reused by the voice handler. The Rust journal currently binds the recipient/content and principal, while trusting the configured worker for sender evidence. A persistent account identity binding is required before multi-account workers or background sends can use durable approvals independently.

## WhatsApp Business choice

The user selected WhatsApp Business API sending, not personal-app click-to-chat. Phone contacts store an explicitly supplied international number; this does not establish WhatsApp availability, ownership, opt-in or permission to message. No personal contact store is imported automatically.

`connectors/whatsapp.py` contains the standalone `WhatsAppBusiness` text transport. Both the exact-approval gate and a trusted recipient consent/window-policy callback default to denial; they are checked again after token acquisition. It uses a fixed Graph v26.0 endpoint, one exact international recipient, at most 4,096 text characters and no automatic POST retry. A validated provider message ID means acceptance; delivery remains unverified. No runtime configuration or model tool exposes this adapter yet.

Production integration must bind a verified business sender, the selected contact and exact message/template content to the central approval. Free-form [service messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages) require recipient opt-in and an active 24-hour customer service window. A model assertion or manually guessed last-contact time must not establish that window. [Template messaging](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview) supports the intended first outbound workflow; approved template content and parameters must be reviewed exactly. No business account, token, webhook endpoint or approved template has been connected on this PC.

Protected account and template foundations are now implemented in `connectors/whatsapp_account.py` and `connectors/whatsapp_templates.py`. Explicit read-only verification checks that the selected phone belongs to the chosen WABA. The template catalog requires the exact approved ID/name/language and currently supports body-only positional text parameters. The standalone template sender rechecks the account, template, trusted recipient policy and exact approval before one POST; accepted still does not mean delivered. Account metadata verification does not establish send permissions or billing readiness. Meta can change template state after the last read, so there is no atomic provider-content guarantee.

The read-only `python -m tools.verify_whatsapp_business --help` CLI accepts an explicitly selected existing protected token file, WABA/phone IDs and optional template selection. Credentials remain outside the repository, synced folders and command-line arguments. This CLI does not create a sender, establish recipient consent, open a service window or send anything. No real account verification was attempted during implementation.

The implementation plan is [WhatsApp Business and reviewed checkout](../superpowers/plans/2026-11-20-whatsapp-business-checkout.md). A durable business-message journal, trusted consent records, voice sending approval and signed event processing still need integration. WhatsApp remains absent from the runtime model registry.

## Own Shopify store and reviewed checkout

The user selected their own Shopify store. The commerce path now connects the existing voice UI to an owner-scoped Rust journal, protected merchant configuration and the Shopify Storefront adapter pinned to `2026-10`. The model can search the configured store, select recently observed variant IDs/quantities and save a local proposal. Prices and titles come from the observed catalog; models cannot supply a checkout URL, create a cart, open a browser or pay. Catalog queries are external calls and are unavailable after private knowledge has tainted a model conversation.

Use these commands in a trusted private adult/elder session after configuring the exact store:

1. Ask EDITH to search the store and propose the selected items/quantities.
2. Say `review cart` (or `review cart <eight-character ID>` if several match).
3. Say `prepare reviewed cart`. Listen to the merchant, every item/quantity/unit price and catalog subtotal, then confirm. A fresh exact quote must still match before one cart-create attempt. `reject reviewed cart` rejects instead.
4. Say `review checkout`. EDITH reads the exact prepared cart again and announces its stored subtotal and estimated total.
5. Say `open reviewed checkout`, listen to the fresh full readback and confirm. EDITH opens the approved merchant checkout once. Verify delivery and final cost there and enter payment details yourself.

Quotes expire five minutes after the oldest selected catalog observation. Changed selection, price, store configuration, identity or session blocks the stale action. Changed cart estimates require a new proposal and review. Cart preparation and browser opening have separate durable claims; interrupted or uncertain effects become `unknown` and are never automatically retried. Opening is not proof of an order, payment or delivery. The private cart ID and checkout URL remain outside model summaries/readback; they are stored in protected local SQLite, not encrypted by the application. Browser history can retain the opened checkout. The current paired local credential is trusted for both journal and worker operations; separate worker credentials remain future work.

Setup requires the actual lowercase `.myshopify.com` domain and exact checkout host(s). In the protected hub configuration, an administrator adds a `commerce_merchants` entry and restarts the hub:

```json
{
  "principal_id": "owner",
  "id": "own-shop",
  "display_name": "Your store",
  "shop_domain": "your-store.myshopify.com",
  "checkout_hosts": ["your-store.myshopify.com"]
}
```

This is an entry in an array, not a replacement for the full hub configuration. Use actual store domains; no wildcards, guessed hosts or API credentials belong in that entry. The server derives a version from the entire member/store binding. A configuration change invalidates earlier approvals. The real store domain is still pending; no merchant entry was added to the actual household configuration.

Run `python -m tools.configure_household_merchant --help` for explicit local onboarding. It selects a currently allowlisted store for the paired adult/elder and creates a new dedicated protected directory. Choose tokenless Storefront access if supported by the store, or provide a protected file containing a **public Storefront access token**. Admin API tokens and private Storefront header credentials are not supported by this connector. Local setup reports configured, not live-connected. Runtime startup never enrolls a store or repairs credential permissions.

For a configured `own-shop` allowlist entry, explicit tokenless setup is:

```powershell
& 'D:\CodexTools\edith-python\Scripts\python.exe' -m tools.configure_household_merchant --hub-client-config 'D:\CodexTools\edith-household\owner-client.json' --merchant-id 'own-shop' --account-dir 'D:\CodexTools\edith-household\merchant' --tokenless
```

The hub must be running. The new account directory must not already exist. Replace `--tokenless` with `--storefront-token-file '<absolute protected token-file path>'` if the chosen store requires a public Storefront token. The selected token file contains plaintext, with an optional final newline, and its directory/file must already be private; setup does not silently repair them. Keep the token out of chat and shell arguments.

`EDITH_MERCHANT_CONFIG` points to the resulting absolute `merchant.json`. The owner launcher loads `D:/CodexTools/edith-household/merchant/merchant.json` when present; the shared launcher clears that setting. No actual store, checkout or payment was used in tests. Cross-language acceptance uses the real Rust binary and native protected local account storage, with synthetic Shopify HTTP, speech and browser effects. Authenticated order/receipt/refund reconciliation remains future work.

## Scoped coding assistance

`EDITH_WORKSPACE_CONFIG` points to a protected, absolute JSON file with exactly `root`, `staging_dir`, `principal_id` and `project_id`. The root and principal come from trusted configuration; a model cannot choose them. The configured adult/elder must match the authenticated hub principal. This development PC has an owner configuration for the EDITH repository at `D:\CodexTools\edith-household\workspace-config.json`; the launcher enables it only with `-OwnerSession`. Guest and child sessions never load this project configuration.

Tools read selected source lines with a whole-file hash, search bounded source text, and prepare a hash-bound replacement/diff for review. Source files are limited to 256 KB; scans/snapshots are bounded. Hidden/credential/data/dependency paths, traversal, links, junctions, reparse points and hard links are rejected. Staging is outside the repository and OneDrive with restricted permissions. Allowed source code can still contain embedded secrets; all such tool content stays in the local-model path.

The model has no patch-apply or execution tool. The trusted `Workspace.apply_patch` API requires the reviewed proposal hash and unchanged baseline; hashes bind content but do not establish human approval. Applications must invoke it only from an authenticated approval handler. A receipt-write failure after replacement reports an unknown outcome. An unrelated program can still race the final source check; this is not a filesystem sandbox.

`python -m tools.code_worker` reports execution availability. Docker and Windows Sandbox were not present during inspection; WSL's executable alone does not establish an isolated worker. The module can prepare a restricted digest-pinned Docker command for review but never launches it and has no host execution fallback. Runtime isolation, build/test execution and publishing remain unfinished.

## Home control and other remaining work

The user chose to set up Home Assistant later. Its connector is therefore disconnected. Code exists for exact allowlisted service calls and state readback, plus a Rust action journal with approval hashes, single execution claims and unknown-outcome recovery. Passing connector tests does not prove a physical device changed. A token-authenticated worker reports outcome evidence; the journal does not independently observe hardware.

Other remaining work includes trusted member interaction, multilingual/acoustic evaluation and lower conversational latency, broader RAG parsers/token budgets, verified isolated code execution and publishing, parent controls, recurring reminders/delegation, live account acceptance, order/payment reconciliation, current sourced recommendations and operational packaging. Existing desktop tools and standalone adapters are not proof those complete experiences exist.

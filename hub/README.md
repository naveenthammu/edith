# EDITH central hub

This Rust service adds persistent, permission-filtered document retrieval, durable typed action journals, one-time reminders and scoped contacts behind the existing EDITH interface. It does not provide a new UI, record audio or invoke a language model. The current implementation is a foundation for the household engine; it does not itself control appliances, speak reminders, send messages or process payments.

## Run

1. Install the Rust toolchain and a C compiler suitable for its target (bundled SQLite is compiled from source). Use the checked-in `Cargo.lock`.
2. Copy `config.example.json` outside the repository and set an absolute database path. Replace its deliberately invalid placeholder with a cryptographically random token of at least 32 characters. For example, Python's `secrets.token_urlsafe(32)` generates an appropriate value. Store it directly in the protected config and client configuration; do not put it in chat, logs or version control.
3. Set `EDITH_HUB_CONFIG` to the absolute configuration filename. The default listener is `127.0.0.1:8788`; only loopback IPs and nonzero ports are accepted. The example token is too short to start the service.
4. Run `cargo run --locked --manifest-path hub/Cargo.toml` from the repository root. Stop with Ctrl+C.

Keep the config, database, WAL/SHM files and backups in an OS directory accessible only to the intended account. This version uses ordinary SQLite, not application-level encrypted storage. No data is seeded. Existing data is migrated transactionally; unsupported future schema versions are rejected. Rotating/revoking a principal credential requires updating the protected config and restarting the hub.

The current Windows development toolchain uses an isolated Cargo/Rustup installation under `D:/CodexTools/edith-rust`, LLVM-MinGW `clang` to build SQLite, and the Rust GNU toolchain's bundled `x86_64-w64-mingw32-gcc.exe` as linker. This is a development setup, not a portable installed product.

## API

Send `Authorization: Bearer <principal token>` and JSON to `/v1/*`. Principals and roles are mapped from server configuration; requests cannot choose their identity. Credentials are compared as fixed-size SHA-256 digests using constant-time comparison. Credentials and document contents are not logged by this service. CORS is not enabled.

### WhatsApp Business template journal (schema 8)

This is a worker-attested metadata store and durable send journal, not a Meta connector in Rust. No account is created, no provider request is made, and no token is acquired automatically. Explicit server configuration grants a worker permission to verify one account on behalf of one enabled adult/elder. A paired/model client cannot acquire worker privileges by approving a proposal.

Optional protected config `whatsapp_workers` defaults to `[]`. Each entry is `{id,principal_id,connection_id,waba_id,phone_number_id,token,enabled:true}`. At most 64 workers; one worker per `(principal_id,connection_id)`. Worker IDs and 32–256 printable non-space ASCII credentials must be unique across both paired and worker credentials. Connection IDs use 1–64 ASCII letters/digits/underscore/hyphen. WABA and phone-number IDs contain 1–32 decimal digits without a leading zero. A worker must reference an adult/elder, and an enabled worker requires an enabled principal. Disable associated workers when disabling that principal. Exact account IDs are configuration authorization, not proof of provider verification. Config changes take effect on restart.

Worker credentials are accepted only by `/v1/whatsapp-worker/*`; paired credentials are rejected there. Worker credentials cannot access ordinary knowledge, contact, home, commerce or paired message APIs. Worker binding and message reads are scoped to the configured principal, connection, exact WABA and sender phone ID before applying list limits. Worker permission reads additionally require the current exact account revision, so retargeting a worker cannot expose old-sender permission evidence. Paired owners retain access to their historical records. Ordinary paired business endpoints require an adult/elder owner; children/guests receive 403 and other owners receive 404. All lists are capped at 100 rows. All new input structures reject unknown JSON fields.

| Endpoint | Contract |
|---|---|
| `GET /v1/whatsapp-worker/capabilities` | `{worker_id,principal_id,connection_id,waba_id,phone_number_id,capabilities:{binding_write:true,permission_write:true,message_claim:true,message_result:true}}` |
| `GET /v1/whatsapp-bindings` and worker `/bindings` | `{bindings:[...]}`; paired owner or worker connection scope |
| `GET /v1/whatsapp-bindings/{id}` and worker `/bindings/{id}` | One binding record |
| `POST /v1/whatsapp-worker/bindings` | `{account,template}` verified by the trusted Python worker; returns binding, 201 new or 200 refresh |
| `GET /v1/whatsapp-permissions` and worker `/permissions` | `{permissions:[...]}`; permissions are explicit operator records, never inferred from contact existence |
| `POST /v1/whatsapp-worker/permissions` | Explicit permission creation/replacement/revocation with nullable required `expected_version`; returns 201/200 record; stale/missing CAS revision 409 |
| `POST /v1/business-messages` | `{binding_id,binding_version,contact_id,contact_version,permission_version,parameters,idempotency_key}`; new 201, exact replay 200, changed replay 409 |
| `GET /v1/business-messages` and worker `/messages` | `{messages:[public_record,...]}` |
| `GET /v1/business-messages/{id}` and worker `/messages/{id}` | One public message record |
| `POST /v1/business-messages/{id}/decide` | Paired owner only; `{decision:"approve"\|"reject",argument_hash}`; exact hash and fresh approval, same historical choice idempotent |
| `POST /v1/whatsapp-worker/messages/{id}/claim` | `{argument_hash}`; one approved execution claim, bound to the claiming worker ID |
| `POST /v1/whatsapp-worker/messages/{id}/authorize` | `{argument_hash}`; returns `{authorized:true,message:public_record}` only for the exact still-authorized executing claim; denial is 409 and makes no state change |
| `POST /v1/whatsapp-worker/messages/{id}/result` | `{status:"accepted"\|"failed"\|"unknown",evidence}`; immutable historical receipt from the claiming worker only |

An account snapshot has exactly `connection_id,principal_id,waba_id,phone_number_id,display_phone_number,verified_name,version,verified_at`. It must match the worker's configured account and principal. `version` is an opaque lowercase SHA256 revision from Python, binding account metadata and credential revision; Rust does not know the Meta token. Display number is exact syntactic E.164; verified name is nonblank, at most 256 Unicode characters without controls. On synchronization, `verified_at` must be less than 300 seconds old and at most 30 seconds in the future. Older account verification cannot replace a newer one; changed metadata at the same timestamp requires a later verification timestamp. Rust records the current account for the connection, so updating one template to a different account version invalidates old-account bindings for other templates as well.

A template snapshot has exactly `id,name,language,status,category,parameter_format,body,content_hash`. Supported templates have numeric IDs, lowercase/digit/underscore names up to 512 characters, language such as `en` or `en_US`, status `APPROVED`, category `UTILITY` or `MARKETING`, format `POSITIONAL`, and one text BODY up to 1,024 Unicode characters. The Python verifier excludes other provider components before synchronization. Rust recomputes `content_hash` over the compact sorted-key UTF-8 JSON of the other seven fields. It validates contiguous positional slots `{{1}}` through at most `{{10}}`. Parameters are exact nonblank plain-text values of at most 1,024 characters each, with no controls or nested `{{...}}`; rendered text is at most 4,096 characters, permitting CR/LF only as body formatting. The server renders it itself.

Binding record: `{id,version,account,template,updated_at}`. ID is a stable UUID per owner/connection/template ID/language. Version is SHA256 over canonical `{account:account_without_verified_at,template}`. Refreshing unchanged metadata preserves version and updates verification time. The full binding and safe sender metadata are owner-readable, but credentials never enter this store.

Permission input: `{connection_id,account_version,contact_id,contact_version,expected_version:null|string,status:"granted"|"revoked",allowed_categories:["UTILITY"|"MARKETING"],recorded_at,expires_at,source:"explicit_operator_record",evidence_reference,source_note?}`. The contact must currently belong to the owner and have an exact phone number. The account/contact revisions must be current. The first write requires explicit null `expected_version`; every replacement/revocation requires the current version and generates a new opaque revision even if content is otherwise unchanged. Categories must be unique; grants require at least one category and `now < expires_at <= now+366 days`. Revocation may use an empty category list and a nonnegative past expiry. `recorded_at` describes this administrative event, must be no more than 300 seconds old or 30 seconds in the future, and is not a claim that the recipient personally authenticated at that time. `evidence_reference` is required nonblank text up to 200 characters; optional `source_note` is at most 400 characters, default empty. Both reject control characters. Record the actual opt-in evidence reference/date in these fields, never credentials. Operator attestation remains trusted input rather than independently verified recipient consent.

Permission record: `{id,version,connection_id,account_version,contact_id,contact_version,recipient_e164,status,allowed_categories,recorded_at,expires_at,source,evidence_reference,source_note,updated_at}`. IDs are UUIDs; category order is `UTILITY`, then `MARKETING` when both are present. Version is an opaque lowercase SHA256 revision with a fresh random nonce for every CAS write. Prior permission versions never become valid again after revoke/regrant. Missing permissions, wrong template categories, changed contacts/phone numbers, stale versions, expired grants and revocations all prevent approval, claim and authorization.

Public message record has exactly `id,kind,payload,argument_hash,created_at,expires_at,status,evidence,recovery_note`. Kind is `contact.whatsapp_template`. Payload is exactly `{binding_id,binding_version,contact_id,contact_version,recipient_name,recipient_e164,permission_version,prepared:{account,template,parameters,rendered_text}}`. The server resolves sender, recipient and content from trusted records; proposal callers cannot override these fields. Hash is lowercase SHA256 of compact sorted-key UTF-8 JSON `{kind,payload}`. Message ID is a UUID; all binding/contact/permission/hash revisions are 64 lowercase hex characters. The original prepared account timestamp stays immutable. Refreshed binding comparisons ignore that timestamp while enforcing all content/revision matches.

A cached binding may create a local proposal if its current configured account and permission remain valid. Proposal expiry is always `created_at+300`, and exact replay never refreshes it. Before fresh approval, claim and pre-send authorization, the current binding must have provider verification younger than 300 seconds. These transitions also atomically recheck current enabled worker configuration, exact account, unchanged contact/phone/name, binding content/version and permission version/status/category/expiry. Claim requires `approved`, changes it to `executing`, records the worker ID privately, and never grants execution twice. The Python worker must refresh the provider snapshot and use `/authorize` again after blocking provider/credential work and immediately before the send POST. The authorization response is a point-in-time check; it is not an atomic transaction with Meta or a proof of human identity.

The paired owner may reject an approved message until execution is claimed, using its exact argument hash. Rejection and claim are atomic competing transitions: only one succeeds. Repeating rejection is idempotent; a rejected message cannot be approved again. Executing and terminal messages cannot be cancelled through the decision endpoint. Rejecting an approved but unclaimed message remains permitted after expiry or configuration revocation because it removes execution authority.

Receipt evidence has exactly `{provider:"whatsapp_business",phone_number_id,waba_id,account_version,template_content_hash,recipient_e164,provider_message_id,delivery_status:"unverified"}`. Sender/template/recipient fields must match the immutable proposal. Accepted results require a nonempty printable non-space ASCII provider ID up to 512 bytes; failed results require null; unknown may carry a validated ID. Only the same claiming worker may record a result. A historical receipt may be recorded after proposal/permission expiry or revocation because the provider attempt has already happened. First result is immutable except exact replay. Startup converts interrupted `executing` rows to `unknown` with null evidence and a fixed recovery note; it never reclaims or automatically resends. Provider acceptance is not delivery; webhook delivery state and free-form service-window messages are outside this slice.

Paired capabilities expose `business_message_proposals` for an adult/elder with an enabled configured worker, `whatsapp_business_journal:true`, and `whatsapp_business_connected:false`. Configuration does not imply a live verified account, valid recipient permission, or successful send. Existing schema 7 APIs remain compatible.

### Merchant carts and manual checkout (schema 7)

Optional protected config `commerce_merchants` defaults to `[]`. Each entry is `{principal_id,id,display_name,shop_domain,checkout_hosts}`. The principal must be a configured adult/elder; IDs are unique per principal; at most 128 entries. Use one exact lowercase `shop.myshopify.com` domain and 1–10 unique exact lowercase DNS checkout hosts, without schemes, wildcard, ports or IP addresses. The server provides no merchant-edit HTTP route. Changing config requires a restart and revokes pending approvals through a version check. The stable `version` is lowercase SHA256 of compact sorted-key UTF-8 JSON of this complete config entry, with `checkout_hosts` sorted. Array ordering outside an entry does not change its version.

All commerce endpoints require the record's adult/elder owner. Other owners receive 404; child/guest roles receive 403. Lists contain at most 100 recent records. Unknown JSON fields are rejected. Configured merchant identity and checkout origins come from the server, never arbitrary model URLs.

| Endpoint | Contract |
|---|---|
| `GET /v1/merchants` | `{merchants:[{id,display_name,shop_domain,checkout_hosts,version}]}` for this principal |
| `POST /v1/commerce` | `{merchant_id,merchant_version,lines,currency,observed_at,idempotency_key}`. New 201; exact replay 200; key reused with different arguments 409 |
| `GET /v1/commerce` | `{commerce:[public_record,...]}` |
| `GET /v1/commerce/{id}` | One public record |
| `POST /v1/commerce/{id}/decide` | `{decision:"approve"\|"reject",argument_hash}`; exact arguments and fresh approval; same historical decision idempotent |
| `POST /v1/commerce/{id}/claim` | `{}`; approved, current merchant revision, unexpired; one winner transitions to `executing`, subsequent claims 409 |
| `POST /v1/commerce/{id}/result` | `{status:"prepared"\|"failed"\|"unknown",evidence,checkout}`; only after claim; immutable except exact replay |
| `GET /v1/commerce/{id}/checkout` | Trusted coordinator only: `{action:public_record,checkout:{cart_id,checkout_url}}`; requires prepared, current merchant revision and unexpired original quote |
| `POST /v1/commerce/{id}/checkout/claim` | `{argument_hash}`; one opening claim; same private wrapper, `checkout_status:"executing"`; repeated claim 409 |
| `POST /v1/commerce/{id}/checkout/result` | `{status:"opened"\|"failed"\|"unknown",evidence:{payment_status:"not_submitted"}}`; immutable except exact replay; returns public record |

Every line is exactly `{variant_id,title,quantity,unit_price_minor}`. Use Shopify ProductVariant GIDs with 1–20 decimal digits (no leading zero), title 1–300 Unicode characters without controls, 1–10 units per distinct variant, 1–10 lines, at most 50 total units. Prices are integer minor units, 0–99,999,999,999,999. Currency is `INR`, `USD`, `EUR` or `GBP`. `observed_at` is UTC epoch seconds, less than 300 seconds old and no more than 30 seconds in the future. `expires_at=min(created_at+300,observed_at+300)`; exact replay never refreshes it. Approval, cart claim, private checkout read and opening claim all require `now < expires_at`. Result recording remains possible after expiry because the external attempt has already occurred.

Public record keys are exactly `id,kind,payload,argument_hash,created_at,expires_at,status,evidence,recovery_note,checkout_status,checkout_evidence`. `kind` is `commerce.prepare_cart`. Payload keys are `merchant_id,merchant_version,merchant_name,shop_domain,lines,currency,observed_at,quoted_subtotal_minor`. Name and domain are bound from trusted config; subtotal is computed from all approved unit prices and quantities. Line order is preserved. `argument_hash` is SHA256 of compact sorted-key UTF-8 JSON `{kind,payload}`. Initial status is `awaiting_approval`, and initial checkout status is `not_opened`. Evidence and recovery note initially are null.

Cart evidence is exactly `{provider:"shopify",payment_status:"not_submitted",currency,subtotal_minor,total_minor,lines}`. A prepared result must contain the exact approved lines, currency and subtotal. `total_minor` is an estimate between zero and 4,999,999,999,999,950, may include tax/shipping, and must be reviewed separately before opening checkout. A prepared result includes private `checkout:{cart_id,checkout_url}`. Failed/unknown results require null currency/subtotal/total, empty lines, and null checkout. These outcomes cannot provide an openable cart.

Checkout URLs are bounded ASCII HTTPS URLs (at most 2048 bytes), exact allowlisted host with optional `:443`, Shopify `/cart/c/<segments>`, `/checkouts/<segments>` or `/<digits>/checkouts/<segments>` paths. No userinfo, fragment, redirect parameters, escaped controls or backslashes are accepted. The only permitted query is optional `key=<nonempty ASCII token>`; this deliberately narrow initial contract can reject additional provider URL forms. Cart IDs use `gid://shopify/Cart/<token>` with optional `?key=<token>`, also at most 2048 bytes. These private values are stored only within protected SQLite and never serialized into normal lists, detail, approval or result responses. The checkout endpoint is not a distinct identity boundary: clients holding the same bearer token can read it, so Python must keep it out of model tools, logs and ordinary UI text.

Cart and opening claims are separate durable one-shot transitions. Startup converts interrupted cart execution to `unknown` and interrupted opening to checkout `unknown`; no automatic retry or reopen is allowed. Fixed recovery notes report uncertainty. Neither a prepared cart nor an opened checkout means an order, delivery or payment was submitted. Rust performs no merchant request or browser operation. Provider authenticity, fresh quote/cart rechecks and fresh interactive confirmation are the trusted Python coordinator's responsibilities; this API does not attest a provider response or distinguish the LLM from its token-holding client.

Capabilities expose `commerce_journal:true`, role/allowlist-based `commerce_proposals`, `commerce:"not_configured"` and `merchant_checkout_connected:false`. These flags do not claim a live merchant connection. Schema 6 contacts, reminders, messages, home actions and knowledge remain compatible.

| Endpoint | Contract |
|---|---|
| `GET /health` | Public service/version/readiness; no private state |
| `GET /v1/capabilities` | Principal ID, role and actual capability states |
| `POST /v1/documents` | Adult/elder only. `{title,content,visibility:"private"\|"household",child_allowed:false}`; returns 201 plus document metadata |
| `GET /v1/documents` | `{documents:[...]}` containing permitted metadata, at most 1,000 entries |
| `GET /v1/documents/{id}/chunks` | Owner adult/elder only. `{document:{metadata},chunks:[{chunk_id,content,source_version}]}` for local indexing |
| `PUT /v1/documents/{id}` | Owner adult/elder only. Full replacement `{expected_source_version,title,content,visibility,child_allowed}`; stale revision returns 409; successful update clears embeddings |
| `DELETE /v1/documents/{id}` | Owner adult/elder only; removes metadata and retrieval index atomically. Missing or nonowned IDs return 404 |
| `POST /v1/documents/{id}/embeddings` | Owner adult/elder only. Atomic complete-chunk index replacement; contract below |
| `POST /v1/search` | `{query,limit:5,query_vector?:{model_id,vector},min_similarity?:0.2}`; returns `{citations:[{document_id,chunk_id,title,excerpt,score,source_version}],retrieval_mode,semantic_available}` |
| `POST /v1/sources/validate` | `{sources:[{document_id,source_version}]}` (at most 100); returns corresponding `{sources:[{document_id,source_version,valid}]}` without titles or content |
| `POST /v1/actions` | Propose allowlisted `home.power`; 201 new, 200 exact idempotency replay, 409 changed arguments for a previously used key |
| `GET /v1/actions` | `{actions:[...]}` scoped to the adult/elder principal; at most 1,000 entries |
| `POST /v1/actions/{id}/decide` | `{decision:"approve"\|"reject",argument_hash}`; exact owner/hash and fresh pending action required |
| `POST /v1/actions/{id}/claim` | No body. Atomically consumes a fresh approval, returning `executing` to exactly one claimant; duplicate claim returns 409 |
| `POST /v1/actions/{id}/result` | Typed worker evidence after claim; exact repeat is idempotent, changed terminal result returns 409 |

An adult/elder reads their own private sources and household sources. A child/guest reads only household sources explicitly marked `child_allowed:true`. Children/guests cannot ingest or delete documents. Sharing with adults does not imply child suitability. A configured token authenticates a client installation, not whoever happens to speak near its microphone; the Python runtime must keep a least-privilege principal for shared listening and use a trusted identity interaction before switching to an adult account.

`source_version` is an opaque SHA-256 revision of canonical, length-delimited title/content/visibility/child-sharing fields. Clients return the server's revision instead of recomputing it. Documents created by the initial content-only version retain their old revision until replaced. Metadata-only changes advance the revision as well, so stale writers cannot overwrite a changed sharing decision and cached citations cannot silently retain old titles. Updates preserve the document ID, atomically replace its chunks and clear every stored embedding model for that document.

Titles and overlapping 1,600-character chunks are indexed with SQLite FTS5. Query words are quoted as data; permission filtering occurs in SQL before text reaches relevance scoring or citation output. Lexical ranking uses only permitted candidate text, not corpus-wide FTS statistics. Search considers at most 32 query words and streams every permitted matching chunk, retaining the strongest candidates in memory. A relevant late chunk is not discarded by an arbitrary SQL candidate cutoff. Clients send extracted UTF-8 text, with a 2,000,000-byte document limit, 200-character title limit and 2,048-byte query limit. Document/PDF parsing is performed by a separately scoped client, not by the Rust service.

## Semantic indexing and hybrid retrieval

The local indexing worker reads an owned document's versioned chunks, generates vectors with a pinned embedding engine/model digest, and uploads:

```json
{"source_version":"<server revision>","model_id":"ollama:model-name@sha256:<installed-digest>","embeddings":[{"chunk_id":"<document-id>:0","vector":["<16 to 4096 finite numbers>"]}]}
```

The placeholder vector above illustrates the shape; the actual vector must contain numbers. Upload every current chunk exactly once. A transaction replaces that document/model index only after checking ownership, source revision, unique chunk IDs, completeness, finite nonzero vectors and consistent dimensions. The opaque model ID is permanently bound to its dimension; changing engines/digests requires a different ID. Vectors are persisted as JSON `f32` values with their dimension, magnitude and source revision. The upload route permits at most 32 MiB of JSON; other routes keep their smaller request limit. A 413 response leaves the existing index intact. Very large/high-dimensional indexes exceeding this ceiling need smaller selected source documents; multipart indexing is not implemented.

With a query vector, the server selects only currently authorized chunks for the exact model ID and source revision **before computing cosine distances**. It independently ranks the complete permitted lexical and semantic candidate sets, keeps the top 100 from each, then applies reciprocal-rank fusion with `k=60`. It returns at most ten citations. The default minimum cosine similarity is 0.2; callers may choose a finite threshold between -1 and 1. Hybrid citation scores are floating-point fused rank scores; lexical scores are term-overlap scores. Scores from the two modes are not directly comparable.

`retrieval_mode:"hybrid"` and `semantic_available:true` mean the request used a permitted index matching its query model, even if no vector passed the similarity threshold. Without a query vector or a matching permitted index, the server honestly falls back to `retrieval_mode:"lexical"` and `semantic_available:false`. A model indexed solely in another person's private documents does not appear available. The capability endpoint advertises semantic API support, not that a model has been installed or any document indexed. No embedding model is downloaded or invoked by Rust.

Both ranking channels are exact scans of permitted matching data; query time grows with corpus size and vector dimension. This is suitable for an initial household corpus, not a measured large-scale approximate-nearest-neighbor service. Source quality, engine quality and answer grounding still require separate evaluation.

Clients must revalidate retained citation IDs/revisions before using them in future model context, and before delivering an answer after a long model call. `/v1/sources/validate` evaluates current access and current version; private, revoked, changed, deleted and nonexistent sources all return `valid:false` without disclosing metadata. This endpoint does not retract content already delivered to a user or model, and it cannot eliminate a later permission change after a response has left the server.

Deletion removes retrieval access immediately; it is not a promise of forensic erasure from SQLite free pages, filesystem snapshots or backups. Avoid retaining citations in another client/model cache without corresponding deletion and permission rules.

## Action journal

Configure `home_entities` with exact IDs such as `light.study`, `switch.table` or `fan.bedroom`. It defaults to empty, allowing no home actions. Wildcards and other domains are rejected. Allowlisting power control does not attest that an attached appliance is safe; select actual devices deliberately. Removed entities cannot receive a new approval or execution claim after configuration is reloaded by restart.

An adult/elder client proposes:

```json
{"kind":"home.power","payload":{"entity_id":"light.study","on":true},"idempotency_key":"client-generated-unique-key"}
```

All fields are strict. The server calculates an opaque canonical `argument_hash`, readable summary and five-minute expiry. Reusing an idempotency key with identical arguments returns the existing action; different arguments conflict. Keys are scoped by principal. The client must show/speak the exact action and obtain the appropriate trusted approval before posting the returned hash to `/decide`. Saying a generic yes is not authentication, and this API does not authenticate a voice speaker.

The sequence is `awaiting_approval → approved → executing → verified / failed / unknown`, or `awaiting_approval → rejected`. Approval is not execution. The connector must obtain a successful, first `/claim` response **before** any device call. A lost claim response is an uncertain outcome; do not execute or claim again. The first recorded result is immutable; only an exact retry succeeds. Pending or approved actions cannot be claimed after expiry. Repeating a historical same-choice approval returns the existing state and does not extend expiry.

Worker result example:

```json
{"status":"verified","evidence":{"entity_id":"light.study","requested_state":"on","observed_state":"on","command_status":"accepted"}}
```

`status` is `verified`, `failed` or `unknown`; requested state is `on`/`off`; observed state additionally allows `unknown`/`unavailable`; command status is `accepted`/`rejected`/`unknown`. Entity and requested state must match the prepared payload. A verified result requires the requested observed state and an accepted or unknown command status. These are distinct claims: observed matching device state is not proof that a particular command caused the state. This service validates and records the connector's report, not an independent hardware attestation. Arbitrary provider output, secrets and extra evidence fields are rejected.

On startup, any `executing` action becomes `unknown` with an interruption note; it is never automatically re-executed. Reconciliation of interrupted actions remains a separate client/operator workflow. The production executable binds its listener and acquires an OS-held exclusive lock for the canonical database path **before** opening or recovering the journals. A second instance—even on a different port—cannot recover a live instance's actions. The lock is released when its process exits or crashes. Its empty `.hub.lock` sidecar deliberately remains; file existence is not treated as proof of a live server, and it must not be deleted while the server is running. The in-process `app(config)` test/library constructor assumes its caller controls database ownership; use the executable for production. This version has no distributed leader election, payment actions or complete household action authority.

## Personal reminders, contacts and email journal

All endpoints in this section require the owning adult/elder principal. Children/guests receive 403, and another owner's record ID receives 404. No household sharing applies. Collection responses contain at most 1,000 records, ordered newest creation first, then ID. Pagination is not implemented; collection responses can be large, especially historical message bodies. Capabilities expose role-gated `reminders`, `contacts` and `email_proposals`; `gmail_connected:false` is intentional because the Rust service has no provider credentials or provider connection. Schema version 5 added these records without seeding data; version 6 adds optional phone numbers. Legacy email contacts retain their IDs and revisions, with `phone_e164:null`. The separate schema 8 WhatsApp Business journal is documented above; its presence does not imply a connected provider.

### One-time reminders

`POST /v1/reminders` accepts `{text,due_at,idempotency_key}`. Text is nonempty, at most 400 Unicode characters, without control characters. `due_at` is an integer UTC Unix timestamp, no more than 60 seconds in the past or 366 days in the future. A new reminder returns 201; exact idempotency replay returns 200, including after its original due time, while changed arguments return 409. Idempotency keys are principal-scoped, 1–128 ASCII letters/digits/underscore/hyphen.

Ordinary reminder responses contain `{id,text,due_at,created_at,updated_at,status,announced_at,acknowledged_at,cancelled_at}`; unset timestamps are null. `GET /v1/reminders` returns `{reminders:[...]}`. `POST /v1/reminders/{id}/cancel` or `/acknowledge`, with `{}`, changes the record and invalidates every active lease. Repeating the same terminal operation succeeds; a different terminal choice conflicts. Acknowledgement is an explicit client/user report, not inferred from speech output.

`POST /v1/reminders/claim-due`, with `{}`, atomically leases one earliest-due scheduled record for 120 seconds. It returns `{reminder:null}` or `{reminder:{...ordinary_fields,delivery_id,lease_expires_at}}`. No other worker may claim that record while its lease remains valid. Uncompleted leases survive restart and become reclaimable after expiration. The worker posts `{delivery_id,status:"announced"|"retry"}` to `/v1/reminders/{id}/delivery`; this requires the current, unexpired lease. `announced` records the connector's completed announcement and stops automatic claims, while `retry` releases the lease and leaves the reminder scheduled. Exact repetitions of the latest completed delivery receipt succeed; changed, superseded, cancelled or expired delivery receipts conflict. Delivery IDs remain worker protocol data and are excluded from ordinary lists.

This is **at-least-once announcement**, not proof of hearing, understanding, taking medication or completing an action. A crash after speech but before recording completion may cause a duplicate announcement. Rust does not schedule a process, monitor audio, manage recurring reminders or interpret local-language dates; the client must convert an explicitly understood date/time to UTC and operate its delivery worker.

### Contacts and immutable email proposals

`POST /v1/contacts` accepts `{display_name,email?,phone_e164?}` and returns 201 with `{id,display_name,email,phone_e164,version,created_at,updated_at}`. Omitted channel fields default to null, and at least one must be supplied. Both supplied channels must validate. Old email-only requests remain valid. Names are nonempty, at most 100 Unicode characters, without control characters. Email addresses must be one bounded plain ASCII address (254 bytes maximum), without display-name syntax, spaces or header injection. Phone numbers must exactly match the syntactic E.164 shape `+[1-9][0-9]{1,14}`: no spaces, national-format guessing, Unicode digits or implicit country code. These checks do not establish existence, ownership, WhatsApp registration or messaging consent.

`GET /v1/contacts` returns `{contacts:[...]}`. `PUT /v1/contacts/{id}` accepts `{expected_version,display_name,email?,phone_e164?}` as a full replacement; omitted channels are cleared, and stale versions conflict. Each update issues a new opaque SHA-256 revision, including a phone-only change or an update restoring older values. Existing email approvals require the current full contact revision, so phone edits conservatively invalidate them too. `DELETE /v1/contacts/{id}` returns `{deleted:true}`. Clients must select an exact contact ID and revision; the server never silently chooses a fuzzy name match. Phone-only contacts cannot create email proposals; such a request returns 400 and creates no message.

`POST /v1/messages` accepts `{contact_id,contact_version,subject,body,idempotency_key}`. Subject is nonempty, at most 200 Unicode characters and no control characters; body is nonempty and at most 4,000 Unicode characters, permitting tabs and line breaks. The server resolves the current owned contact and binds its email address into an immutable payload:

```json
{"kind":"contact.email","payload":{"contact_id":"<id>","contact_version":"<revision>","recipient":"friend@example.test","subject":"Hello","body":"Message text"}}
```

The lowercase `argument_hash` is SHA-256 over this sorted-key, compact UTF-8 JSON object. Proposal responses contain `{id,kind,payload,argument_hash,created_at,expires_at,status,evidence,recovery_note}`. New proposals return 201, exact key replays 200 and changed arguments 409. `GET /v1/messages` returns `{messages:[...]}`; `GET /v1/messages/{id}` retrieves an exact owned record. A proposal expires after five minutes.

`POST /v1/messages/{id}/decide` accepts `{decision:"approve"|"reject",argument_hash}`. `POST /v1/messages/{id}/claim` accepts `{}` and atomically transitions a fresh approved proposal to `executing` once; any duplicate claim conflicts. Approval and claim both require the same current owned contact revision and recipient. Changing/deleting a contact invalidates future execution authority without erasing historical proposals or receipts. Exact historical decision replays return the recorded state but cannot extend expiry or restore a claim.

After a successful first claim, a trusted provider connector may report to `/v1/messages/{id}/result`:

```json
{"status":"accepted","evidence":{"provider":"gmail","recipient":"friend@example.test","provider_message_id":"provider-assigned-id","delivery_status":"unverified"}}
```

Status is `accepted`, `failed` or `unknown`. Evidence has exactly these fields; provider is `gmail`, recipient must exactly match the immutable payload, delivery status must be `unverified`, and provider message ID is null or 1–256 printable non-space ASCII characters. `accepted` requires a nonnull provider ID; `failed` requires null; `unknown` permits either. Accepted means **the provider accepted the submission**, not delivered, read or acted upon. Results are immutable except for exact retries. On startup, any executing message becomes unknown with an interruption note and is never automatically resent. A lost claim/send response must be reconciled; the server cannot prove whether an external send happened. The service contains no Gmail/network code and does not validate a connector report against the provider independently.

## Verification

Run `cargo test --locked --manifest-path hub/Cargo.toml` and `cargo build --locked --manifest-path hub/Cargo.toml`. Integration tests use temporary SQLite files and synthetic principals/documents. They cover authentication, role/owner isolation, explicit child sharing, malformed and oversized input, Unicode/FTS quoting, restart, deletion, title/late-chunk retrieval, bounds, schema migration, unsafe configuration, exact action approval, expiry, idempotency, concurrent claims, immutable evidence and interrupted execution recovery. Semantic tests use hand-constructed vectors to prove cosine/fusion behavior, model separation, atomic indexing, dimension and size limits, metadata revisions, private-scope filtering and citation invalidation. Personal-record tests cover contact revisions, invalid addresses/header injection, recipient binding, terminal results, reminder lease races, stale delivery receipts and restart. Internal storage tests inject timestamps to exercise exact expiry boundaries without sleeping. Real-binary tests exercise process ownership and recovery. Tests do not access live household data, run a real embedding model or call external services.

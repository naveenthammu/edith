# EDITH household prototype: implementation contract

> **Status — paused, unfinished (20 November 2026).** This is a proposed contract for the separate local `prototype/` work, not the API contract of the newly pulled Python application. The Rust hub currently exposes an empty router; an integrated working prototype has not been demonstrated. The requirements below remain targets. Use the [updated blueprint](household-agent-blueprint.md) and [current code assessment](updated-code-assessment.md) for the latest implementation status. This review does not resume implementation.

Date: 20 November 2026. User requested the broader household/coding/children/elder/shopping/communication blueprint **plus a working first prototype**. This is a local development prototype, not the complete production assistant. The full blueprint must retain every requested capability.

## Decisions

- Rust Axum/Tokio hub in `prototype/hub`; React/TypeScript/Vite client in `prototype/web`; Windows voice scripts in `prototype/voice`.
- Hub binds only `127.0.0.1:8787`; built client served by hub, Vite development proxy `/api` to hub. Do not expose LAN or open firewall ports.
- Pure-Rust dependencies preferred: isolated GNU Rust and LLVM-MinGW tooling was installed on D: during the paused work; a successful integrated hub build is not established. Avoid introducing more native dependencies for this slice. Proposed persistence uses bounded JSON state in `EDITH_DATA_DIR`, with development data on D:. This is a prototype storage adapter, not the production PostgreSQL design.
- Local Ollama on `127.0.0.1:11434`, model `qwen3:4b-instruct`; optional until installed. No external model calls, keys, or automatic cloud fallback. Inference cannot directly execute tools.
- Real local text-document retrieval with citations and local model-grounded answers; lexical retrieval is explicitly identified. Production hybrid vector retrieval is in the blueprint, not falsely claimed here.
- Adult, elder, child and guest **demo profiles**, not authenticated household identities. Profile switching is intentionally available for testing; no production privacy/authentication claim. Elder has adult capabilities with optional large controls/slower speech; age does not remove autonomy.
- Home devices, contacts, cart totals and purchase/message completion are **demonstration fixtures only**. State transitions must work locally in the eventual demo; this has not been demonstrated. The demo must visibly state that no external purchase, payment, message, call or physical home change is performed.
- Developer reads only this EDITH repository through allowlisted paths; cannot run terminal commands or apply model-generated code. It can inspect real source and request a local model explanation. Full coding sandbox/patch/test workflow remains in blueprint.
- Children get curated quizzes/stories and approved household lookup. No unrestricted local-model chat in child mode, no commerce/contact/developer access.

## Proposed HTTP contract

JSON snake_case throughout. Error: `{ "error": "Human readable reason" }` with appropriate4xx/5xx. `GET /api/bootstrap` returns session_token; client keeps it in sessionStorage and sends `X-Edith-Session` on API requests. A repeat bootstrap may use its existing token. Session is server-side. All API routes validate Origin/Host against local service/development addresses; do not enable wildcard CORS. Mutations and private reads require token. This prevents accidental cross-origin use, not malicious local users; do not call it identity authentication.

Types:

```ts
type Profile = 'adult' | 'elder' | 'child' | 'guest';
type Device = { id:string; name:string; room:string; on:boolean; provider:'demo' };
type DocumentInfo = { id:string; title:string; scope:'household'|'adult'; created_at:string };
type Citation = { document_id:string; title:string; excerpt:string; score:number };
type PendingAction = { id:string; kind:'purchase'|'message'; title:string; details:Record<string,unknown>; status:'awaiting_approval'|'approved_preview'|'rejected'; created_at:string; expires_at:string; demo:true };
type Activity = { id:string; timestamp:string; kind:string; summary:string };
type ModelStatus = { available:boolean; model:string; detail:string };
type AppState = { profile:Profile; devices:Device[]; documents:DocumentInfo[]; actions:PendingAction[]; activity:Activity[]; model:ModelStatus };
```

Endpoints:

| Method/path | Request | Response |
|---|---|---|
| GET `/api/health` | none | `{ok:true,version:string}` |
| GET `/api/bootstrap` | optionalexistingtoken | `{session_token:string,state:AppState}` |
| GET `/api/state` | token | `AppState` (scope-filtered before serialization) |
| POST `/api/profile` | `{profile:Profile}` | `AppState` |
| POST `/api/chat` | `{message:string}` | `{reply:string,mode:'local-tool'|'retrieval'|'model'|'curated',citations:Citation[],navigate?:string}` |
| POST `/api/devices/:id/toggle` | `{on:boolean}` | `Device` |
| POST `/api/documents` | `{title:string,content:string,scope:'household'|'adult',share_confirmed?:boolean}` | `DocumentInfo` |
| DELETE `/api/documents/:id` | none | `{deleted:true}` |
| POST `/api/search` | `{query:string}` | `{citations:Citation[]}` |
| POST `/api/actions` | `{kind:'purchase'|'message',details:Record<string,unknown>}` | `PendingAction` |
| POST `/api/actions/:id/decide` | `{decision:'approve'|'reject'}` | `PendingAction` |
| GET `/api/play` | none | `{questions:[{id:string,prompt:string,choices:string[]}]}` (no answers) |
| POST `/api/play/answer` | `{question_id:string,answer:string}` | `{correct:boolean,explanation:string}` |
| GET `/api/developer/files` | none | `{files:[{path:string,bytes:number}]}` |
| GET `/api/developer/file?path=...` | relativepath | `{path:string,content:string}` |
| POST `/api/developer/explain` | `{path:string,question:string}` | `{reply:string,mode:'model'|'local-tool'}` |
| GET `/api/voice/capabilities` | token | `{available:boolean,engine:string,languages:string[],detail:string}` |
| POST `/api/voice/transcribe` | raw `audio/wav` bytes | `{text:string,confidence:number,engine:string}` |

Voice endpoints wired by root; backend agent should expose route extension hook or reserve routes, not implement scripts. Root provides a `voice` Rust module. Browser uses getUserMedia only on Talk click; converts PCM to 16kHz mono16bit RIFF WAV; records at most12seconds, permits stop/cancel, releases mic tracks. Never use browser cloud SpeechRecognition as an undisclosed fallback. Read aloud uses browser speechSynthesis only with a localService voice, explicit button/toggle, stop control, elder rate0.8. If no local voice exists, disable read-aloud with an explanation; never silently select a remote/browser-default voice. Synthetic WAV tests are allowed; no microphone capture by the agent without a deliberate UI request.

## Content/state and routing

- Seed three demo devices: `study-light` Off, `living-light` On, `bedroom-fan` Off. Child may toggle `study-light` only; guest no devices. Adults/elders all three.
- Seed 3 clearly labelled synthetic household notes (router restart instructions without passwords, recycling day Thursday, plant watering Sunday) and one adult-only synthetic budget note. Actual user-added notes must remain local and deletable.
- Maximum document content50,000chars, title120chars,100documents; chat4,000chars; WAV at most512KB. Bound loaded data/response sizes.
- Role restrictions checked server-side before retrieval, document listing, model prompt, action read/approval or source access. Adult/elder may create/delete notes. Child/guest only household sources. New notes default to adult scope; household scope is visibly described as shared with all demo profiles, including children and guests, and requires share_confirmed:true on ingestion. Adult scope is a demo policy boundary, not authenticated privacy. Conversations do not mix after a profile change: client clears visible conversation, cancels/ignores pending chat, transcription, search and developer responses, and stops speech. Backend rechecks authorization after long model work before returning results; deleted/revoked citations are not returned.
- Route recognized safe demo home commands deterministically. Navigation prompts such as start game / shopping list return navigate. Questions about household notes use scoped retrieval. General adult/elder chat may call local model, never tools from model output. Child uses curated responses. Guest does not call model or inspect adult sources.
- Purchase details fixed schema: `{items:[{name:string,quantity:number,unit_price:number}],merchant:string}`; INR totals computed by server, not trusted from client; bounded positive quantities/prices and total. Message details `{recipient:string,text:string}` with recipient limited to synthetic `Alex (demo)` or `Sam (demo)`; no real addresses/numbers. Enforce same-profile/session ownership for actions, immutable details,5minuteexpiry, authorization again at decision, idempotent decisions. Approved preview says explicitly that nothing was sent or paid.
- Per-session action/activity visibility; do not expose private source text or model prompts in audit summaries. State writes are crash-conscious (temporary file then replace; no success claim on failed persistence). Activities bounded200entries, actions100. No raw audio stored after processing.
- Local inference timeout90seconds and one concurrentgeneration; clear error/fallback when unavailable; text answers always grounded/qualified. `reqwest` only to configuredloopback Ollama. Model output renders as text, not HTML. Cancel UI must ignore late responses; no model-based external actions.
- Developer path resolution must stay under canonical repo root, disallow hidden/secrets and `.git`, `venv`, `node_modules`, `target`; reject absolute/parent paths, symlink escapes and oversized files. Exclude credentials/key files. Do not execute inspected code. Filelist limited150 entries; include main.py/README and new prototype source.

## Visual system

Primary concept: `docs/product-plan/assets/edith-household-concept.png` (1536x1024). Evergreen sidebar approximately250px at this width, ivory-white main, restrained serif headings, sans UI, open conversation panel and separated context rail. Main navigation Overview / Knowledge / Home / Developer / Play / Assist / Activity. Settings and demo profile at sidebar bottom. Responsive collapse for900px/tablet and600px/mobile; keyboard focus, labelled controls, reduced motion, readable elder mode. Functional subviews reuse this system. No fabricated live connections or LLM response. State labels may differ from initial concept when an actual local model is connected; record that intentional functional difference.

## Completion evidence for the first prototype

- Cargo build/tests and frontend TypeScript/build succeed.
- HTTP checks prove scoped RAG/deletion, role denial (including direct API calls), replay/expiry controls, path traversal denial, persistence and honest demo outcomes.
- Actual local model inference tested if installed; if not, explicitly mark generation unavailable rather than fake it.
- Synthetic speech WAV → local Windows recognition → command transcript verified; browser mic flow manually available, not falsely declared real-user tested. Speech output and stop UI verified without unexpected automatic playback.
- Browser review at desktop/mobile: conversation, note add/search/delete, light toggles, child game, elder accessibility, shopping/message preview/approve, developer source/explanation and activity.
- Full architecture document addresses every requested advanced capability, storage/permissions/RAG/audio/operations/costs/integration limitations and phases. No claim prototype implements those production milestones.

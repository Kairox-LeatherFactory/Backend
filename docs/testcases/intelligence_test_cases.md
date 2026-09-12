# Intelligence (Chatbot) — API Test Cases

> Chatbot — deterministic agent with an optional LLM path · module `app/modules/intelligence` · 28 cases

Request-level test cases for the chatbot API: a JSON question/answer endpoint and an SSE streaming variant. Both run the same agent — a keyword intent router over four production tools (schedule status, bottleneck, plan, overview) — so most of the testable behaviour is intent routing, degradation and auth rather than status codes.

## Connection

| | |
|---|---|
| Base URL | `http://127.0.0.1:8000` |
| Module prefix | `/api/v1` |
| Login | `POST /api/v1/auth/login` with `{"username": phone, "password": phone}` |
| Auth header | `Authorization: Bearer <token>` |
| Docs | `/docs` (Swagger) |

### Demo accounts (phone = password)

| Role | Phone | Collection variable |
|---|---|---|
| Managing Director | `9000000000` | `{{md_token}}` |
| Direct Manager | `9000000001` | `{{dm_token}}` |
| Cutting Manager | `9000000002` | `{{cm_token}}` |
| Stitching Manager | `9000000003` | `{{other_token}}` |
| Office Viewer | `9000000004` | `{{viewer_token}}` |
| HR / Accounts | `9000000005` | `{{hr_token}}` |

## Case mix

| Category | Count |
|---|---|
| Positive (POS) | 8 |
| Negative (NEG) | 6 |
| Boundary (BND) | 6 |
| Business Logic (LGC) | 8 |
| **Total** | **28** |

## Endpoints covered

| Group | Endpoints | Role gate | Cases |
|---|---|---|---|
| A · Ask a question | `POST /chat` | Any authenticated user | 20 |
| B · Streaming answers | `POST /chat/stream` | Any authenticated user | 8 |

## Verify before relying on

- **No role gate at all.** Both endpoints depend only on `get_current_user`, so every logged-in role reaches them. The only auth case that fails is a missing or invalid token (401). Whether a Viewer should be able to ask for factory-wide numbers is a product question worth raising.
- **Intent routing is keyword matching, in a fixed order**: bottleneck words → plan words → overview words → schedule (default when a style is named). The first matching branch wins, so a question containing two intents resolves to whichever appears earlier in that chain, not to the more specific one.
- The literal word **"leather"** is in the *planning* keyword list. Any question mentioning leather routes to the planner — see A-L3.
- `use_llm: true` only reaches the LangGraph agent when `CHAT_MODEL` is configured; otherwise the request silently falls back to the deterministic router. The response shape is identical either way, so assert on behaviour, not on which engine ran.
- `/chat/stream` computes the **whole** answer first and then chunks it word by word for a typing effect. Time-to-first-token therefore equals full compute time — it is not a real token stream (B-L1).
- `ChatRequest.question` has no length bound and no minimum; empty and enormous strings are both accepted (A-B1, A-B2).

## A · Ask a question

`POST /chat` · **Role gate:** Any authenticated user

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **A-P1** | POS | High | Schedule status for a named style<br>_Any authenticated role_ | `POST /api/v1/chat`<br>`Body: {"question":"Is CARNABY on schedule?","use_llm":false}` | 200 {"answer":"<headline + advice>","tool":"schedule_status","data":{ok, required rate, projected finish, …}} — substitute a style name that exists in your seed data. |
| **A-P2** | POS | High | Bottleneck question<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"Where is the bottleneck right now?"}` | 200 with tool "bottleneck" and data naming the slowest stage/department plus a note. Try the synonyms too — "which department is lagging", "what is slowing production" — they all route here. |
| **A-P3** | POS | High | Planning with a blocked style<br>_Any authenticated role_ | `POST /api/v1/chat`<br>`Body: {"question":"Plan production, RICANO leather is late"}` | 200 with tool "plan"; the named style is passed as a blocked style, so the returned daily targets route capacity to the other styles. The answer text should say which style was held back. |
| **A-P4** | POS | Medium | Factory overview<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"Give me an overview of the factory"}` | 200 with tool "overview" and data carrying clients, styles, total ordered and total produced. |
| **A-P5** | POS | Medium | A daily capacity figure is picked up<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"What rate do we need for CARNABY if we can do 40 a day?"}` | 200; the parsed capacity (40/day) constrains the answer — the router extracts "N a day" / "N per day" / "N jackets a day" and passes it to the tool as max_daily. |
| **A-P6** | POS | Low | Every logged-in role can ask<br>_Office Viewer (9000000004), then Security Gate (9000000007)_ | `POST /api/v1/chat — Body: {"question":"Give me an overview"} as each role` | 200 for both — there is no role gate. Note in your report whether factory-wide numbers should really be readable by a gate/security login. |
| **A-N1** | NEG | High | No token<br>_Unauthenticated_ | `POST /api/v1/chat with no Authorization header — Body: {"question":"overview"}` | 401 {"detail":"Could not validate credentials"} with a WWW-Authenticate: Bearer header. |
| **A-N2** | NEG | High | Missing the question field<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {}`<br>`then Body: {"use_llm":true}` | 422 both times — `question` is required on ChatRequest. |
| **A-N3** | NEG | Medium | Wrong types<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question": 123}`<br>`then Body: {"question":"overview","use_llm":"yes"}` | The first 422s (int is not a string under strict coercion — confirm against your Pydantic settings). The second may be accepted, since "yes" is a truthy string for bool coercion — record which. |
| **A-N4** | NEG | Low | Expired or garbage token<br>_Any role with a bad token_ | `POST /api/v1/chat with Authorization: Bearer not.a.real.token` | 401 — the token is decoded before the user lookup, so a malformed token fails the same way an expired one does. |
| **A-B1** | BND | Medium | Empty question<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":""}` | 200 with the fallback answer ("I can answer questions about production schedules, bottlenecks, and planning…"), tool null and data.styles listing up to 8 known styles. No 422 — there is no min_length on the field. |
| **A-B2** | BND | Medium | Very long question<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"<10,000 characters of text>"}` | 200, no crash and no timeout — the whole string is lowercased and keyword-scanned. Flag the absence of any length bound: this is the cheapest way to make the endpoint do work. |
| **A-B3** | BND | Medium | Unknown style name<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"Is NOTASTYLE on schedule?"}` | 200 — schedule words are present but no known style matches, so the answer asks which style you mean and data.styles offers real names. Never a 404 or a 500. |
| **A-B4** | BND | Low | Style name in the wrong case or padded<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"is carnaby on schedule"}  (lowercase, no punctuation)` | 200 with tool schedule_status — matching is done on a lowercased question, so case and trailing punctuation must not matter. If it fails here, the style matcher is stricter than the router and that is worth a bug. |
| **A-L1** | LGC | High | Ambiguity produces a question, not a guess<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"are we late?"}` | 200; schedule words matched but no style was named, so the answer is a clarifying question naming an example style, with tool null and data.styles populated. The agent must not pick a style on the user's behalf. |
| **A-L2** | LGC | High | use_llm with no model configured degrades silently<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"Where is the bottleneck?","use_llm":true}`<br>`[in an environment where CHAT_MODEL is unset]` | 200 with the same deterministic answer as A-P2 — never a 500, never a hang, and no error field telling the caller the LLM was skipped. Confirm; a caller cannot currently tell which engine answered. |
| **A-L3** | LGC | High | "leather" hijacks the question into the planner<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"How much leather do we have in stock?"}` | 200 with tool "plan" — "leather" is a planning keyword, so a stock question is answered with a production plan. The routing is keyword-order based, not intent-based. Confirmed quirk; file it with this exact example. |
| **A-L4** | LGC | Medium | First matching branch wins<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"Give me an overview of the bottleneck"}` | tool "bottleneck", not "overview" — bottleneck is checked first. Worth documenting for anyone writing UI suggestion chips, so the chips don't collide. |
| **A-L5** | LGC | Medium | The fallback teaches the user<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"what is the weather"}` | 200 with the fallback answer listing the three things it can actually do, tool null, and real style names in data.styles — an unrecognised question must be a helpful dead end, not an error. |
| **A-L6** | LGC | Low | The answer and the data agree<br>_Any authenticated role_ | `POST /api/v1/chat — Body: {"question":"Is CARNABY on schedule?"}`<br>`Compare the prose in 'answer' against the numbers in 'data'` | The rendered sentence is built from the same tool result it returns, so the headline verdict, the rate and the dates in the prose must match `data` exactly. A mismatch means a renderer bug and would make the chatbot untrustworthy. |

## B · Streaming answers

`POST /chat/stream` · **Role gate:** Any authenticated user

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **B-P1** | POS | High | Consume the SSE stream<br>_Any authenticated role_ | `curl -N -X POST {{base_url}}/api/v1/chat/stream -H "Authorization: Bearer {token}" -H "Content-Type: application/json" -d '{"question":"Where is the bottleneck?"}'` | 200 with content-type text/event-stream: a sequence of `data: {"delta":"<word> "}` frames, then a final `data: {"done":true,"tool":"bottleneck","data":{…}}` frame. The -N flag is required or curl buffers the whole response. |
| **B-P2** | POS | Medium | The streamed text equals the JSON answer<br>_Any authenticated role_ | `POST /api/v1/chat with a question, note 'answer'`<br>`Stream the same question through /api/v1/chat/stream and concatenate every delta` | The concatenated deltas reproduce the JSON endpoint's `answer` (allowing for the trailing space on each word), and the final frame's tool/data match. The two endpoints must never diverge. |
| **B-N1** | NEG | High | No token on the stream<br>_Unauthenticated_ | `curl -N -X POST {{base_url}}/api/v1/chat/stream with no Authorization header` | 401 before any stream opens — the dependency runs first, so you get a normal JSON error, not an event-stream containing an error. |
| **B-N2** | NEG | Medium | Missing question on the stream<br>_Any authenticated role_ | `POST /api/v1/chat/stream — Body: {}` | 422, again as a normal JSON response rather than an SSE frame. |
| **B-B1** | BND | Medium | Empty question streams the fallback<br>_Any authenticated role_ | `Stream Body: {"question":""}` | 200; the fallback sentence arrives word by word and the final frame carries tool null with data.styles. Confirm the stream terminates rather than hanging on an empty answer. |
| **B-B2** | BND | Low | Client disconnects mid-stream<br>_Any authenticated role_ | `Start the stream and kill the client after the first few frames`<br>`Check the server log for an unhandled exception` | The generator stops cleanly; no traceback, no stuck worker. The whole answer was already computed before the first frame, so nothing is left half-done server-side. |
| **B-L1** | LGC | High | It is a fake stream, and that shows in the timing<br>_Any authenticated role_ | `Time the first delta frame of /chat/stream`<br>`Time the full response of /chat for the same question` | Time-to-first-token is roughly the FULL compute time, not a fraction of it — the endpoint computes the whole answer then chunks it at ~20 ms per word. A slow tool therefore looks like a frozen UI, not a slow typist. Document it; this is the thing to change when a real LLM is wired in. |
| **B-L2** | LGC | Medium | Postman's plain Send is the wrong tool here<br>_Any authenticated role_ | `Send /api/v1/chat/stream from Postman's normal request view` | The request appears to hang or buffers until the whole stream ends. That is a client limitation, not an API defect — use curl -N or a client with SSE support. Same caveat as the BOM module's notification stream. |

---

Generated from `docs/testcases/_cases/intelligence.cases.json` by `scripts/build_testcase_collections.js`. Edit the case file and re-run, so this sheet and `intelligence.postman_collection.json` stay in step.

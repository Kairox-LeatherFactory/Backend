# 1. What this service is

**The factory chatbot.** A manager types a question in plain English and gets back a plain-English answer **plus the structured data behind it**:

```
POST /api/v1/chat
{ "question": "Is CARNABY on schedule?" }

-> 200
{
  "answer": "CARNABY: BEHIND. At 12/day it finishes ~4 working day(s) late. …",
  "tool":   "schedule_status",
  "data":   { "ok": true, "remaining": 148, "required_rate": 16.4,
              "current_rate": 12.0, "projected_finish": "2026-10-14",
              "days_late": 4, "feasible": true, … }
}
```

Two endpoints, and that is the whole HTTP surface:

| Endpoint | Returns |
|---|---|
| `POST /api/v1/chat` | the full JSON answer above |
| `POST /api/v1/chat/stream` | the **same answer**, word by word, as Server-Sent Events for a typing UI |

It is `app/modules/intelligence/`, and it is the only service in KairoX with a language model anywhere near it.

---

# 2. The one design decision that matters

> **The model never does the arithmetic.**

Every question this chatbot is actually asked is arithmetic over SQL: how many pieces are left, what rate do we need, which stage is slowest, what should we start first. Those are computed **exactly**, by pure Python functions, against the production tables. A language model — when one is configured at all — only **routes** (which question is this?) and **phrases** (say it in English).

Three things follow, and they are the reason to build it this way:

- **The numbers are right.** They come from the same rows the analytics service reads, not from a model's recollection of them.
- **A small, cheap model is enough** — including a free local one — because routing and phrasing are easy and the hard part was never given to it.
- **It runs with no model at all.** See below.

RAG is used for exactly **one** thing: free-text questions against the workflow document. It is one tool among the others, not the mechanism.

---

# 3. Two backends, one response shape

`ChatRequest` carries a `use_llm` flag (default `false`), and there are two ways the question can be answered. **Both return `{answer, tool, data}`**, so the frontend never changes and never needs to know which one ran.

## The deterministic router — the default

No model, no API key, no cost. It reads the question, matches keywords and entities, picks a tool, runs it, and renders the structured result into a sentence. It answers every question in §4 correctly today.

It finds a style name by matching the **real style names out of the database**, longest first, case-insensitively — so "carnaby" finds `CARNABY`. It pulls a capacity out of phrasings like "20/day", "20 per day", "20 jackets a day".

When it cannot identify the style, it **asks**, and puts the known style names in `data.styles` so the UI can offer them:

```
"Which style do you mean? Try e.g. 'is CARNABY on schedule?'"
```

## The LangGraph ReAct agent — when a model is configured

Set `CHAT_MODEL` and send `use_llm: true`, and the question goes to a real ReAct agent with the same tools bound to it. Its advantage is **multi-step questions** — *"is Carnaby on track AND where's the bottleneck?"* — which the ReAct loop answers by calling several tools in turn.

```
CHAT_MODEL=ollama:qwen2.5:3b-instruct        local, free
CHAT_MODEL=anthropic:claude-3-5-haiku-latest
CHAT_MODEL=openai:gpt-4o-mini
```

## It never errors its way out of an answer

The fallbacks are layered, and all three land somewhere useful:

| Situation | What happens |
|---|---|
| `use_llm: false` (the default) | the deterministic router |
| `use_llm: true` but **`CHAT_MODEL` is unset** | the deterministic router — **silently, and the answer is still exact** |
| `use_llm: true`, a model is set, and it **fails** | caught, and the deterministic router answers |

> **There is no configuration under which this endpoint returns a 500 because of the model.** A model outage degrades the phrasing, never the answer.

---

# 4. What it can actually answer

Four tools. The router picks one; the ReAct agent may call several.

## `schedule_status` — one style against its deadline

*"Is CARNABY on schedule?", "will RICANO be late?", "what daily rate do we need for TOWER?"*

| Field | Meaning |
|---|---|
| `remaining` | ordered − through the final stage |
| `working_days_left` | to the deadline, **Monday–Saturday** (Sunday off) |
| `required_rate` | pieces/day needed from now to hit the deadline |
| `current_rate` | the observed recent rate for this style |
| `projected_finish` | when it finishes at the current rate — `null` if nothing is moving |
| `days_late` | 0 if on time; working days past the deadline otherwise |
| `feasible` | **can it be done at all within capacity?** |
| `headline`, `advice` | the sentence, and what to do about it |

The verdict is one of four, and the fourth is the one worth escalating:

- **complete** — nothing left
- **ON SCHEDULE** — hold the pace
- **BEHIND** — raise the rate to *X*/day, which capacity allows
- **AT RISK** — the required rate **exceeds capacity**. The deadline cannot be met as things stand, and `advice` names the three real options: extend the deadline, add a line or shift, or split the shipment.

## `bottleneck` — which stage is dragging

*"Where is the bottleneck?", "which department is slow?", "why are we behind?"*

Each stage's throughput over the **last 14 days** is compared with the **fastest stage upstream of it** — not merely the adjacent one, so a gap is still caught when the stage in between has logged nothing.

| Ratio to upstream | `severity` |
|---|---|
| below 0.5 | **`critical`** — "this is the bottleneck" |
| 0.5 to 0.75 | `watch` — a backlog is forming |
| above 0.75 | `ok` |

`data.results` is every stage; `data.flagged` is the `critical` and `watch` ones. With nothing logged recently it answers honestly: *"No recent production logged."*

## `plan` — daily targets across every active style

*"Plan production", "what should we start first?", "re-plan, RICANO leather is late", "set targets at 20 a day"*

Earliest-deadline-first across all styles with a live order. Each line carries a `daily_target`, a `start`, a `finish`, the `deadline`, and a `status` of `planned` / `tight` / `infeasible` / `blocked`.

Two inputs it picks out of the question itself:

- **a shared capacity** ("20 a day") caps the sum of the daily targets rather than each one
- **a blocked style** — a style named alongside "late", "delayed", "not arrived" or "missing" is **deferred, and its capacity is reallocated to the rest**. That is the re-calibration case, and it is the single most useful thing this tool does: the answer to "the leather for X hasn't turned up" is a new plan for everything else.

## `overview` — the factory in one line

*"How is the factory doing?", "give me a summary"*

Clients, styles, total ordered, total finished, percent complete.

## And when it recognises nothing

It says what it *can* do and lists the style names, rather than guessing:

> *"I can answer questions about production schedules, bottlenecks, and planning. Try: 'Is CARNABY on schedule?', 'Where is the bottleneck?', or 'Plan production, RICANO leather is late.'"*

---

# 5. Where the numbers come from

| Quantity | Read from |
|---|---|
| ordered | `SKU.qty_ordered`, summed for the style |
| produced | `ProductionEvent.qty` at the **final stage** (`Operation.code == "FF"`) |
| recent rate | the same events over a **14-day** lookback, divided by working days |
| deadline | the style's order; **falls back to today + 30 days** when none is set |
| capacity | **20 pieces/day** unless the question names one |

Two of those are defaults, not facts — the 30-day deadline and the 20/day capacity. A style with no deadline on file is being judged against an assumption. Where a screen shows an AT RISK verdict, it is worth showing the deadline it was measured against.

The working week is **Monday to Saturday**; Sunday is not counted.

> **This is a live read, exactly like Analytics.** It owns no tables and writes nothing. It will legitimately disagree with a closed wage run or any other frozen snapshot, for the same reason Analytics does.

---

# 6. The streaming endpoint

`POST /api/v1/chat/stream` takes the same body and returns `text/event-stream`.

```
data: {"delta": "CARNABY: "}
data: {"delta": "BEHIND. "}
…
data: {"done": true, "tool": "schedule_status", "data": { … }}
```

Two things to build against:

- **the deltas are words, not tokens.** The backend computes the whole answer first and then chunks it for a live-typing effect, at about 20 ms a word.
- **the `data` block arrives on the final frame**, with `done: true`. Do not try to parse it out of the deltas.

When a real model is wired in, the chunker is replaced by the model's own token stream. **The endpoint shape and the frontend do not change** — which is why building against this shape now is safe.

---

# 7. RAG — the one place it belongs

`rag.py` answers free-text process questions against the workflow document (`Leather_Factory_Workflow.docx`): *"what happens at the fusing stage?"*, *"why does a cutting delay cascade?"*

```
chunk the doc → embed → FAISS index → top-k by cosine → POST-FILTER on a score floor
```

The post-filter is the part worth knowing: weak matches are **dropped** rather than padding the answer with irrelevant text. The index is built lazily on the first query and cached in-process, so the embedding model loads once. In-process FAISS is right at this corpus size; a managed vector database is the move only when the corpus grows.

It needs optional extras (`langchain-huggingface`, `sentence-transformers`, `langchain-community`, `faiss-cpu`, `python-docx`). **Without them it returns a clear, non-fatal note and the rest of the chatbot keeps working.**

---

# 8. Configuration

| Setting | Default | Effect |
|---|---|---|
| `CHAT_MODEL` | `""` (empty) | **empty = deterministic router.** Set `provider:model` to enable the ReAct agent. |
| `LANGCHAIN_TRACING_V2` | unset | `true` plus `LANGCHAIN_API_KEY` sends traces to LangSmith — no code change |

Model choices are catalogued in `models_catalog.py`. Embeddings: `all-MiniLM-L6-v2` (default, fast), `all-mpnet-base-v2`, `bge-m3` (multilingual), `e5-large-v2`, `nomic-embed-v1.5`.

> **One trap worth repeating:** `MiniMaxAI/MiniMax-M2` is a **chat/reasoning** model — use it as `CHAT_MODEL`. It is **not** a sentence-similarity model and will not work as an embedding model. Pick embeddings from the list above.

---

# 9. Who can call it

**Any logged-in user.** Both endpoints require a valid token and check no role beyond that.

That is a deliberate breadth — the point of the chatbot is that a supervisor can ask where the bottleneck is without learning a dashboard — but it is worth knowing what it implies: **the tools read production across every style and client and apply no per-client scoping.** Anyone with a login can ask about anything in the factory. If client-scoped answers are ever needed, the filter belongs in `tools.py`, not in the prompt.

---

# 10. Notes for backend developers

- **`forecast.py` is pure math.** No DB, no I/O, no session — `schedule_status`, `detect_bottleneck`, `plan_styles`. That is what makes the scheduling logic unit-testable without a database, and it is where a rule change belongs.
- **`tools.py` is the only layer that touches SQL.** It reads, hands plain values to `forecast.py`, and returns a dict. Adding a capability means adding a tool there and a route to it in `agent.py` — not enlarging a prompt.
- **`service.py` is deliberately thin**, and separate from the router, so the agent can also be driven by a scheduled job (a nightly production report) without going through HTTP.
- **The DB session reaches the LangChain tools through a contextvar**, set per request. LangChain tool functions have sync signatures, so this keeps `db` out of the tool schema the model sees — the model is shown business arguments only.
- **`model=None` yields a fake deterministic chat model**, so the whole LangGraph graph still runs in tests and CI with no API key. The wiring is proved end to end without spending anything.
- **Adding a capability does not change the response contract.** `{answer, tool, data}` is fixed; `tool` names which one ran and `data` is its structured result.

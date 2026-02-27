# Purpose

Nipper exists as a learning project — a minimal clone of [OpenClaw](https://github.com) built from first principles. The goal is not to ship a product but to deeply understand the architecture that separates a simple chatbot from an autonomous AI agent. By building each layer by hand, you confront the real engineering decisions behind systems like Claude Code, OpenClaw, and other tool-using assistants.

This document breaks down every aspect of the application from the perspective of a web engineer.

---

## Why Build This

Most web engineers interact with LLMs through chat interfaces or API wrappers. That surface-level exposure hides the hard problems: How does an agent decide to use a tool? How do you persist context across sessions? How do you let an AI run shell commands without giving it root access to your life?

Building Nipper answers these questions by forcing you to implement:

- A **tool-use loop** that lets the LLM call functions and react to their output
- A **permission model** that gates dangerous operations behind user approval
- A **memory system** that survives session resets and program restarts
- A **multi-agent router** that directs messages to specialized agents
- A **session store** that persists conversation history in a crash-safe database
- A **context compaction** strategy that keeps conversations running indefinitely

None of these are exotic concepts, but implementing them from scratch reveals the trade-offs that documentation glosses over.

---

## The Agent Loop

This is the core of the entire application. Everything else exists to support it.

```
User message
     |
     v
+--- LLM Call <----------------+
|         |                    |
|   Stop reason?               |
|     +-- end_turn --> Return  |
|     +-- tool_use --> Execute |
|                        |     |
|              Tool results ---+
+------------------------------+
     (max 20 iterations)
```

The loop in `run_agent_turn()` sends the full message history plus tool definitions to the LLM. If the model responds with text and a `stop_reason` of `end_turn`, the turn is over. If it responds with `tool_use` blocks, Nipper executes each tool, appends the results to the conversation, and calls the LLM again. This repeats until the model is done or 20 iterations are reached.

**What this teaches you:** The fundamental insight is that tool-using LLMs are not one-shot request/response systems. They are iterative. The model reasons, acts, observes, and reasons again. This is the same ReAct (Reason + Act) pattern that powers every serious AI agent. As a web engineer, this is analogous to a server-sent event loop or a recursive middleware chain — the LLM is both the controller and the decision-maker in a loop you provide.

The 20-iteration cap is a safety valve. Without it, a confused model could loop forever, burning API credits and never returning. Real production agents use similar caps combined with cost tracking.

---

## Tool Definitions and Execution

Nipper defines five tools using JSON Schema, the same format the Anthropic and OpenAI APIs expect:

| Tool | What It Does |
|---|---|
| `run_command` | Executes a shell command via `subprocess.run()` |
| `read_file` | Reads a file from disk (truncated to 10,000 chars) |
| `write_file` | Writes content to a file, creating parent directories |
| `save_memory` | Stores a key/value pair in long-term memory |
| `memory_search` | Full-text searches across all stored memories |

The tool schemas are sent to the LLM with every API call. The model chooses which tool to invoke (if any) and provides structured JSON input. Nipper's `execute_tool()` function dispatches based on the tool name and returns a string result.

**What this teaches you:** Tool use is the bridge between language and action. The LLM never actually runs code — it produces structured JSON that says "I want to call `run_command` with `{"command": "ls -la"}`". Your application is responsible for executing that intent and feeding the result back. This is exactly like a REST API where the client (LLM) sends a request and the server (your code) fulfills it. The difference is that the client is non-deterministic and you have to validate its requests.

The JSON Schema format matters. If your schema is vague, the model will send malformed input. If your descriptions are unclear, the model will misuse the tool. Schema design is API design — the same skills that make you good at designing REST endpoints make you good at designing tool interfaces for LLMs.

---

## Permission Model

Not all tools are equal. `read_file` is harmless. `run_command` can delete your home directory.

Nipper implements a three-tier permission system for shell commands:

1. **Safe commands** — A hardcoded allowlist (`ls`, `cat`, `date`, `git`, etc.) that execute immediately
2. **Previously approved** — Commands the user approved before, stored in the `approvals` SQLite table
3. **Needs approval** — Everything else prompts the user in the terminal

```python
SAFE_COMMANDS = {"ls", "cat", "head", "tail", "wc", "date", "whoami",
                 "echo", "pwd", "which", "git", "python", "node", "npm"}
```

The safety check extracts the base command (first token) and looks it up. Approvals and denials are persisted so you only need to decide once per unique command string.

**What this teaches you:** Every agentic system faces the same problem: the LLM has no inherent sense of danger. It will happily `rm -rf /` if it thinks that solves your problem. Permission surfaces are where AI safety meets practical engineering. This is directly analogous to CORS, CSP headers, and OAuth scopes in web development — you define a boundary and enforce it in code. The difference is that your "client" is an LLM that might find creative ways to circumvent your rules (e.g., using `bash -c "rm -rf /"` to bypass a check on `rm`). Nipper's base-command check is intentionally simple to show the pattern, but production systems need deeper analysis.

---

## Provider Abstraction

Nipper supports both Anthropic (Claude) and OpenAI (GPT-4o) through a provider abstraction layer. The application stores all messages in a canonical format (Anthropic's structure) and converts to/from OpenAI's format at the API boundary.

The key functions:

- `call_llm()` — Routes to the correct API based on the agent's configured provider
- `parse_response()` — Normalizes the response into a common `{"text", "tool_calls", "done"}` shape
- `_to_openai_message()` — Converts canonical messages to OpenAI's format (tool calls, tool results)
- `_migrate_message()` — Handles backward compatibility for old session data

**What this teaches you:** This is the adapter pattern applied to LLM APIs. Anthropic and OpenAI have fundamentally different message formats — Anthropic uses `tool_use`/`tool_result` content blocks within messages, while OpenAI uses separate `tool` role messages and `tool_calls` arrays. By picking one canonical format and converting at the boundaries, you avoid leaking provider-specific logic throughout the codebase. This is the same strategy web engineers use when wrapping multiple payment processors or email providers behind a unified interface. The migration function also demonstrates a real-world concern: when you change internal formats, you need to handle data that was persisted under the old schema.

---

## Session Management and SQLite Storage

Conversations are stored in a SQLite database at `~/.nipper/nipper.db`. Each message is a row in the `sessions` table with a `session_key`, `role`, JSON-encoded `content`, and `created_at` timestamp.

```sql
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_sessions_key ON sessions(session_key);
```

The design is append-only: new messages are inserted without rewriting history. Sessions are identified by keys like `agent:main:repl` or `cron:morning-check`.

**What this teaches you:** This is event sourcing in miniature. Instead of storing "the current state of the conversation," you store every message as an immutable event. Loading a session replays all events in order. This makes it trivial to debug what happened, implement undo, or fork a conversation. WAL (Write-Ahead Logging) mode enables concurrent reads and writes without locking — the same pattern used by high-traffic web applications that read more than they write. For a web engineer, this is analogous to choosing between storing a user's "current cart" vs. storing every "add to cart" / "remove from cart" event. The event-sourced approach costs more storage but gives you a complete audit trail.

---

## Long-Term Memory

Memory is separate from session history. It uses two SQLite tables:

- `memories` — A key/value store where the key is a short label (e.g., `user-preferences`) and the value is freeform text
- `memories_fts` — An FTS5 virtual table that mirrors `memories` and enables full-text search

When the agent saves a memory, both tables are updated. When it searches, FTS5 tokenizes the query, OR-joins each word, and returns ranked matches.

```python
fts_query = " OR ".join(query.split())
rows = conn.execute(
    "SELECT key, content FROM memories_fts WHERE memories_fts MATCH ?",
    (fts_query,)
).fetchall()
```

**What this teaches you:** Memory is what separates a stateless chatbot from a persistent assistant. Without it, every conversation starts from zero. The FTS5 approach is SQLite's built-in full-text search engine — it creates an inverted index behind the scenes, similar to how Elasticsearch or Solr work but embedded in a single file. For a web engineer, this is the same problem as building a search feature: you need an index structure that's fast for keyword lookup, not just primary key access. The OR-join strategy is deliberately broad (high recall, lower precision) because it's better for the agent to find too many memories than to miss relevant ones.

---

## Context Compaction

LLMs have finite context windows. When a session's estimated token count exceeds 100,000 tokens, Nipper automatically compacts:

1. Split the history in half
2. Summarize the older half by sending it to the LLM with a summarization prompt
3. Replace the old messages with the summary
4. Keep recent messages intact

```python
def compact_session(session_key, messages, agent_config):
    if estimate_tokens(messages) < 100_000:
        return messages
    split = len(messages) // 2
    old, recent = messages[:split], messages[split:]
    # ... summarize old, prepend summary to recent ...
```

**What this teaches you:** This is a lossy compression strategy for conversation history. You trade exact recall of old messages for the ability to keep talking indefinitely. The trade-off is real: after compaction, the agent can't quote an exact response from 50 messages ago, but it retains the key facts and decisions. This is analogous to log rotation in web infrastructure — you keep recent logs in full detail and archive or summarize older ones. The token estimation (`len(json.dumps(m)) // 4`) is a rough heuristic; production systems use proper tokenizers, but the principle is the same.

---

## Multi-Agent Routing

Nipper has two agents:

| Agent | Name | Provider Preference | Role |
|---|---|---|---|
| `main` | Jarvis | Anthropic (Claude) | General-purpose assistant |
| `researcher` | Scout | OpenAI (GPT-4o) | Research specialist |

Routing is prefix-based: messages starting with `/research` go to Scout, everything else goes to Jarvis. Each agent has its own system prompt ("soul"), preferred provider, and session namespace.

```python
def resolve_agent(message_text):
    if message_text.startswith("/research "):
        return "researcher", message_text[len("/research "):]
    return "main", message_text
```

Both agents share the same tool set and memory store, so Scout can save findings that Jarvis reads later.

**What this teaches you:** Multi-agent routing is essentially a message broker pattern. You have incoming messages, routing rules, and specialized consumers. In web terms, this is like an API gateway that routes `/api/search` to one microservice and `/api/users` to another. The shared memory store is the equivalent of a shared database between microservices — it enables cross-agent communication without direct coupling. The "soul" (system prompt) is the personality layer; it determines how the same underlying model behaves differently for each agent role.

---

## Scheduled Heartbeats

A background thread runs a scheduler that triggers agent tasks on a cron-like schedule:

```python
schedule.every().day.at("07:30").do(morning_check)
```

Heartbeat tasks run in isolated sessions (e.g., `cron:morning-check`) so they don't interfere with interactive chat. The scheduler loop checks for pending tasks every 60 seconds.

**What this teaches you:** This is a basic job scheduler — the same concept as cron jobs, Celery beat, or GitHub Actions schedules. The daemon thread pattern (`threading.Thread(daemon=True)`) means the scheduler dies when the main process exits, which is correct for a REPL application. In a web context, you would use a dedicated task queue (Redis + Celery, Bull, etc.) for reliability, but the core idea is identical: trigger autonomous agent actions on a timer without user intervention. This is what makes an agent proactive rather than purely reactive.

---

## The REPL

The main loop is a simple read-eval-print loop:

1. Read user input
2. Parse commands (`/new`, `/quit`, `/research`)
3. Route to the appropriate agent
4. Run the agent turn
5. Print the response

**What this teaches you:** The REPL is the UI layer. In a web application, this would be a WebSocket connection or an HTTP endpoint that accepts messages and streams responses. The REPL pattern strips away all frontend complexity so you can focus on the agent mechanics. If you wanted to put a web UI on Nipper, you would replace the `input()`/`print()` calls with request handlers — the agent loop and everything beneath it stays exactly the same.

---

## Key Takeaways for Web Engineers

1. **LLM agents are iterative, not one-shot.** The tool-use loop is the core pattern. Master it and everything else is plumbing.

2. **Schema design is API design.** The tool definitions you send to the LLM are your API contract. Treat them with the same rigor as REST endpoints.

3. **Persistence is non-optional.** Without session storage and memory, your agent has amnesia. SQLite is enough to start; the patterns scale to Postgres or Redis.

4. **Permissions are a first-class concern.** Any system that executes code on behalf of an LLM needs a permission surface. Design it early, not after the first `rm -rf`.

5. **Provider abstraction pays for itself.** The LLM landscape changes fast. An adapter layer lets you swap models without rewriting your application.

6. **Compaction is compression.** You will hit context limits. Have a strategy before you do.

7. **Multi-agent is just routing.** If you can build a reverse proxy, you can build a multi-agent system. The complexity is in the agents, not the router.

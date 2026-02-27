# Nipper

A minimal OpenClaw clone built from first principles. A persistent, tool-using, multi-agent AI assistant/autonomous task executor that runs in your terminal.

See [purpose.md](purpose.md) for a deep dive into the architecture and what each component teaches from a web engineering perspective.

## Features

- **SQLite Storage** — All data (sessions, memory, approvals) in a single `nipper.db` file with WAL mode
- **Persistent Sessions** — Conversation history stored in SQLite, survives restarts
- **Long-Term Memory** — Key/value memory store with FTS5 full-text search
- **Tool Use** — Shell commands, file read/write, memory save/search
- **Permission Controls** — Safe command allowlist with interactive approval for unknown commands
- **Context Compaction** — Automatic summarization when conversation history approaches token limits
- **Multi-Agent Routing** — Two agents (Jarvis + Scout) with shared memory
- **Scheduled Heartbeats** — Cron-style recurring agent tasks

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- At least one API key: [Anthropic](https://console.anthropic.com/settings/keys) or [OpenAI](https://platform.openai.com/api-keys) (or both)

## Quick Start

### 1. Set your API key(s)

At least one is required. If both are set, Jarvis uses Claude and Scout uses GPT-4o. If only one is set, both agents use that provider.

```bash
export ANTHROPIC_API_KEY=your-key-here   # https://console.anthropic.com/settings/keys
export OPENAI_API_KEY=your-key-here      # https://platform.openai.com/api-keys
```

### 2. Install and run

**With uv (recommended):**

```bash
uv sync
uv run python nipper.py
```

**With pip:**

```bash
pip install anthropic schedule
python nipper.py
```

**Zero-install one-liner (uv):**

```bash
uv run --with anthropic --with schedule python nipper.py
```

**Force a specific provider:**

```bash
uv run python nipper.py --provider anthropic   # both agents use Claude
uv run python nipper.py --provider openai      # both agents use GPT-4o-mini
uv run python nipper.py                        # auto-detect (default)
```

When both API keys are set and no `--provider` flag is given, Jarvis uses Anthropic and Scout uses OpenAI. The startup banner shows which provider each agent is using.

## Usage

Once running, you'll see the REPL prompt:

```
Nipper
  Agents: Jarvis, Scout
  Workspace: ~/.nipper
  Commands: /new (reset), /research <query>, /quit

You:
```

### REPL Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh session (previous session is preserved in the database) |
| `/research <query>` | Route your message to the Scout research agent |
| `/quit` `/exit` `/q` | Exit the program |
| `Ctrl+C` | Exit the program |

### Example Conversations

**General assistant (Jarvis):**

```
You: What's the current date and time?
  🔧 run_command: {"command": "date"}
     → Thu Feb 26 21:30:00 PST 2026

🤖 [Jarvis] It's Thursday, February 26, 2026 at 9:30 PM PST.
```

**Research agent (Scout):**

```
You: /research what is the capital of France
🤖 [Scout] The capital of France is Paris. It has been the country's capital since 987 CE.
```

**Memory persistence:**

```
You: Remember that my favorite language is Python
  🔧 save_memory: {"key": "user-preferences", "content": "Favorite language: Python"}
     → Saved to memory: user-preferences

🤖 [Jarvis] Got it. I've saved that to memory.

You: /new
  Session reset.

You: What's my favorite programming language?
  🔧 memory_search: {"query": "favorite language preferences"}
     → --- user-preferences ---
        Favorite language: Python

🤖 [Jarvis] Your favorite language is Python.
```

## Architecture

### Agents

The system ships with two agents, each with their own personality (SOUL) and session history:

| Agent | Name | Trigger | Role |
|---|---|---|---|
| `main` | Jarvis | Default (any message) | General-purpose personal assistant |
| `researcher` | Scout | `/research <query>` | Research specialist, cites sources |

Both agents share the same tool set and memory store, so findings from Scout are accessible to Jarvis and vice versa.

### Tools

| Tool | Description |
|---|---|
| `run_command` | Execute shell commands (with permission controls) |
| `read_file` | Read file contents (truncated to 10,000 chars) |
| `write_file` | Write content to a file (creates parent directories) |
| `save_memory` | Store information to long-term memory by key |
| `memory_search` | Full-text search across all memories (FTS5) |

### Permission Model

Shell commands are categorized into three tiers:

1. **Safe** — Execute immediately without prompting. Includes: `ls`, `cat`, `head`, `tail`, `wc`, `date`, `whoami`, `echo`, `pwd`, `which`, `git`, `python`, `node`, `npm`

2. **Previously approved** — Commands the user has approved before (stored in the `approvals` table)

3. **Needs approval** — Everything else. The user is prompted in the terminal:

```
  ⚠️  Command: curl https://example.com
  Allow? (y/n):
```

Approved and denied commands are persisted to the SQLite database so you only need to approve once.

### Session Management

Sessions are stored in the `sessions` table of the SQLite database (`nipper.db`). Each message is a row with a `session_key`, `role`, JSON-encoded `content`, and a `created_at` timestamp. This approach is:

- **Append-only** — New messages are inserted without rewriting existing data
- **ACID-safe** — SQLite transactions ensure crash safety
- **Indexed** — Sessions are keyed by `session_key` with a B-tree index for fast lookups

Session keys follow the pattern `agent:<name>:repl` (e.g., `agent:main:repl`, `agent:researcher:repl`, `cron:morning-check`).

### Context Compaction

When a session's estimated token count exceeds **100,000 tokens** (~80% of a 128k context window), the system automatically:

1. Splits the history in half
2. Summarizes the older half using Claude
3. Replaces the old messages with the summary
4. Preserves recent messages intact

This keeps conversations running indefinitely without hitting context limits.

### Long-Term Memory

Memory is separate from session history. It uses two SQLite tables: `memories` for storage and `memories_fts` (an [FTS5](https://www.sqlite.org/fts5.html) virtual table) for full-text search.

The agent can:
- **Save** memories with `save_memory` (key + content, upserts on key)
- **Search** memories with `memory_search` (FTS5 full-text search with OR-based matching)

Memory survives session resets (`/new`) and program restarts.

### Heartbeats

A background scheduler runs cron-style tasks. By default, a morning check is scheduled at **07:30 daily**:

```python
schedule.every().day.at("07:30").do(morning_check)
```

Heartbeat tasks run in isolated sessions (e.g., `cron:morning-check`) so they don't pollute your chat history. Output is printed to the terminal.

To add custom heartbeats, edit the `setup_heartbeats()` function:

```python
def setup_heartbeats():
    # Existing morning check...

    # Add your own:
    def weekly_summary():
        run_agent_turn(
            "cron:weekly-summary",
            "Summarize what we accomplished this week based on memory.",
            AGENTS["main"]
        )

    schedule.every().monday.at("09:00").do(weekly_summary)
```

### Agent Loop

The core agent loop follows a standard tool-use pattern:

```
User message
     ↓
┌─── LLM Call ◄──────────────────┐
│         ↓                      │
│   Stop reason?                 │
│     ├─ end_turn → Return text  │
│     └─ tool_use → Execute tool │
│                        ↓       │
│              Tool results ─────┘
└────────────────────────────────┘
     (max 20 iterations)
```

Each iteration:
1. Sends the full message history + tools to Claude
2. If Claude responds with text (`end_turn`), returns it
3. If Claude requests tool use, executes the tools and feeds results back
4. Repeats until done or 20 iterations reached

## Workspace Layout

All persistent data lives under `~/.nipper/`:

```
~/.nipper/
└── nipper.db                  # SQLite database (WAL mode)
    ├── sessions               # Conversation history (table)
    ├── memories               # Long-term memory (table)
    ├── memories_fts           # Full-text search index (FTS5 virtual table)
    └── approvals              # Approved/denied commands (table)
```

You can inspect the database directly:

```bash
sqlite3 ~/.nipper/nipper.db ".tables"
sqlite3 ~/.nipper/nipper.db "SELECT DISTINCT session_key FROM sessions;"
sqlite3 ~/.nipper/nipper.db "SELECT key, content FROM memories;"
```

## Configuration

### Changing the Model

Edit the `model` field in the `AGENTS` dictionary:

```python
AGENTS = {
    "main": {
        "model": "claude-sonnet-4-5-20250929",  # Change this
        ...
    },
}
```

### Customizing Agent Personality

Edit the `soul` field in the `AGENTS` dictionary. This is the system prompt sent with every API call:

```python
AGENTS = {
    "main": {
        "soul": "You are a helpful coding assistant. Be concise and technical.",
        ...
    },
}
```

### Adding New Agents

Add a new entry to the `AGENTS` dictionary and update `resolve_agent()`:

```python
AGENTS = {
    # ...existing agents...
    "coder": {
        "name": "Dev",
        "model": "claude-sonnet-4-5-20250929",
        "soul": "You are Dev, a coding specialist. Write clean, tested code.",
        "session_prefix": "agent:coder",
    },
}

def resolve_agent(message_text):
    if message_text.startswith("/research "):
        return "researcher", message_text[len("/research "):]
    if message_text.startswith("/code "):
        return "coder", message_text[len("/code "):]
    return "main", message_text
```

### Adding New Tools

1. Add the tool schema to the `TOOLS` list
2. Add the execution logic to `execute_tool()`

```python
# In TOOLS list:
{
    "name": "web_search",
    "description": "Search the web for information",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"}
        },
        "required": ["query"]
    }
}

# In execute_tool():
elif name == "web_search":
    # Integrate with your preferred search API
    return search_web(tool_input["query"])
```

### Modifying Safe Commands

Edit the `SAFE_COMMANDS` set to add or remove commands that execute without approval:

```python
SAFE_COMMANDS = {"ls", "cat", "head", "tail", "wc", "date", "whoami",
                 "echo", "pwd", "which", "git", "python", "node", "npm",
                 "docker", "cargo", "go"}  # Add your trusted commands
```

## Resetting Data

```bash
# Reset all sessions (keeps memory and approvals)
sqlite3 ~/.nipper/nipper.db "DELETE FROM sessions;"

# Reset long-term memory
sqlite3 ~/.nipper/nipper.db "DELETE FROM memories; DELETE FROM memories_fts;"

# Reset command approvals
sqlite3 ~/.nipper/nipper.db "DELETE FROM approvals;"

# Full reset
rm -rf ~/.nipper/
```

## License

MIT

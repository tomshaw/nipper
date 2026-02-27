#!/usr/bin/env python3
# nipper.py
# Run: uv run --with anthropic --with schedule python nipper.py

import anthropic
import openai
import argparse
import subprocess
import json
import os
import sqlite3
import threading
import time
import schedule
from datetime import datetime

# ─── CLI Args ───

_parser = argparse.ArgumentParser(description="Nipper — a multi-agent AI assistant")
_parser.add_argument(
    "--provider", choices=["anthropic", "openai"],
    help="Force all agents to use this provider (ignores the other API key)"
)
_args = _parser.parse_args()

# ─── Client Init ───

anthropic_client = None
openai_client = None

if _args.provider == "anthropic":
    anthropic_client = anthropic.Anthropic()
elif _args.provider == "openai":
    openai_client = openai.OpenAI()
else:
    if os.environ.get("ANTHROPIC_API_KEY"):
        anthropic_client = anthropic.Anthropic()
    if os.environ.get("OPENAI_API_KEY"):
        openai_client = openai.OpenAI()

# ─── Configuration ───

WORKSPACE = os.path.expanduser("~/.nipper")
DB_PATH = os.path.join(WORKSPACE, "nipper.db")

# ─── Provider Detection ───

PROVIDER_MODELS = {
    "anthropic": "claude-sonnet-4-5-20250929",
    "openai": "gpt-4o-mini",
}

def _available_provider():
    """Return the first available provider, preferring anthropic."""
    if anthropic_client:
        return "anthropic", PROVIDER_MODELS["anthropic"]
    if openai_client:
        return "openai", PROVIDER_MODELS["openai"]
    raise SystemExit("Error: Set ANTHROPIC_API_KEY or OPENAI_API_KEY to use Nipper.")

_default_provider, _default_model = _available_provider()

def _pick(preferred, preferred_client):
    """Use the preferred provider if its client is available, otherwise fallback."""
    if preferred_client:
        return preferred, PROVIDER_MODELS[preferred]
    return _default_provider, _default_model

# ─── Agents ───

_main_provider, _main_model = _pick("anthropic", anthropic_client)
_research_provider, _research_model = _pick("openai", openai_client)

AGENTS = {
    "main": {
        "name": "Jarvis",
        "provider": _main_provider,
        "model": _main_model,
        "soul": (
            "You are Jarvis, a personal AI assistant.\n"
            "Be genuinely helpful. Skip the pleasantries. Have opinions.\n"
            "You have tools — use them proactively.\n\n"
            "## Memory\n"
            f"Your workspace is {WORKSPACE}.\n"
            "Use save_memory to store important information across sessions.\n"
            "Use memory_search at the start of conversations to recall context."
        ),
        "session_prefix": "agent:main",
    },
    "researcher": {
        "name": "Scout",
        "provider": _research_provider,
        "model": _research_model,
        "soul": (
            "You are Scout, a research specialist.\n"
            "Your job: find information and cite sources. Every claim needs evidence.\n"
            "Use tools to gather data. Be thorough but concise.\n"
            "Save important findings with save_memory for other agents to reference."
        ),
        "session_prefix": "agent:researcher",
    },
}

# ─── Database ───

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(WORKSPACE, exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);

        CREATE TABLE IF NOT EXISTS memories (
            key TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS approvals (
            command TEXT PRIMARY KEY,
            approved INTEGER NOT NULL
        );
    """)
    # Standalone FTS5 table for memory search (stores its own copy of data)
    try:
        conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(key, content)")
    except sqlite3.OperationalError:
        pass  # already exists
    conn.commit()
    conn.close()

# ─── Tools ───

TOOLS = [
    {
        "name": "run_command",
        "description": "Run a shell command",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file from the filesystem",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates directories if needed)",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "Content to write"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "save_memory",
        "description": "Save important information to long-term memory",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short label (e.g. 'user-preferences')"},
                "content": {"type": "string", "description": "The information to remember"}
            },
            "required": ["key", "content"]
        }
    },
    {
        "name": "memory_search",
        "description": "Search long-term memory for relevant information",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"}
            },
            "required": ["query"]
        }
    },
]

def tools_for_openai():
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]

# ─── Permission Controls ───

SAFE_COMMANDS = {"ls", "cat", "head", "tail", "wc", "date", "whoami",
                 "echo", "pwd", "which", "git", "python", "node", "npm"}

def save_approval(command, approved):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO approvals (command, approved) VALUES (?, ?)",
        (command, 1 if approved else 0)
    )
    conn.commit()
    conn.close()

def check_command_safety(command):
    base_cmd = command.strip().split()[0] if command.strip() else ""
    if base_cmd in SAFE_COMMANDS:
        return "safe"
    conn = get_db()
    row = conn.execute(
        "SELECT approved FROM approvals WHERE command = ?", (command,)
    ).fetchone()
    conn.close()
    if row and row["approved"]:
        return "approved"
    return "needs_approval"

# ─── Tool Execution ───

def execute_tool(name, tool_input):
    if name == "run_command":
        cmd = tool_input["command"]
        safety = check_command_safety(cmd)
        if safety == "needs_approval":
            print(f"\n  ⚠️  Command: {cmd}")
            confirm = input("  Allow? (y/n): ").strip().lower()
            if confirm != "y":
                save_approval(cmd, False)
                return "Permission denied by user."
            save_approval(cmd, True)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            output = result.stdout + result.stderr
            return output if output else "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30 seconds"
        except Exception as e:
            return f"Error: {e}"

    elif name == "read_file":
        try:
            with open(tool_input["path"], "r") as f:
                return f.read()[:10000]
        except Exception as e:
            return f"Error: {e}"

    elif name == "write_file":
        try:
            os.makedirs(os.path.dirname(tool_input["path"]) or ".", exist_ok=True)
            with open(tool_input["path"], "w") as f:
                f.write(tool_input["content"])
            return f"Wrote to {tool_input['path']}"
        except Exception as e:
            return f"Error: {e}"

    elif name == "save_memory":
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO memories (key, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (tool_input["key"], tool_input["content"])
        )
        # Sync FTS: remove old entry, insert new one
        conn.execute("DELETE FROM memories_fts WHERE key = ?", (tool_input["key"],))
        conn.execute(
            "INSERT INTO memories_fts (key, content) VALUES (?, ?)",
            (tool_input["key"], tool_input["content"])
        )
        conn.commit()
        conn.close()
        return f"Saved to memory: {tool_input['key']}"

    elif name == "memory_search":
        conn = get_db()
        query = tool_input["query"]
        # Build an FTS5 query: OR together each word for broad matching
        fts_query = " OR ".join(query.split())
        try:
            rows = conn.execute(
                "SELECT key, content FROM memories_fts WHERE memories_fts MATCH ?",
                (fts_query,)
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        conn.close()
        if rows:
            results = [f"--- {row['key']} ---\n{row['content']}" for row in rows]
            return "\n\n".join(results)
        return "No matching memories found."

    return f"Unknown tool: {name}"

# ─── Session Management ───

def _migrate_message(msg):
    """Convert old provider-specific messages to canonical Anthropic format."""
    role = msg["role"]
    content = msg.get("content")

    # Old OpenAI "tool" role → canonical tool_result
    if role == "tool":
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": msg.get("tool_call_id", "unknown"), "content": content or ""}],
        }

    # Old OpenAI assistant with tool_calls → canonical tool_use blocks
    if role == "assistant" and "tool_calls" in msg:
        blocks = []
        if content:
            blocks.append({"type": "text", "text": content})
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": fn.get("name", ""),
                "input": json.loads(fn.get("arguments", "{}")),
            })
        return {"role": "assistant", "content": blocks}

    # Assistant with string content → wrap in list
    if role == "assistant" and isinstance(content, str):
        return {"role": "assistant", "content": [{"type": "text", "text": content}]}

    return msg

def load_session(session_key):
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM sessions WHERE session_key = ? ORDER BY id",
        (session_key,)
    ).fetchall()
    conn.close()
    messages = []
    for row in rows:
        data = json.loads(row["content"])
        # Backward compat: old format stored full dict with "_full" marker
        if isinstance(data, dict) and data.get("_full"):
            data.pop("_full")
            msg = data
        else:
            msg = {"role": row["role"], "content": data}
        messages.append(_migrate_message(msg))
    return messages

def append_message(session_key, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (session_key, role, content) VALUES (?, ?, ?)",
        (session_key, message["role"], json.dumps(message["content"]))
    )
    conn.commit()
    conn.close()

def save_session(session_key, messages):
    """Replace all messages for a session (used during compaction)."""
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
    rows = [(session_key, m["role"], json.dumps(m["content"])) for m in messages]
    conn.executemany(
        "INSERT INTO sessions (session_key, role, content) VALUES (?, ?, ?)",
        rows
    )
    conn.commit()
    conn.close()

# ─── Provider Abstraction ───

def call_llm(agent_config, messages):
    """Make an API call to the configured provider and return the raw response."""
    provider = agent_config.get("provider", "anthropic")

    if provider == "openai":
        if not openai_client:
            raise RuntimeError("OpenAI provider selected but OPENAI_API_KEY is not set.")
        oai_messages = []
        if agent_config.get("soul"):
            oai_messages.append({"role": "system", "content": agent_config["soul"]})
        for m in messages:
            converted = _to_openai_message(m)
            if isinstance(converted, list):
                oai_messages.extend(converted)
            else:
                oai_messages.append(converted)
        return openai_client.chat.completions.create(
            model=agent_config["model"],
            max_tokens=4096,
            tools=tools_for_openai(),
            messages=oai_messages,
        )
    else:
        if not anthropic_client:
            raise RuntimeError("Anthropic provider selected but ANTHROPIC_API_KEY is not set.")
        return anthropic_client.messages.create(
            model=agent_config["model"],
            max_tokens=4096,
            system=agent_config.get("soul", ""),
            tools=TOOLS,
            messages=messages,
        )

def _to_openai_message(msg):
    """Convert a canonical (Anthropic-format) message to OpenAI format."""
    role = msg["role"]
    content = msg["content"]

    if role == "assistant" and isinstance(content, list):
        text_parts = []
        tool_calls = []
        for block in content:
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    },
                })
        result = {"role": "assistant", "content": "\n".join(text_parts) or None}
        if tool_calls:
            result["tool_calls"] = tool_calls
        return result

    if role == "user" and isinstance(content, list):
        return [
            {"role": "tool", "tool_call_id": block["tool_use_id"], "content": block.get("content", "")}
            for block in content
            if block.get("type") == "tool_result"
        ]

    return {"role": role, "content": content if isinstance(content, str) else json.dumps(content)}

def parse_response(agent_config, response):
    """Extract structured data from a provider response.

    Returns: {"text": str|None, "tool_calls": [{"id", "name", "input"}], "done": bool}
    """
    provider = agent_config.get("provider", "anthropic")

    if provider == "openai":
        choice = response.choices[0]
        message = choice.message
        text = message.content
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments),
                })
        done = choice.finish_reason == "stop"
        return {"text": text, "tool_calls": tool_calls, "done": done}
    else:
        text_parts = []
        tool_calls = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        text = "\n".join(text_parts) if text_parts else None
        done = response.stop_reason == "end_turn"
        return {"text": text, "tool_calls": tool_calls, "done": done}

def format_assistant_message(parsed):
    """Build a canonical assistant message from parsed response data."""
    content = []
    if parsed["text"]:
        content.append({"type": "text", "text": parsed["text"]})
    for tc in parsed["tool_calls"]:
        content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
    if not content:
        content = [{"type": "text", "text": parsed["text"] or ""}]
    return {"role": "assistant", "content": content}

def format_tool_results(tool_results_data):
    """Format tool results as a canonical message.

    tool_results_data: [{"id", "name", "result"}, ...]
    Returns a list of messages to append.
    """
    return [{
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tr["id"],
                "content": tr["result"],
            }
            for tr in tool_results_data
        ],
    }]

# ─── Compaction ───

def estimate_tokens(messages):
    return sum(len(json.dumps(m)) for m in messages) // 4

def compact_session(session_key, messages, agent_config):
    if estimate_tokens(messages) < 100_000:
        return messages
    split = len(messages) // 2
    old, recent = messages[:split], messages[split:]
    print("\n  📦 Compacting session history...")
    summary_prompt = (
        "Summarize this conversation concisely. Preserve key facts, "
        "decisions, and open tasks:\n\n"
        f"{json.dumps(old, indent=2)}"
    )
    compact_config = {**agent_config, "soul": ""}
    summary_response = call_llm(compact_config, [{"role": "user", "content": summary_prompt}])
    parsed = parse_response(compact_config, summary_response)
    compacted = [{
        "role": "user",
        "content": f"[Conversation summary]\n{parsed['text'] or ''}"
    }] + recent
    save_session(session_key, compacted)
    return compacted

# ─── Agent Loop ───

def run_agent_turn(session_key, user_text, agent_config):
    """Run a full agent turn: load session, call LLM in a loop, save."""
    messages = load_session(session_key)
    messages = compact_session(session_key, messages, agent_config)

    user_msg = {"role": "user", "content": user_text}
    messages.append(user_msg)
    append_message(session_key, user_msg)

    for _ in range(20):  # max tool-use turns
        response = call_llm(agent_config, messages)
        parsed = parse_response(agent_config, response)

        assistant_msg = format_assistant_message(parsed)
        messages.append(assistant_msg)
        append_message(session_key, assistant_msg)

        if parsed["done"]:
            return parsed["text"] or ""

        # Execute tools
        tool_results_data = []
        for tc in parsed["tool_calls"]:
            print(f"  🔧 {tc['name']}: {json.dumps(tc['input'])[:100]}")
            result = execute_tool(tc["name"], tc["input"])
            print(f"     → {str(result)[:150]}")
            tool_results_data.append({"id": tc["id"], "name": tc["name"], "result": str(result)})

        result_msgs = format_tool_results(tool_results_data)
        for msg in result_msgs:
            messages.append(msg)
            append_message(session_key, msg)

    return "(max turns reached)"

# ─── Multi-Agent Routing ───

def resolve_agent(message_text):
    """Route messages to the right agent based on prefix commands."""
    if message_text.startswith("/research "):
        return "researcher", message_text[len("/research "):]
    return "main", message_text

# ─── Cron / Heartbeats ───

def setup_heartbeats():
    def morning_check():
        print("\n⏰ Heartbeat: morning check")
        result = run_agent_turn(
            "cron:morning-check",
            "Good morning! Check today's date and give me a motivational quote.",
            AGENTS["main"]
        )
        print(f"🤖 {result}\n")

    schedule.every().day.at("07:30").do(morning_check)

    def scheduler_loop():
        while True:
            schedule.run_pending()
            time.sleep(60)

    threading.Thread(target=scheduler_loop, daemon=True).start()

# ─── REPL ───

def main():
    init_db()

    setup_heartbeats()

    session_key = "agent:main:repl"

    print("Nipper")
    agent_info = ", ".join(f"{a['name']} ({a['provider']})" for a in AGENTS.values())
    print(f"  Agents: {agent_info}")
    print(f"  Workspace: {WORKSPACE}")
    print("  Commands: /new (reset), /research <query>, /quit\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ["/quit", "/exit", "/q"]:
            print("Goodbye!")
            break
        if user_input.lower() == "/new":
            session_key = f"agent:main:repl:{datetime.now().strftime('%Y%m%d%H%M%S')}"
            print("  Session reset.\n")
            continue

        agent_id, message_text = resolve_agent(user_input)
        agent_config = AGENTS[agent_id]
        sk = (
            f"{agent_config['session_prefix']}:repl"
            if agent_id != "main" else session_key
        )

        response = run_agent_turn(sk, message_text, agent_config)
        print(f"\n🤖 [{agent_config['name']}] {response}\n")

if __name__ == "__main__":
    main()

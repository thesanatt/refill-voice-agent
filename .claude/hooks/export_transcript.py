"""Export the current Claude Code session to agent-logs/ as Markdown. Runs from the Stop and SessionEnd hooks."""
import json
import os
import re
import sys
from pathlib import Path

MAX_CHARS = 2000
SECRET = re.compile(r"(sk-ant-[\w-]{16,}|sk-[\w-]{16,}|g?sk_\w{16,}|AIza[\w-]{30,}|gh[pousr]_\w{20,}|xox[abprs]-[\w-]{10,}|Bearer\s+[\w.-]{20,}|\b[0-9a-f]{32,}\b)")
MIN_ENV_SECRET_LENGTH = 16
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def clip(text):
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n... (truncated)"


def env_secrets(root):
    env_file = root / ".env"
    if not env_file.exists():
        return []
    pairs = (line.partition("=") for line in env_file.read_text(encoding="utf-8").splitlines())
    values = (value.strip().strip("'\"") for name, _, value in pairs if not name.lstrip().startswith("#"))
    return [value for value in values if len(value) >= MIN_ENV_SECRET_LENGTH]


def redact(text, secrets):
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return SECRET.sub("[REDACTED]", text)


def render(block):
    kind = block.get("type")
    if kind == "text":
        return REMINDER.sub("", block.get("text", "")).strip()
    if kind == "tool_use":
        args = clip(json.dumps(block.get("input", {}), ensure_ascii=False, indent=1))
        return f"**Tool call: {block.get('name')}**\n```json\n{args}\n```"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        return f"**Tool result**\n```\n{clip(str(content or ''))}\n```"
    return ""


def main():
    event = json.load(sys.stdin)
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    out_dir = root / "agent-logs"
    out_dir.mkdir(exist_ok=True)
    secrets = env_secrets(root)
    entries, started = [], None
    for raw in Path(event["transcript_path"]).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        if rec.get("type") not in ("user", "assistant") or rec.get("isMeta"):
            continue
        content = rec.get("message", {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or [] if isinstance(b, dict)]
        body = "\n\n".join(part for part in map(render, blocks) if part)
        if not body:
            continue
        started = started or rec.get("timestamp", "")
        tool_only = all(b.get("type") == "tool_result" for b in blocks)
        speaker = "Tool" if tool_only else ("User" if rec["type"] == "user" else "Claude")
        entries.append(f"### {speaker} ({rec.get('timestamp', '')})\n\n{redact(body, secrets)}\n")
    if not entries:
        return
    name = f"{started[:10]}_session_{event['session_id'][:8]}.md"
    header = (f"# Claude Code session {event['session_id']}\n\n"
              f"Exported after every turn by .claude/hooks/export_transcript.py. Tool input and output are cut at {MAX_CHARS} characters, and likely API keys are redacted.\n\n")
    (out_dir / name).write_text(header + "\n".join(entries), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"systemMessage": f"Transcript export failed, so agent-logs/ is out of date: {exc}"}))
    sys.exit(0)

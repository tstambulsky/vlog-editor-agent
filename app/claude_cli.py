"""The only module that talks to the Claude Code CLI.

The command is configurable (CLAUDE_CODE_COMMAND, default `claude`) so the
reasoning layer can be swapped without touching the rest of the system.
Calls run headless, read-only, scoped to a working directory, serialized,
schema-validated, and logged.
"""
import asyncio
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import settings

_claude_lock = asyncio.Lock()

T = TypeVar("T", bound=BaseModel)


class ClaudeCliError(RuntimeError):
    pass


def _build_cmd() -> list[str]:
    cmd = shlex.split(settings.claude_code_command)
    cmd += [
        "-p",
        "--output-format", "json",
        "--max-turns", "25",
        "--allowedTools", "Read,Glob,Grep",
    ]
    if settings.claude_code_extra_args:
        cmd += shlex.split(settings.claude_code_extra_args)
    return cmd


def extract_json_block(text: str) -> str:
    """Pull the first JSON object out of a model reply (fenced or bare)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("{")
    if start == -1:
        raise ClaudeCliError(f"No JSON object found in reply: {text[:300]!r}")
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    raise ClaudeCliError("Unbalanced JSON object in reply")


async def _invoke(prompt: str, cwd: Path, log_dir: Path) -> str:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    (log_dir / f"{stamp}_prompt.md").write_text(prompt)

    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(),
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=settings.claude_timeout_s
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeCliError(f"Claude CLI timed out after {settings.claude_timeout_s}s")

    out = stdout.decode(errors="replace")
    (log_dir / f"{stamp}_raw.json").write_text(out)
    if proc.returncode != 0:
        raise ClaudeCliError(
            f"Claude CLI exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
        )

    try:
        envelope = json.loads(out)
        result = envelope.get("result")
        if result is None:
            raise KeyError("result")
        if envelope.get("is_error"):
            raise ClaudeCliError(f"Claude CLI reported error: {str(result)[:500]}")
        return result
    except (json.JSONDecodeError, KeyError):
        # Wrapper may not support --output-format json; fall back to raw stdout.
        return out


async def run_structured(prompt: str, cwd: Path, schema: type[T],
                         extra_validate=None) -> T:
    """Run a reasoning task and validate the reply against a pydantic schema.

    One retry with the validation error appended; serialized so two pipeline
    paths never hold concurrent CLI sessions.
    """
    log_dir = cwd / "claude_logs"
    attempt_prompt = prompt
    last_error = ""
    async with _claude_lock:
        for attempt in (1, 2):
            reply = await _invoke(attempt_prompt, cwd, log_dir)
            try:
                obj = schema.model_validate_json(extract_json_block(reply))
                errors = extra_validate(obj) if extra_validate else []
                if not errors:
                    return obj
                last_error = "; ".join(errors)
            except (ValidationError, ClaudeCliError, ValueError) as exc:
                last_error = str(exc)
            attempt_prompt = (
                f"{prompt}\n\n---\nYour previous reply was invalid:\n{last_error}\n"
                f"Reply again with ONLY a valid JSON object fixing these issues."
            )
    raise ClaudeCliError(f"Claude reply failed validation twice: {last_error[:800]}")


async def health_check() -> bool:
    try:
        reply = await _invoke('Reply with exactly this JSON: {"ok": true}',
                              settings.data_dir, settings.data_dir / "claude_logs")
        return json.loads(extract_json_block(reply)).get("ok") is True
    except Exception:
        return False

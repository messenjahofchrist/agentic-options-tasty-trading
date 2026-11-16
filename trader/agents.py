"""Vendor-neutral AI agents (owner request 2026-11-11).

The bot talks to "an agent", never to a particular product. agents.json (project root) maps each job ("role") to an
agent name and says how every agent is started: a command that takes a prompt and returns text. Claude, Codex,
Gemini or any other command-line bot fit the same contract, so switching is a one-line config change.

    from trader.agents import ask_json
    reply = ask_json("advisor", prompt_text, validate=parse)     # -> AgentReply(agent, data, attempts, errors)

    python -m trader.agents list                 # roles, agents, whether each command is installed
    python -m trader.agents test [agent ...]     # ask each agent for a tiny JSON reply

Agents run in an empty temporary folder with a filtered environment (no broker, mail or API credentials).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .strategy_loader import _strip_json_comments

CONFIG_FILE = "agents.json"
SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|API.?KEY|_KEY$|ACCOUNT.?NUMBER|SMTP|TASTY|MASSIVE|EMAIL", re.I)


class AgentError(RuntimeError):
    pass


@dataclass
class AgentReply:
    agent: str
    data: Any
    attempts: int = 1
    errors: list[str] = field(default_factory=list)


def load_config(root: Path | None = None) -> dict[str, Any]:
    path = (root or Path.cwd()) / CONFIG_FILE
    try:
        config = json.loads(_strip_json_comments(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"Cannot read {path}: {exc}") from exc
    agents, roles = config.get("agents"), config.get("roles")
    if not isinstance(agents, dict) or not isinstance(roles, dict):
        raise AgentError(f"{path.name} needs 'roles' and 'agents' objects")
    for name, spec in agents.items():
        command = spec.get("command") if isinstance(spec, dict) else None
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise AgentError(f"agent {name}: 'command' must be a non-empty list of strings")
        if spec.get("prompt_via", "stdin") not in ("stdin", "argument"):
            raise AgentError(f"agent {name}: prompt_via must be stdin or argument")
        if spec.get("output_from", "stdout") not in ("stdout", "file"):
            raise AgentError(f"agent {name}: output_from must be stdout or file")
        if spec.get("prompt_via") == "argument" and not any("{prompt}" in part for part in command):
            raise AgentError(f"agent {name}: prompt_via=argument needs {{prompt}} in the command")
        if spec.get("output_from") == "file" and not any("{output_file}" in part for part in command):
            raise AgentError(f"agent {name}: output_from=file needs {{output_file}} in the command")
    for role, chosen in roles.items():
        for name in ([chosen] if isinstance(chosen, str) else chosen or []):
            if name not in agents:
                raise AgentError(f"role {role}: unknown agent {name!r}")
    return config


def agents_for(role: str, config: dict[str, Any]) -> list[str]:
    """The agents for a role, in the order to try them. OPTIOBOT_AGENT_<ROLE> (or OPTIOBOT_AGENT) overrides the file."""
    override = os.environ.get(f"OPTIOBOT_AGENT_{role.upper()}") or os.environ.get("OPTIOBOT_AGENT")
    chosen = override.split(",") if override else config["roles"].get(role)
    if not chosen:
        raise AgentError(f"No agent is configured for role {role!r} in {CONFIG_FILE}")
    names = [chosen] if isinstance(chosen, str) else list(chosen)
    unknown = [n for n in names if n not in config["agents"]]
    if unknown:
        raise AgentError(f"role {role}: unknown agent(s) {', '.join(unknown)}")
    return names


def safe_environment() -> dict[str, str]:
    """Only OS/user-profile variables; never broker, mail or API credentials."""
    return {key: value for key, value in os.environ.items() if not SECRET_NAME.search(key)}


def run_agent(name: str, prompt: str, *, config: dict[str, Any], timeout: int = 420) -> str:
    """Run one agent once and return its raw text reply."""
    spec = config["agents"][name]
    executable = shutil.which(spec["command"][0])
    if not executable:
        raise AgentError(f"{name}: command {spec['command'][0]!r} is not installed or not on PATH")
    with tempfile.TemporaryDirectory(prefix=f"optiobot-agent-{name}-") as folder:
        output_file = Path(folder) / "reply.txt"
        fill = {"{workdir}": folder, "{output_file}": str(output_file), "{prompt}": prompt}
        argv = [executable]
        for part in spec["command"][1:]:
            for key, value in fill.items():
                part = part.replace(key, value)
            argv.append(part)
        by_stdin = spec.get("prompt_via", "stdin") == "stdin"
        try:
            result = subprocess.run(argv, input=prompt if by_stdin else None, cwd=folder, env=safe_environment(),
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=int(spec.get("timeout_seconds") or timeout), check=False,
                                    stdin=None if by_stdin else subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"{name}: no reply within {exc.timeout:.0f} seconds") from exc
        if result.returncode:
            raise AgentError(f"{name} exited {result.returncode}: {(result.stderr or result.stdout or '')[-1500:]}")
        if spec.get("output_from", "stdout") == "file":
            if not output_file.is_file():
                raise AgentError(f"{name} did not write its reply file")
            return output_file.read_text(encoding="utf-8", errors="replace").strip()
        return (result.stdout or "").strip()


def extract_json(raw: str) -> Any:
    """The outermost JSON object in an agent's reply (agents often wrap it in prose or a code fence)."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the reply contains no JSON object")
    return json.loads(raw[start:end + 1])


def ask_json(role: str, prompt: str, *, validate: Callable[[Any], Any] | None = None, timeout: int = 420,
             attempts_per_agent: int = 2, root: Path | None = None,
             runner: Callable[..., str] | None = None) -> AgentReply:
    """Ask the role's agent(s) for a JSON reply. Each agent gets attempts_per_agent tries; the next configured agent
    is used only when the previous one keeps failing. validate(data) may raise ValueError and may return cleaned data."""
    runner = runner or run_agent
    config = load_config(root)
    errors: list[str] = []
    attempts = 0
    for name in agents_for(role, config):
        for _ in range(attempts_per_agent):
            attempts += 1
            try:
                data = extract_json(runner(name, prompt, config=config, timeout=timeout))
                if validate is not None:
                    cleaned = validate(data)
                    data = data if cleaned is None else cleaned
                return AgentReply(name, data, attempts, errors)
            except (AgentError, ValueError, OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}"[:600])
    raise AgentError("; ".join(errors) or f"no agent answered for role {role}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.agents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show roles and agents")
    test = sub.add_parser("test", help="ask agents for a tiny JSON reply")
    test.add_argument("names", nargs="*")
    test.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    config = load_config()
    if args.command == "list":
        print(json.dumps({"roles": {role: agents_for(role, config) for role in config["roles"]},
                          "agents": {name: {"installed": shutil.which(spec["command"][0]) is not None,
                                            "command": spec["command"][0]} for name, spec in config["agents"].items()}}, indent=2))
        return 0
    failed = 0
    for name in args.names or list(config["agents"]):
        try:
            data = extract_json(run_agent(name, 'Reply with exactly this JSON object and nothing else: {"ok": true}',
                                          config=config, timeout=args.timeout))
            ok = isinstance(data, dict) and data.get("ok") is True
            print(f"{name}: {'OK' if ok else 'unexpected reply ' + json.dumps(data)[:200]}")
            failed += not ok
        except (AgentError, ValueError, OSError) as exc:
            failed += 1
            print(f"{name}: FAILED: {exc}"[:800])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Antigravity lifecycle hooks for Colab Pilot.

Handles:
  - session-start: Injects compact Colab session/CU status if sessions are active.
  - pre-tool-use:  Guards against TTY-hanging `colab` commands, invalid `--gpu`
                   tiers (silent A100 fallback), and >30 MiB `colab upload`.
  - stop:          Prevents ending a turn with unleased live Colab VMs burning CUs.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

VALID_GPU_TIERS = {"T4", "L4", "G4", "A100", "H100"}
TTY_SUBCOMMANDS = {"drivemount", "auth", "pay"}
MAX_DIRECT_UPLOAD_BYTES = 30 * 1024 * 1024


def _data_dir() -> Path:
    raw = (os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("PLUGIN_DATA") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".cache" / "colab-pilot"


def _sessions_file() -> Path:
    return Path.home() / ".config" / "colab-cli" / "sessions.json"


def _resolve_colab_bin() -> str | None:
    for cand in (
        os.environ.get("COLAB_BIN", ""),
        "/Users/allen/.local/bin/colab",
        shutil.which("colab") or "",
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


def _read_json(path: Path, default: object) -> object:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _active_leases() -> dict[str, dict]:
    raw = _read_json(_data_dir() / "kept_sessions.json", {})
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    return {k: v for k, v in raw.items() if isinstance(v, dict) and float(v.get("expires_at", 0)) > now}


def _active_detached_sessions() -> set[str]:
    out: set[str] = set()
    for fname in ("detached_jobs.json", "transfer_jobs.json"):
        raw = _read_json(_data_dir() / fname, {})
        if not isinstance(raw, dict):
            continue
        for job in raw.values():
            if isinstance(job, dict) and job.get("state") in {"running", "starting"} and job.get("session"):
                out.add(str(job["session"]))
    return out


def handle_session_start(payload: dict) -> dict:
    sessions = _read_json(_sessions_file(), {})
    usage_cache = _read_json(_data_dir() / "usage_cache.json", {})
    has_local_sessions = isinstance(sessions, dict) and len(sessions) > 0
    has_cached_active = isinstance(usage_cache, dict) and int(usage_cache.get("active_assignments", 0) or 0) > 0

    if not has_local_sessions and not has_cached_active:
        return {}

    colab_bin = _resolve_colab_bin()
    usage_line = ""
    if colab_bin:
        try:
            res = subprocess.run([colab_bin, "usage"], capture_output=True, text=True, timeout=4)
            if res.returncode == 0 and res.stdout.strip():
                usage_line = " | ".join(ln.strip() for ln in res.stdout.strip().splitlines())
        except Exception:
            pass

    names = list(sessions.keys()) if isinstance(sessions, dict) else []
    msg = (
        f"Colab Pilot active: local sessions={names or 'none'}"
        + (f" ({usage_line})" if usage_line else "")
        + ". Commands: /colab-pilot, /colab-train, /colab-shard-cpu, /colab-watch."
    )
    return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}


def _split_shell_segments(cmd: str) -> list[str]:
    return [seg.strip() for seg in re.split(r"(?:&&|\|\||;|\||\n)", cmd) if seg.strip()]


def _parse_colab_invocation(segment: str) -> tuple[list[str], str] | None:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            idx += 1
            continue
        if tok in {"env", "nohup", "time", "nice"}:
            idx += 1
            continue
        break

    if idx >= len(tokens):
        return None
    exe = os.path.basename(tokens[idx])
    if exe != "colab":
        return None
    return tokens[idx + 1 :], segment


def _check_colab_args(args: list[str], raw_segment: str, full_cmd: str) -> str | None:
    # Skip global options before subcommand
    i = 0
    subcmd = ""
    while i < len(args):
        a = args[i]
        if a in {"--client-oauth-config", "-c", "--config", "--auth"}:
            i += 2
            continue
        if a.startswith("--auth=") or a.startswith("--config=") or a.startswith("-c="):
            i += 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        subcmd = a
        sub_args = args[i + 1 :]
        break
    else:
        return None

    if subcmd in TTY_SUBCOMMANDS:
        return (
            f"Blocked interactive TTY command `colab {subcmd}` which hangs headless agents. "
            "Use MCP tool `colab_mount_rclone` for headless Drive access."
        )

    if subcmd == "repl":
        # Allow only if stdin is piped or redirected
        seg_idx = full_cmd.find(raw_segment)
        preceded_by_pipe = seg_idx > 0 and "|" in full_cmd[:seg_idx]
        has_redir = "<" in raw_segment
        if not preceded_by_pipe and not has_redir:
            return (
                "Blocked bare `colab repl` which waits on an interactive TTY. "
                "Use `colab exec -s <name> -f script.py` or `colab_launch_detached`."
            )

    if subcmd == "ssh":
        if "--" not in sub_args or sub_args.index("--") == len(sub_args) - 1:
            return (
                "Blocked bare `colab ssh` without `-- '<command>'`. "
                "Pass `-- '<cmd>' </dev/null` for one-shot commands."
            )

    if subcmd in {"new", "run"}:
        for j, tok in enumerate(sub_args):
            tier = ""
            if tok == "--gpu" and j + 1 < len(sub_args):
                tier = sub_args[j + 1]
            elif tok.startswith("--gpu="):
                tier = tok.split("=", 1)[1]
            if tier and tier.upper() not in VALID_GPU_TIERS:
                return (
                    f"Blocked invalid `--gpu {tier}` (google-colab-cli silently falls back to A100). "
                    f"Valid tiers: {sorted(VALID_GPU_TIERS)} — or use `colab_allocate_cascade`."
                )

    if subcmd == "upload":
        positional = [t for t in sub_args if not t.startswith("-")]
        # Skip values of -s/--session
        filtered: list[str] = []
        skip = False
        for t in sub_args:
            if skip:
                skip = False
                continue
            if t in {"-s", "--session"}:
                skip = True
                continue
            if t.startswith("-"):
                continue
            filtered.append(t)
        if filtered:
            local_p = Path(filtered[0]).expanduser()
            if local_p.is_file() and local_p.stat().st_size > MAX_DIRECT_UPLOAD_BYTES:
                mb = local_p.stat().st_size / (1024 * 1024)
                return (
                    f"Blocked direct `colab upload` of {mb:.1f} MiB file (>30 MiB triggers Jupyter "
                    "`/api/contents` 500 errors and wastes GPU CUs). Use MCP tool `colab_smart_transfer`."
                )

    return None


def handle_pre_tool_use(payload: dict) -> dict:
    tool_call = payload.get("toolCall") or {}
    if tool_call:
        if tool_call.get("name") != "run_command":
            return {"decision": "allow"}
        cmd = str((tool_call.get("args") or {}).get("CommandLine") or "")
    else:
        if payload.get("tool_name") != "Bash":
            return {"decision": "allow"}
        cmd = str((payload.get("tool_input") or {}).get("command") or "")
    if "colab" not in cmd:
        return {"decision": "allow"}

    for seg in _split_shell_segments(cmd):
        parsed = _parse_colab_invocation(seg)
        if not parsed:
            continue
        args, raw_seg = parsed
        reason = _check_colab_args(args, raw_seg, cmd)
        if reason:
            return {"decision": "deny", "reason": reason}

    return {"decision": "allow"}


def handle_stop(payload: dict) -> dict:
    if payload.get("terminationReason") and payload.get("terminationReason") != "NO_TOOL_CALL":
        return {"decision": "stop"}
    if not payload.get("fullyIdle", True) or payload.get("continuationActive", False) or payload.get("stop_hook_active", False):
        return {"decision": "stop"}

    sessions = _read_json(_sessions_file(), {})
    if not isinstance(sessions, dict) or not sessions:
        return {"decision": "stop"}

    leases = _active_leases()
    job_sessions = _active_detached_sessions()
    unleased = [s for s in sessions.keys() if s not in leases and s not in job_sessions]
    if not unleased:
        return {"decision": "stop"}

    colab_bin = _resolve_colab_bin()
    if not colab_bin:
        return {"decision": "stop"}

    try:
        res_u = subprocess.run([colab_bin, "usage"], capture_output=True, text=True, timeout=5)
        txt = res_u.stdout
        m_cnt = re.search(r"Active assignments:\s*(\d+)", txt)
        m_rate = re.search(r"Usage rate:\s*([0-9.]+/hr)", txt)
        active_cnt = int(m_cnt.group(1)) if m_cnt else 0
        rate = m_rate.group(1) if m_rate else "unknown"
        if active_cnt > 0:
            return {
                "decision": "continue",
                "reason": (
                    f"Colab Pilot CU Guardrail: Active Colab session(s) {unleased} are still allocated "
                    f"(Active assignments: {active_cnt}, burn rate: {rate}) with no detached job or keep-warm lease. "
                    "Stop them via `colab_stop_all` / `colab stop -s <name>`, or register a lease via `colab_keep_warm`."
                ),
            }
    except Exception:
        pass

    return {"decision": "stop"}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    try:
        if event == "session-start":
            out = handle_session_start(payload)
        elif event == "pre-tool-use":
            out = handle_pre_tool_use(payload)
        elif event == "stop":
            out = handle_stop(payload)
        else:
            out = {}
    except Exception:
        out = {"decision": "allow"} if event == "pre-tool-use" else {}

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

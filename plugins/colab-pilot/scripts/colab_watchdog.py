#!/usr/bin/env python3
"""Active watchdog for Colab training/extraction jobs.

Supports:
  1. Local runner log files (--log-file)
  2. Remote detached jobs (--session + --job-id), polling
     /content/.colab_jobs/<job_id>/status.json and job.log via REST Contents API
     so WebSocket disconnects never blind monitoring.

Exits immediately on completion, error, stall, unreachable VM, or max runtime.
Optionally triggers colab_wakeup_alarm.py (--alarm-on-finish / --alarm-on-error).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

FATAL_PATTERNS: tuple[str, ...] = (
    "RuntimeError: Connection was lost.",
    "Traceback (most recent call last)",
    "CUDA out of memory",
    "OutOfMemoryError",
    "[colab] Upload failed:",
    "RuntimeError:",
)
RETRY_MARKER = "[retry] Retrying"


def _resolve_colab_bin(explicit: str) -> str:
    if explicit and os.path.exists(explicit):
        return explicit
    for cand in ("/Users/allen/.local/bin/colab", shutil.which("colab") or ""):
        if cand and os.path.exists(cand):
            return cand
    return explicit


def scan_log_state(
    text: str,
    done_marker: str,
    ignore_lines: tuple[str, ...] = ("DeprecationWarning",),
) -> tuple[str, str]:
    """Inspect log text (after the latest retry marker, if any) for done/error state."""
    if RETRY_MARKER in text:
        text = text.rsplit(RETRY_MARKER, 1)[1]

    if done_marker and done_marker in text:
        for line in reversed(text.splitlines()):
            if done_marker in line:
                return ("done", line.strip())
        return ("done", done_marker)

    for line in text.splitlines():
        if any(ig in line for ig in ignore_lines):
            continue
        for pat in FATAL_PATTERNS:
            if pat in line:
                return ("error", line.strip())

    return ("running", "")


def _run_colab_usage(colab_bin: str) -> None:
    if not colab_bin or not os.path.exists(colab_bin):
        return
    try:
        res = subprocess.run([colab_bin, "usage"], capture_output=True, text=True, timeout=15)
        if res.stdout.strip():
            print(res.stdout.strip(), flush=True)
    except Exception as exc:
        print(f"[watchdog] colab usage note: {exc}", flush=True)


def _probe_session(colab_bin: str, session: str) -> bool:
    try:
        res = subprocess.run(
            [colab_bin, "ls", "-s", session, "/content/"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return res.returncode == 0
    except Exception:
        return False


def _download_remote_text(colab_bin: str, session: str, remote_path: str) -> str | None:
    tmp_dir = tempfile.mkdtemp(prefix="colab_wd_")
    local_p = os.path.join(tmp_dir, "out.txt")
    try:
        res = subprocess.run(
            [colab_bin, "download", "-s", session, remote_path, local_p],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if res.returncode == 0 and os.path.isfile(local_p):
            return Path(local_p).read_text(encoding="utf-8", errors="replace")
        return None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _stop_session(colab_bin: str, session: str) -> None:
    if not colab_bin or not session or not os.path.exists(colab_bin):
        return
    try:
        subprocess.run([colab_bin, "stop", "-s", session], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        print(f"[watchdog] colab stop note: {exc}", flush=True)


def _trigger_alarm(phrase: str) -> None:
    alarm_script = Path(__file__).resolve().parent / "colab_wakeup_alarm.py"
    if not alarm_script.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, "-B", str(alarm_script), "--action", "start", "--daemon", "--phrase", phrase],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        print(f"[watchdog] alarm note: {exc}", flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Active watchdog for Colab jobs")
    p.add_argument("--log-file", default="", help="Path to local runner or task log file")
    p.add_argument("--session", default="", help="Colab session name (empty string disables remote probe)")
    p.add_argument("--job-id", default="", help="Detached remote job ID under /content/.colab_jobs/<job_id>/")
    p.add_argument("--colab-bin", default="/Users/allen/.local/bin/colab", help="Path to colab CLI binary")
    p.add_argument("--done-marker", default="DONE:", help="Completion substring in log")
    p.add_argument("--poll-sec", type=float, default=20.0)
    p.add_argument("--max-stall-sec", type=float, default=900.0)
    p.add_argument("--max-runtime-sec", type=float, default=3300.0)
    p.add_argument("--probe-every-sec", type=float, default=180.0)
    p.add_argument("--stop-on-timeout", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--alarm-on-finish", action="store_true")
    p.add_argument("--alarm-on-error", action="store_true")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.log_file and not (args.session and args.job_id):
        print("ERROR: Provide --log-file or both --session and --job-id", file=sys.stderr)
        return 2

    colab_bin = _resolve_colab_bin(args.colab_bin)
    start_ts = time.monotonic()
    last_change_ts = start_ts
    last_probe_ts = start_ts
    last_offset = 0
    last_mtime = 0.0
    last_remote_len = 0
    last_heartbeat = 0.0
    consec_probe_fails = 0

    print(
        f"[WATCHDOG:START] pid={os.getpid()} log={args.log_file or '<remote>'} "
        f"session={args.session or '<none>'} job_id={args.job_id or '<none>'} "
        f"poll={args.poll_sec}s max_stall={args.max_stall_sec}s max_runtime={args.max_runtime_sec}s",
        flush=True,
    )

    while True:
        now = time.monotonic()

        # 1. Local log mode
        if args.log_file and os.path.isfile(args.log_file):
            st = os.stat(args.log_file)
            if st.st_size != last_offset or st.st_mtime != last_mtime:
                last_change_ts = now
                last_mtime = st.st_mtime
            text = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
            if len(text) > last_offset:
                for ln in text[last_offset:].splitlines():
                    if ln.strip():
                        print(f"[log+] {ln}", flush=True)
                last_offset = len(text)

            state, detail = scan_log_state(text, args.done_marker)
            if state == "done":
                print(f"[WATCHDOG:DONE] {detail}", flush=True)
                _run_colab_usage(colab_bin)
                if args.alarm_on_finish:
                    _trigger_alarm("0 0 0 0 0 Colab job finished cleanly")
                return 0
            if state == "error":
                print(f"[WATCHDOG:ERROR] {detail}", flush=True)
                tail = "\n".join(text.splitlines()[-20:])
                if tail:
                    print(f"--- log tail ---\n{tail}", flush=True)
                _run_colab_usage(colab_bin)
                if args.alarm_on_error:
                    _trigger_alarm("0 0 0 0 0 Colab job hit an error")
                return 1

        # 2. Remote detached job mode
        if args.session and args.job_id:
            status_raw = _download_remote_text(colab_bin, args.session, f"/content/.colab_jobs/{args.job_id}/status.json")
            log_raw = _download_remote_text(colab_bin, args.session, f"/content/.colab_jobs/{args.job_id}/job.log")

            if status_raw is not None:
                consec_probe_fails = 0
                try:
                    st_obj = json.loads(status_raw)
                except Exception:
                    st_obj = {}
                hb = float(st_obj.get("heartbeat_ts", 0) or 0)
                if hb > last_heartbeat:
                    last_heartbeat = hb
                    last_change_ts = now
                if log_raw is not None and len(log_raw) > last_remote_len:
                    for ln in log_raw[last_remote_len:].splitlines():
                        if ln.strip():
                            print(f"[remote+] {ln}", flush=True)
                    last_remote_len = len(log_raw)
                    last_change_ts = now

                jstate = str(st_obj.get("state", "running"))
                exit_code = st_obj.get("exit_code")
                if jstate == "completed" or exit_code == 0:
                    print(f"[WATCHDOG:DONE] Remote job {args.job_id} completed (exit_code={exit_code})", flush=True)
                    _run_colab_usage(colab_bin)
                    if args.alarm_on_finish:
                        _trigger_alarm(f"0 0 0 0 0 Colab job {args.job_id} complete")
                    return 0
                if jstate == "failed" or (exit_code is not None and exit_code != 0):
                    print(f"[WATCHDOG:ERROR] Remote job {args.job_id} failed (exit_code={exit_code})", flush=True)
                    if log_raw:
                        print("--- remote log tail ---\n" + "\n".join(log_raw.splitlines()[-20:]), flush=True)
                    _run_colab_usage(colab_bin)
                    if args.alarm_on_error:
                        _trigger_alarm(f"0 0 0 0 0 Colab job {args.job_id} failed")
                    return 1
            else:
                consec_probe_fails += 1
                print(f"[watchdog] remote status unreachable ({consec_probe_fails}/3)", flush=True)
                if consec_probe_fails >= 3:
                    print(f"[WATCHDOG:VM_UNREACHABLE] Session {args.session} failed 3 consecutive checks", flush=True)
                    _run_colab_usage(colab_bin)
                    if args.alarm_on_error:
                        _trigger_alarm("0 0 0 0 0 Colab VM unreachable")
                    return 3

        # 3. Stall detection
        if (now - last_change_ts) > args.max_stall_sec:
            print(f"[WATCHDOG:STALL] No progress for {now - last_change_ts:.1f}s (limit={args.max_stall_sec}s)", flush=True)
            _run_colab_usage(colab_bin)
            if args.alarm_on_error:
                _trigger_alarm("0 0 0 0 0 Colab job stalled")
            return 2

        # 4. Periodic session liveness probe (for local log mode)
        if args.session and not args.job_id and (now - last_probe_ts) >= args.probe_every_sec:
            last_probe_ts = now
            if _probe_session(colab_bin, args.session):
                consec_probe_fails = 0
            else:
                consec_probe_fails += 1
                print(f"[watchdog] probe failed ({consec_probe_fails}/3) for session {args.session}", flush=True)
                if consec_probe_fails >= 3:
                    print(f"[WATCHDOG:VM_UNREACHABLE] Session {args.session} failed 3 consecutive probes", flush=True)
                    _run_colab_usage(colab_bin)
                    if args.alarm_on_error:
                        _trigger_alarm("0 0 0 0 0 Colab VM unreachable")
                    return 3

        # 5. Max runtime timeout
        if (now - start_ts) > args.max_runtime_sec:
            print(f"[WATCHDOG:TIMEOUT] Exceeded max runtime {args.max_runtime_sec}s", flush=True)
            if args.stop_on_timeout and args.session:
                _stop_session(colab_bin, args.session)
            _run_colab_usage(colab_bin)
            if args.alarm_on_error:
                _trigger_alarm("0 0 0 0 0 Colab watchdog timeout")
            return 4

        time.sleep(args.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())

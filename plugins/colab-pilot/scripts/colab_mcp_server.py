#!/usr/bin/env python3
"""Colab Pilot MCP Server (stdio JSON-RPC 2.0 + MCP Apps UI).

Provides 10 tools for zero-waste Google Colab orchestration and serves the
interactive `ui://colab-pilot/dashboard.html` MCP App resource.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_URI = "ui://colab-pilot/dashboard.html"
VALID_TIERS = {"A100-highmem", "A100", "G4", "L4", "H100", "T4", "NONE"}
RESOURCES_TTL_SEC = 8.0
_SEND_LOCK = threading.Lock()
_STATE_FILE_LOCK = threading.Lock()
_ACTIVE_OPS_LOCK = threading.Lock()
_ACTIVE_OPS: dict[str, dict[str, Any]] = {}
_RESOURCES_CACHE_LOCK = threading.Lock()
_RESOURCES_MEM_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_colab_bin() -> str:
    for cand in (
        os.environ.get("COLAB_BIN", ""),
        "/Users/allen/.local/bin/colab",
        shutil.which("colab") or "",
    ):
        if cand and os.path.exists(cand):
            return cand
    raise FileNotFoundError("Could not locate `colab` CLI binary")


def _sessions_file() -> Path:
    return Path.home() / ".config" / "colab-cli" / "sessions.json"


def _read_json(path: Path, default: Any) -> Any:
    with _STATE_FILE_LOCK:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default


def _write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, indent=2)
    with _STATE_FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _run_colab(args: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    colab_bin = _resolve_colab_bin()
    return subprocess.run(
        [colab_bin, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_usage(data_dir: Path) -> dict[str, Any]:
    prev = _read_json(data_dir / "usage_cache.json", None)
    txt = ""
    err_detail = ""
    cli_installed = True
    ok = False
    try:
        res = _run_colab(["usage"], timeout=12)
        txt = res.stdout or ""
        ok = res.returncode == 0
        if not ok:
            err_detail = (res.stderr or res.stdout or f"exit {res.returncode}").strip()[:200]
    except FileNotFoundError:
        cli_installed = False
        err_detail = "colab CLI not found"
    except subprocess.TimeoutExpired:
        err_detail = "colab usage timed out"

    m_bal = re.search(r"Current balance:\s*([0-9.]+)", txt)
    m_rate = re.search(r"Usage rate:\s*([0-9.]+)/hr", txt)
    m_cnt = re.search(r"Active assignments:\s*(\d+)", txt)
    if (not ok or m_bal is None) and isinstance(prev, dict) and "balance" in prev:
        stale = dict(prev)
        stale["cli_installed"] = cli_installed
        stale["stale"] = True
        if err_detail:
            stale["raw_error"] = err_detail
        return stale

    parsed = {
        "cli_installed": cli_installed,
        "balance": float(m_bal.group(1)) if m_bal else 0.0,
        "rate_hourly": float(m_rate.group(1)) if m_rate else 0.0,
        "active_assignments": int(m_cnt.group(1)) if m_cnt else 0,
        "raw": txt.strip(),
        "stale": False,
        "updated_at": time.time(),
    }
    if err_detail:
        parsed["raw_error"] = err_detail
    _write_json(data_dir / "usage_cache.json", parsed)
    return parsed


def _colab_web_url(endpoint: str) -> str:
    if not endpoint:
        return ""
    host = "https://colab.research.google.com"
    dbu = urllib.parse.quote(f"/tun/m/{endpoint}", safe="")
    return f"{host}/notebooks/empty.ipynb?dbu={dbu}#datalabBackendUrl={host}/tun/m/{endpoint}"


_SESSION_LINE_RE = re.compile(
    r"^\s*\[([^\]]+)\]\s+(\S+)\s*\|\s*Hardware:\s*([^|]+?)\s*\|\s*Shape:\s*([^|]+?)\s*\|\s*Variant:\s*([^|\n]+?)(?:\s*\|\s*Status:\s*(.+))?$"
)


def _parse_sessions_output(txt: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ln in txt.splitlines():
        m = _SESSION_LINE_RE.match(ln)
        if not m:
            continue
        out.append(
            {
                "name": m.group(1).strip(),
                "endpoint": m.group(2).strip(),
                "hardware": m.group(3).strip(),
                "shape": m.group(4).strip(),
                "variant": m.group(5).strip(),
                "status": (m.group(6) or "").strip(),
            }
        )
    return out


def _list_active_ops() -> list[dict[str, Any]]:
    now = time.time()
    with _ACTIVE_OPS_LOCK:
        items = list(_ACTIVE_OPS.values())
    return [
        {
            "tool": op["tool"],
            "session": op.get("session") or "",
            "elapsed_sec": round(max(0.0, now - float(op["started_at"])), 1),
        }
        for op in items
    ]


def _colab_python_bin() -> str:
    try:
        colab_bin = _resolve_colab_bin()
        with open(colab_bin, "r", encoding="utf-8", errors="ignore") as f:
            first = f.readline().strip()
        if first.startswith("#!"):
            interp = first[2:].strip().split()[0]
            if interp and os.path.exists(interp):
                return interp
    except Exception:
        pass
    return sys.executable


def _unassign_endpoint(endpoint: str) -> None:
    py_bin = _colab_python_bin()
    res = subprocess.run(
        [
            py_bin,
            "-B",
            "-c",
            "import sys; from colab_cli.common import state; state.client.unassign(sys.argv[1])",
            endpoint,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or f"unassign failed ({res.returncode})").strip())


def _parse_colab_resources_payload(raw_resources: str, raw_kernels: str = "") -> dict[str, Any]:
    txt = (raw_resources or "").lstrip()
    if txt.startswith(")]}'"):
        txt = txt[4:].lstrip()
    data = json.loads(txt) if txt else {}
    out: dict[str, Any] = {}

    mem = data.get("memory") if isinstance(data.get("memory"), dict) else {}
    mem_tot = int(mem.get("totalBytes") or 0)
    mem_free = int(mem.get("freeBytes") or 0)
    if mem_tot > 0:
        mem_used = max(0, mem_tot - mem_free)
        out["ram_total_gb"] = round(mem_tot / (1024**3), 1)
        out["ram_used_gb"] = round(mem_used / (1024**3), 1)
        out["ram_pct"] = round((mem_used * 100.0) / mem_tot, 1)

    gpus = data.get("gpus") if isinstance(data.get("gpus"), list) else []
    if gpus and isinstance(gpus[0], dict):
        g = gpus[0]
        raw_gu = float(g.get("gpuUtilization", 0.0) or 0.0)
        raw_mu = float(g.get("memoryUtilization", 0.0) or 0.0)
        gu_pct = raw_gu if raw_gu > 1.0 else raw_gu * 100.0
        mu_pct = raw_mu if raw_mu > 1.0 else raw_mu * 100.0
        v_used = int(g.get("memoryUsedBytes") or 0)
        v_tot = int(g.get("memoryTotalBytes") or 0)
        out["gpu_name"] = g.get("name")
        out["gpu_util_pct"] = round(min(100.0, max(0.0, gu_pct)), 1)
        out["gpu_mem_util_pct"] = round(min(100.0, max(0.0, mu_pct)), 1)
        if v_tot > 0:
            out["vram_total_gb"] = round(v_tot / (1024**3), 1)
            out["vram_used_gb"] = round(max(0, v_used) / (1024**3), 1)
            out["vram_pct"] = round((max(0, v_used) * 100.0) / v_tot, 1)
        out["gpu_ever_used"] = bool(g.get("everUsed", False))

    disks = data.get("disks") if isinstance(data.get("disks"), list) else []
    chosen_disk = None
    for d in disks:
        if isinstance(d, dict) and isinstance(d.get("filesystem"), dict):
            if d["filesystem"].get("label") == "kernel":
                chosen_disk = d["filesystem"]
                break
    if chosen_disk is None and disks and isinstance(disks[0], dict) and isinstance(disks[0].get("filesystem"), dict):
        chosen_disk = disks[0]["filesystem"]
    if chosen_disk:
        d_tot = int(chosen_disk.get("totalBytes") or 0)
        d_used = int(chosen_disk.get("usedBytes") or 0)
        if d_tot > 0:
            out["disk_total_gb"] = round(d_tot / (1024**3), 1)
            out["disk_used_gb"] = round(max(0, d_used) / (1024**3), 1)
            out["disk_pct"] = round((max(0, d_used) * 100.0) / d_tot, 1)

    if raw_kernels:
        try:
            klist = json.loads(raw_kernels)
            if isinstance(klist, list):
                out["kernel_count"] = len(klist)
                if klist:
                    is_busy = any(isinstance(k, dict) and k.get("execution_state") == "busy" for k in klist)
                    out["kernel_state"] = "busy" if is_busy else "idle"
        except Exception:
            pass

    return out


def _http_get_runtime_proxy(proxy_url: str, token: str, subpath: str, timeout: float = 2.2) -> str:
    req = urllib.request.Request(
        f"{proxy_url.rstrip('/')}{subpath}",
        headers={
            "X-Colab-Runtime-Proxy-Token": token,
            "X-Colab-Client-Agent": "colab-cli",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_single_vm_http_resources(endpoint: str, proxy_url: str, token: str) -> dict[str, Any] | None:
    if not endpoint or not proxy_url or not token:
        return None
    now = time.monotonic()
    with _RESOURCES_CACHE_LOCK:
        cached = _RESOURCES_MEM_CACHE.get(endpoint)
        if cached and (now - cached[0]) < RESOURCES_TTL_SEC:
            return dict(cached[1])
    try:
        raw_res = _http_get_runtime_proxy(proxy_url, token, "/api/colab/resources", timeout=2.2)
    except Exception:
        return None
    raw_kernels = ""
    try:
        raw_kernels = _http_get_runtime_proxy(proxy_url, token, "/api/kernels", timeout=1.5)
    except Exception:
        pass
    try:
        parsed = _parse_colab_resources_payload(raw_res, raw_kernels)
    except Exception:
        return None
    with _RESOURCES_CACHE_LOCK:
        _RESOURCES_MEM_CACHE[endpoint] = (time.monotonic(), dict(parsed))
    return parsed


def _token_looks_valid(expires_at_iso: Any) -> bool:
    if not expires_at_iso:
        return True
    try:
        dt = datetime.fromisoformat(str(expires_at_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() > 120.0
    except Exception:
        return True


def _fetch_proxy_tokens_fallback(data_dir: Path) -> dict[str, dict[str, str]]:
    cache_path = data_dir / "proxy_tokens_cache.json"
    cached = _read_json(cache_path, {})
    now = time.time()
    if isinstance(cached, dict) and (now - float(cached.get("updated_at") or 0)) < 300.0:
        tokens = cached.get("tokens")
        if isinstance(tokens, dict):
            return tokens
    try:
        colab_bin = Path(_resolve_colab_bin())
        if "colab_cli" not in colab_bin.read_text(encoding="utf-8", errors="ignore"):
            return {}
        py_bin = _colab_python_bin()
        res = subprocess.run(
            [
                py_bin,
                "-B",
                "-c",
                "import json; from colab_cli.common import state; "
                "print(json.dumps({a.endpoint: {'url': a.runtime_proxy_info.url, 'token': a.runtime_proxy_info.token} "
                "for a in state.client.list_assignments() if a.runtime_proxy_info}))",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3.5,
        )
        if res.returncode == 0 and res.stdout.strip():
            tokens = json.loads(res.stdout.strip())
            if isinstance(tokens, dict):
                _write_json(cache_path, {"updated_at": now, "tokens": tokens})
                return tokens
    except Exception:
        pass
    return {}


def _record_vm_cpu_telemetry(data_dir: Path, session: str, payload: dict[str, Any]) -> None:
    if not session or payload.get("cpu_pct") is None:
        return
    path = data_dir / "vm_telemetry.json"
    cur = _read_json(path, {})
    if not isinstance(cur, dict):
        cur = {}
    cur[session] = {
        "cpu_pct": payload.get("cpu_pct"),
        "cpu_count": payload.get("cpu_count"),
        "load_1m": payload.get("load_1m"),
        "sampled_at": float(payload.get("sampled_at") or time.time()),
        "source": str(payload.get("source") or "probe"),
    }
    _write_json(path, cur)


def _get_leases(data_dir: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json(data_dir / "kept_sessions.json", {})
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    active = {k: v for k, v in raw.items() if isinstance(v, dict) and float(v.get("expires_at", 0)) > now}
    if len(active) != len(raw):
        _write_json(data_dir / "kept_sessions.json", active)
    return active


def _find_rclone_conf(explicit: str = "") -> Path | None:
    cands = [
        explicit,
        str(Path.home() / ".config" / "rclone" / "colab_gdrive_root.conf"),
        str(Path.home() / ".config" / "rclone" / "rclone.conf"),
    ]
    for c in cands:
        if c:
            p = Path(c).expanduser()
            if p.is_file():
                return p
    return None


def _detect_rclone_remote(conf_path: Path) -> str:
    txt = conf_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\[([^\]]+)\]", txt, flags=re.MULTILINE)
    return m.group(1).strip() if m else "gdrive_root"


def _get_keepalive_status(data_dir: Path) -> dict[str, Any]:
    st = _read_json(data_dir / "keepalive_daemon.json", {})
    if not isinstance(st, dict):
        return {"running": False}
    pid = int(st.get("pid") or 0)
    alive = False
    if pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    if not alive:
        return {"running": False, "pid": None, "last_tick_at": st.get("last_tick_at")}
    out = dict(st)
    out["running"] = True
    return out


def _ensure_keepalive_daemon(data_dir: Path) -> dict[str, Any]:
    st = _get_keepalive_status(data_dir)
    if st.get("running") and st.get("pid"):
        return st
    interval = float(os.environ.get("COLAB_KEEPALIVE_INTERVAL_SEC", "150") or 150)
    log_path = data_dir / "keepalive_daemon.log"
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                os.path.abspath(__file__),
                "--data-dir",
                str(data_dir),
                "--keepalive-daemon",
                "--interval",
                str(interval),
            ],
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=lf,
            start_new_session=True,
        )
    rec = {
        "pid": proc.pid,
        "started_at": time.time(),
        "last_tick_at": None,
        "interval_sec": interval,
        "pings": {},
    }
    _write_json(data_dir / "keepalive_daemon.json", rec)
    rec["running"] = True
    return rec


def _run_keepalive_tick(data_dir: Path) -> bool:
    leases = _get_leases(data_dir)
    jobs = _read_json(data_dir / "detached_jobs.json", {})
    if not isinstance(jobs, dict):
        jobs = {}

    jobs_dirty = False
    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        if job.get("state") == "running" and job.get("session"):
            sess = str(job["session"])
            tmp_dir = Path(tempfile.mkdtemp(prefix="colab_ka_"))
            try:
                st_local = tmp_dir / "status.json"
                r_st = _run_colab(
                    ["download", "-s", sess, f"/content/.colab_jobs/{job_id}/status.json", str(st_local)],
                    timeout=20,
                )
                if r_st.returncode == 0 and st_local.is_file():
                    st_obj = json.loads(st_local.read_text(encoding="utf-8"))
                    job.update(
                        {
                            "state": st_obj.get("state", "running"),
                            "gpu_util": st_obj.get("gpu_util"),
                            "vram_used": st_obj.get("vram_used"),
                            "cpu_pct": st_obj.get("cpu_pct"),
                            "cpu_count": st_obj.get("cpu_count"),
                            "load_1m": st_obj.get("load_1m"),
                            "exit_code": st_obj.get("exit_code"),
                            "last_drive_sync_ts": st_obj.get("last_drive_sync_ts"),
                            "synced_file_count": st_obj.get("synced_file_count"),
                        }
                    )
                    if st_obj.get("cpu_pct") is not None:
                        _record_vm_cpu_telemetry(
                            data_dir,
                            sess,
                            {
                                "cpu_pct": st_obj.get("cpu_pct"),
                                "cpu_count": st_obj.get("cpu_count"),
                                "load_1m": st_obj.get("load_1m"),
                                "sampled_at": st_obj.get("heartbeat_ts") or time.time(),
                                "source": "detached_job",
                            },
                        )
                    jobs_dirty = True
            except Exception:
                pass
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # Auto-stop reaper for completed/failed detached jobs with auto_stop_vm=True
    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict):
            continue
        if job.get("state") in {"completed", "failed"} and job.get("auto_stop_vm") and not job.get("vm_auto_stopped"):
            sess = str(job.get("session") or "")
            if not sess:
                continue
            still_running = any(
                isinstance(o, dict) and o.get("session") == sess and o.get("state") == "running"
                for o in jobs.values()
            )
            if not still_running and sess not in leases:
                try:
                    r_stop = _run_colab(["stop", "-s", sess], timeout=45)
                    if r_stop.returncode == 0:
                        job["vm_auto_stopped"] = True
                        job["vm_auto_stopped_at"] = time.time()
                        jobs_dirty = True
                except Exception:
                    pass

    if jobs_dirty:
        _write_json(data_dir / "detached_jobs.json", jobs)

    keepalive_sessions = set(leases.keys()) | {
        str(j["session"])
        for j in jobs.values()
        if isinstance(j, dict) and j.get("state") == "running" and j.get("session")
    }
    state_path = data_dir / "keepalive_daemon.json"
    if not keepalive_sessions:
        try:
            if state_path.exists():
                state_path.unlink()
        except OSError:
            pass
        return False

    cur = _read_json(state_path, {})
    if not isinstance(cur, dict):
        cur = {}
    pings = cur.get("pings") if isinstance(cur.get("pings"), dict) else {}
    local_sessions = _read_json(_sessions_file(), {})
    if not isinstance(local_sessions, dict):
        local_sessions = {}

    for sess in sorted(keepalive_sessions):
        try:
            _run_colab(["ls", "-s", sess, "content"], timeout=20)
        except Exception:
            pass
        sess_meta = local_sessions.get(sess) if isinstance(local_sessions.get(sess), dict) else {}
        is_busy = bool(sess_meta.get("running"))
        if not is_busy and sess_meta.get("url") and sess_meta.get("token"):
            try:
                raw_k = _http_get_runtime_proxy(str(sess_meta["url"]), str(sess_meta["token"]), "/api/kernels", timeout=1.5)
                klist = json.loads(raw_k)
                if isinstance(klist, list) and any(isinstance(k, dict) and k.get("execution_state") == "busy" for k in klist):
                    is_busy = True
            except Exception:
                pass
        if not is_busy:
            cpu_probe = (
                "import json, os, time\n"
                "def _rd():\n"
                "    p = [int(x) for x in open('/proc/stat').readline().split()[1:]]\n"
                "    return sum(p), p[3] + (p[4] if len(p) > 4 else 0)\n"
                "t0, i0 = _rd(); time.sleep(0.2); t1, i1 = _rd()\n"
                "print('KA_CPU:' + json.dumps({'cpu_pct': round(100.0 * (1.0 - (i1 - i0) / max(1, t1 - t0)), 1), "
                "'cpu_count': os.cpu_count(), 'load_1m': round(os.getloadavg()[0], 2)}))\n"
            )
            try:
                ex_res = subprocess.run(
                    [_resolve_colab_bin(), "exec", "-s", sess],
                    input=cpu_probe,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if ex_res.returncode == 0 and ex_res.stdout:
                    for ln in ex_res.stdout.splitlines():
                        if ln.startswith("KA_CPU:"):
                            cpu_obj = json.loads(ln.split("KA_CPU:", 1)[1])
                            cpu_obj["sampled_at"] = time.time()
                            cpu_obj["source"] = "keepalive"
                            _record_vm_cpu_telemetry(data_dir, sess, cpu_obj)
                            break
            except Exception:
                pass
        prev_p = pings.get(sess) if isinstance(pings.get(sess), dict) else {}
        pings[sess] = {
            "ping_count": int(prev_p.get("ping_count", 0)) + 1,
            "last_ping_at": time.time(),
        }

    cur.update(
        {
            "pid": os.getpid(),
            "started_at": cur.get("started_at") or time.time(),
            "last_tick_at": time.time(),
            "pings": pings,
        }
    )
    _write_json(state_path, cur)
    return True


def _run_keepalive_daemon(data_dir: Path, interval: float) -> int:
    interval = max(0.1, float(interval))
    initial_wait = min(interval, 0.25 if interval < 5.0 else 1.5)
    time.sleep(initial_wait)
    while True:
        try:
            keep_going = _run_keepalive_tick(data_dir)
            if not keep_going:
                return 0
        except Exception:
            pass
        time.sleep(interval)


def _run_transfer_worker(data_dir: Path, spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    transfer_id = str(spec["transfer_id"])
    local_path = str(spec["local_path"])
    drive_cache = str(spec["drive_cache"])
    drive_target = str(spec["drive_target"])
    conf_path = Path(str(spec["conf_path"]))
    session = str(spec.get("session") or "")
    remote_path = str(spec.get("remote_path") or "")
    size_bytes = int(spec.get("bytes", 0))
    log_file = Path(str(spec["log_file"]))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    def _update(patch: dict[str, Any]) -> None:
        jobs = _read_json(data_dir / "transfer_jobs.json", {})
        if not isinstance(jobs, dict):
            jobs = {}
        rec = jobs.get(transfer_id) if isinstance(jobs.get(transfer_id), dict) else {}
        rec.update(patch)
        jobs[transfer_id] = rec
        _write_json(data_dir / "transfer_jobs.json", jobs)

    try:
        cmd = [
            "rclone",
            "copy",
            local_path,
            drive_cache,
            "--config",
            str(conf_path),
            "--drive-chunk-size",
            "128M",
            "--transfers",
            "4",
            "--stats",
            "2s",
            "--use-json-log",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with open(log_file, "a", encoding="utf-8", buffering=1) as lf:
            assert proc.stdout is not None
            for line in proc.stdout:
                lf.write(line)
                try:
                    obj = json.loads(line)
                    stats = obj.get("stats") if isinstance(obj, dict) else None
                    if isinstance(stats, dict):
                        transferred = int(stats.get("bytes", 0) or 0)
                        total = int(stats.get("totalBytes", size_bytes) or size_bytes or 0)
                        pct = round((transferred * 100.0 / total), 1) if total > 0 else None
                        _update(
                            {
                                "bytes_transferred": transferred,
                                "percent": pct,
                                "speed_bps": stats.get("speed"),
                                "eta_sec": stats.get("eta"),
                                "updated_at": time.time(),
                            }
                        )
                except Exception:
                    pass
        rc = proc.wait()
        if rc != 0:
            _update({"state": "failed", "error": f"rclone copy exited with code {rc}", "finished_at": time.time()})
            return 1

        pulled_to_vm = False
        if session:
            _update({"phase": "vm_pull", "updated_at": time.time()})
            conf_b64 = base64.b64encode(conf_path.read_bytes()).decode("ascii")
            pull_py = (
                "import base64, os, shutil, subprocess\n"
                "from pathlib import Path\n"
                "rc = Path('/root/.config/rclone/rclone.conf')\n"
                "rc.parent.mkdir(parents=True, exist_ok=True)\n"
                "rc.write_bytes(base64.b64decode(os.environ['RCLONE_CONF_B64']))\n"
                "os.chmod(rc, 0o600)\n"
                "if not shutil.which('rclone'):\n"
                "    subprocess.run(['bash', '-c', 'apt-get update -qq && apt-get install -y -qq rclone >/dev/null'], check=True)\n"
                "dst = os.environ['DST_PATH']\n"
                "os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
                "subprocess.run(['rclone', 'copyto', os.environ['SRC_DRIVE'], dst, '--config', str(rc)], check=True)\n"
                "print('PULLED_BYTES:' + str(os.path.getsize(dst)))\n"
            )
            p_res = subprocess.run(
                [
                    _resolve_colab_bin(),
                    "exec",
                    "-s",
                    session,
                    "--env",
                    f"RCLONE_CONF_B64={conf_b64}",
                    "--env",
                    f"SRC_DRIVE={drive_target}",
                    "--env",
                    f"DST_PATH={remote_path}",
                ],
                input=pull_py,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write((p_res.stdout or "") + (p_res.stderr or ""))
            if p_res.returncode != 0:
                _update(
                    {
                        "state": "failed",
                        "error": f"VM rclone pull failed: {(p_res.stderr or p_res.stdout).strip()[:200]}",
                        "finished_at": time.time(),
                    }
                )
                return 1
            pulled_to_vm = True

        _update(
            {
                "state": "completed",
                "phase": "done",
                "percent": 100.0,
                "bytes_transferred": size_bytes,
                "pulled_to_vm": pulled_to_vm,
                "remote_path": remote_path if pulled_to_vm else None,
                "finished_at": time.time(),
            }
        )
        return 0
    except Exception as exc:
        _update({"state": "failed", "error": str(exc)[:300], "finished_at": time.time()})
        return 1
    finally:
        try:
            spec_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_env_secret(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    for env_p in (Path.cwd() / ".env", Path("/Users/allen/git/lg_g5_calibration/.env")):
        if env_p.is_file():
            for line in env_p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return ""


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def _fetch_sessions_safe() -> list[dict[str, str]]:
    try:
        res = _run_colab(["sessions"], timeout=12)
        if res.returncode == 0:
            return _parse_sessions_output(res.stdout or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


def _fetch_alarm_status() -> dict[str, Any]:
    alarm_script = _plugin_root() / "scripts" / "colab_wakeup_alarm.py"
    if alarm_script.is_file():
        try:
            ares = subprocess.run(
                [sys.executable, "-B", str(alarm_script), "--action", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ares.returncode == 0 and ares.stdout.strip():
                return json.loads(ares.stdout)
        except Exception:
            pass
    return {"running": False}


def tool_colab_dashboard(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    ultra_monthly_cu = float(args.get("ultra_monthly_cu", 25000.0) or 25000.0)
    budget_fraction = float(args.get("budget_fraction", 0.20) or 0.20)

    local_sessions = _read_json(_sessions_file(), {})
    if not isinstance(local_sessions, dict):
        local_sessions = {}

    http_futures: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_usage = pool.submit(_parse_usage, data_dir)
        f_sessions = pool.submit(_fetch_sessions_safe)
        f_alarm = pool.submit(_fetch_alarm_status)
        for name, meta in local_sessions.items():
            if not isinstance(meta, dict):
                continue
            ep = str(meta.get("endpoint") or "")
            url = str(meta.get("url") or "")
            tok = str(meta.get("token") or "")
            if ep and url and tok and _token_looks_valid(meta.get("token_expires_at")):
                http_futures[ep] = pool.submit(_fetch_single_vm_http_resources, ep, url, tok)
        usage = f_usage.result()
        server_sessions = f_sessions.result()
        alarm_status = f_alarm.result()
        http_resources_by_ep: dict[str, dict[str, Any] | None] = {
            ep: fut.result() for ep, fut in http_futures.items()
        }

    server_by_name = {s["name"]: s for s in server_sessions if s["name"] != "?"}
    leases = _get_leases(data_dir)
    now = time.time()
    session_list: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()

    for name, meta in local_sessions.items():
        if not isinstance(meta, dict):
            continue
        srv = server_by_name.get(name, {})
        endpoint = str(meta.get("endpoint") or srv.get("endpoint") or "")
        if endpoint:
            seen_endpoints.add(endpoint)
        lease = leases.get(name)
        lease_min = ((float(lease["expires_at"]) - now) / 60.0) if lease else 0.0
        running_job = meta.get("running")
        status = f"busy ({running_job})" if running_job else "active"
        session_list.append(
            {
                "name": name,
                "display_name": name,
                "endpoint": endpoint,
                "untracked": False,
                "accelerator": meta.get("accelerator") or srv.get("hardware") or "NONE",
                "machine_shape": meta.get("machine_shape") or srv.get("shape") or "STANDARD",
                "status": status,
                "lease_expires_in_min": round(lease_min, 1) if lease_min > 0 else None,
                "web_url": _colab_web_url(endpoint),
            }
        )

    for srv in server_sessions:
        ep = srv["endpoint"]
        if ep in seen_endpoints:
            continue
        seen_endpoints.add(ep)
        is_orphan = srv["name"] == "?"
        sess_key = f"untracked:{ep}" if is_orphan else srv["name"]
        display_name = f"Untracked ({ep[:16]}…)" if is_orphan else srv["name"]
        session_list.append(
            {
                "name": sess_key,
                "display_name": display_name,
                "endpoint": ep,
                "untracked": is_orphan,
                "accelerator": srv["hardware"] or "NONE",
                "machine_shape": srv["shape"] or "STANDARD",
                "status": "orphaned (server)" if is_orphan else (srv["status"] or "active"),
                "lease_expires_in_min": None,
                "web_url": _colab_web_url(ep),
            }
        )

    missing_eps = [s["endpoint"] for s in session_list if s.get("endpoint") and not http_resources_by_ep.get(s["endpoint"])]
    if missing_eps:
        fallback_tokens = _fetch_proxy_tokens_fallback(data_dir)
        fb_targets = [(ep, fallback_tokens[ep]) for ep in missing_eps if ep in fallback_tokens]
        if fb_targets:
            with ThreadPoolExecutor(max_workers=min(4, len(fb_targets))) as fb_pool:
                fb_futs = {
                    ep: fb_pool.submit(_fetch_single_vm_http_resources, ep, info.get("url", ""), info.get("token", ""))
                    for ep, info in fb_targets
                }
                for ep, fut in fb_futs.items():
                    res_fb = fut.result()
                    if res_fb:
                        http_resources_by_ep[ep] = res_fb

    jobs_raw = _read_json(data_dir / "detached_jobs.json", {})
    job_list = list(jobs_raw.values()) if isinstance(jobs_raw, dict) else []
    job_list.sort(key=lambda j: float(j.get("started_at", 0) or 0), reverse=True)
    vm_telemetry = _read_json(data_dir / "vm_telemetry.json", {})
    if not isinstance(vm_telemetry, dict):
        vm_telemetry = {}

    for s in session_list:
        telem: dict[str, Any] = {}
        ep = s.get("endpoint") or ""
        if ep and isinstance(http_resources_by_ep.get(ep), dict):
            telem.update(http_resources_by_ep[ep])  # type: ignore[arg-type]
        cpu_rec = vm_telemetry.get(s["name"]) if isinstance(vm_telemetry.get(s["name"]), dict) else None
        if cpu_rec and (now - float(cpu_rec.get("sampled_at") or 0)) <= 600.0:
            for k in ("cpu_pct", "cpu_count", "load_1m", "sampled_at", "source"):
                if cpu_rec.get(k) is not None:
                    telem[k] = cpu_rec[k]
        else:
            for j in job_list:
                if (
                    isinstance(j, dict)
                    and j.get("session") == s["name"]
                    and j.get("state") == "running"
                    and j.get("cpu_pct") is not None
                ):
                    telem["cpu_pct"] = j.get("cpu_pct")
                    if j.get("cpu_count") is not None:
                        telem["cpu_count"] = j.get("cpu_count")
                    if j.get("load_1m") is not None:
                        telem["load_1m"] = j.get("load_1m")
                    break
        s["telemetry"] = telem or None

    transfers_raw = _read_json(data_dir / "transfer_jobs.json", {})
    transfer_list = list(transfers_raw.values()) if isinstance(transfers_raw, dict) else []
    transfer_list.sort(key=lambda j: float(j.get("started_at", 0) or 0), reverse=True)
    keepalive_status = _get_keepalive_status(data_dir)
    active_ops = _list_active_ops()

    structured = {
        "usage": usage,
        "ultra_monthly_cu": ultra_monthly_cu,
        "budget_fraction": budget_fraction,
        "sessions": session_list,
        "jobs": job_list[:10],
        "transfers": transfer_list[:10],
        "keepalive": keepalive_status,
        "alarm": alarm_status,
        "active_operations": active_ops,
    }
    summary = (
        f"Colab Pilot — Balance: {usage['balance']:.2f} CU | "
        f"Rate: {usage['rate_hourly']:.2f}/hr | "
        f"Active assignments: {usage['active_assignments']} | "
        f"Tracked sessions: {len(session_list)} | Tracked jobs: {len(job_list)}"
    )
    if active_ops:
        summary += f" | In-flight ops: {len(active_ops)}"
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": structured,
    }


MAX_CASCADE_BUDGET_SEC = 145.0
DEFAULT_CASCADE_TIER_TIMEOUT_SEC = 28.0
DEFAULT_CASCADE_PROBE_TIMEOUT_SEC = 25.0


def _is_unsupported_accelerator_error(detail: str) -> bool:
    low = detail.lower()
    return (
        "backend rejected accelerator" in low
        or "not entitled" in low
        or "unsupported value" in low
        or "http 400" in low
    )


def tool_colab_allocate_cascade(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    tiers = args.get("tiers") or ["A100-highmem", "A100", "G4", "L4"]
    require_bf16 = bool(args.get("require_bf16", True))
    keep_warm_minutes = float(args.get("keep_warm_minutes", 0) or 0)
    max_wall = min(
        MAX_CASCADE_BUDGET_SEC,
        max(5.0, float(args.get("max_wall_seconds", 140.0) or 140.0)),
    )
    per_tier_cap = float(os.environ.get("COLAB_CASCADE_TIER_TIMEOUT_SEC", str(DEFAULT_CASCADE_TIER_TIMEOUT_SEC)))
    probe_cap = float(os.environ.get("COLAB_CASCADE_PROBE_TIMEOUT_SEC", str(DEFAULT_CASCADE_PROBE_TIMEOUT_SEC)))

    for t in tiers:
        if t not in VALID_TIERS:
            raise ValueError(f"Invalid tier {t!r}; valid: {sorted(VALID_TIERS)}")

    start_t = time.monotonic()
    deadline = start_t + max_wall
    attempts: list[dict[str, Any]] = []
    assigned_tier = None
    had_timeout = False

    for tier in tiers:
        rem = deadline - time.monotonic()
        if rem <= 4.0:
            attempts.append({"tier": tier, "ok": False, "skipped": True, "detail": "Cascade wall-clock budget exhausted"})
            break

        local_sessions = _read_json(_sessions_file(), {})
        if (isinstance(local_sessions, dict) and session in local_sessions) or had_timeout:
            if deadline - time.monotonic() > 2.0:
                try:
                    _run_colab(["stop", "-s", session], timeout=min(8.0, deadline - time.monotonic() - 1.0))
                except Exception:
                    pass

        rem = deadline - time.monotonic()
        if rem <= 2.0:
            attempts.append({"tier": tier, "ok": False, "skipped": True, "detail": "Cascade wall-clock budget exhausted"})
            break

        cmd = ["new", "-s", session]
        if tier == "A100-highmem":
            cmd.extend(["--gpu", "A100", "--high-mem"])
        elif tier != "NONE":
            cmd.extend(["--gpu", tier])

        step_timeout = max(1.0, min(per_tier_cap, rem))
        try:
            res = _run_colab(cmd, timeout=step_timeout)
        except subprocess.TimeoutExpired:
            had_timeout = True
            attempts.append({"tier": tier, "ok": False, "timed_out": True, "detail": f"Timed out after {step_timeout:.1f}s"})
            continue

        if res.returncode == 0:
            assigned_tier = tier
            attempts.append({"tier": tier, "ok": True})
            break

        detail = (res.stderr or res.stdout).strip()[:200]
        rejected = _is_unsupported_accelerator_error(detail)
        attempts.append({"tier": tier, "ok": False, "rejected": rejected, "detail": detail})
        if not rejected:
            sleep_sec = min(1.5, max(0.0, deadline - time.monotonic() - 1.0))
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    if not assigned_tier:
        raise RuntimeError(f"All tiers failed within {round(time.monotonic() - start_t, 1)}s: {json.dumps(attempts)}")

    probe_code = (
        "import json, os, shutil, subprocess\n"
        "info = {'cpu_count': os.cpu_count(), 'gpu_name': None, 'vram_gb': 0.0, 'bf16_supported': False, 'sys_ram_gb': 0.0, 'hevc_nvenc': False}\n"
        "try:\n"
        "    with open('/proc/meminfo') as f:\n"
        "        for ln in f:\n"
        "            if ln.startswith('MemTotal:'):\n"
        "                info['sys_ram_gb'] = round(int(ln.split()[1]) / (1024*1024), 2)\n"
        "except Exception: pass\n"
        "try:\n"
        "    import torch\n"
        "    if torch.cuda.is_available():\n"
        "        p = torch.cuda.get_device_properties(0)\n"
        "        info['gpu_name'] = p.name\n"
        "        info['vram_gb'] = round(p.total_memory / (1024**3), 2)\n"
        "        info['bf16_supported'] = bool(torch.cuda.is_bf16_supported())\n"
        "except Exception as e:\n"
        "    info['torch_err'] = str(e)\n"
        "if shutil.which('ffmpeg'):\n"
        "    r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True)\n"
        "    info['hevc_nvenc'] = 'hevc_nvenc' in r.stdout\n"
        "print('PROBE_JSON:' + json.dumps(info))\n"
    )
    probe_timeout = max(5.0, min(probe_cap, deadline - time.monotonic()))
    p_res = subprocess.run(
        [_resolve_colab_bin(), "exec", "-s", session],
        input=probe_code,
        capture_output=True,
        text=True,
        timeout=probe_timeout,
    )
    probe: dict[str, Any] = {}
    for ln in p_res.stdout.splitlines():
        if ln.startswith("PROBE_JSON:"):
            probe = json.loads(ln.split("PROBE_JSON:", 1)[1])
            break

    if require_bf16 and assigned_tier != "NONE" and not probe.get("bf16_supported", False):
        _run_colab(["stop", "-s", session], timeout=30)
        raise RuntimeError(f"Assigned {assigned_tier} ({probe.get('gpu_name')}) does not support BF16; stopped VM.")

    if keep_warm_minutes > 0:
        tool_colab_keep_warm(data_dir, {"session": session, "minutes": keep_warm_minutes, "reason": "Cascade allocation"})

    vram = float(probe.get("vram_gb", 0) or 0)
    guidance = {
        "dataloader_num_workers": 0,
        "recommended_precision": "bfloat16" if probe.get("bf16_supported") else "float16",
        "suggested_crop": 768 if vram >= 75 else (640 if vram >= 38 else 512),
        "suggested_batch": 12 if vram >= 75 else (6 if vram >= 38 else 2),
    }
    out = {
        "session": session,
        "assigned_tier": assigned_tier,
        "attempts": attempts,
        "hardware": probe,
        "guidance": guidance,
    }
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


def tool_colab_mount_rclone(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    mode = str(args.get("mode", "random_seek"))
    mount_point = str(args.get("mount_point", "/content/drive"))
    conf_path = _find_rclone_conf(str(args.get("rclone_conf", "")))
    if not conf_path:
        raise FileNotFoundError("No rclone.conf found at ~/.config/rclone/colab_gdrive_root.conf or ~/.config/rclone/rclone.conf")

    remote_name = str(args.get("remote_name") or _detect_rclone_remote(conf_path))
    conf_b64 = base64.b64encode(conf_path.read_bytes()).decode("ascii")

    forward_keys = args.get("forward_env_keys") or ["HF_TOKEN"]
    env_flags: list[str] = ["--env", f"RCLONE_CONF_B64={conf_b64}", "--env", f"RCLONE_REMOTE={remote_name}", "--env", f"MOUNT_POINT={mount_point}", "--env", f"MOUNT_MODE={mode}"]
    forwarded: list[str] = []
    for k in forward_keys:
        val = _read_env_secret(str(k))
        if val:
            env_flags.extend(["--env", f"FWD_{k}={val}"])
            forwarded.append(str(k))

    setup_py = (
        "import base64, os, shutil, subprocess, time\n"
        "from pathlib import Path\n"
        "rc_dir = Path('/root/.config/rclone')\n"
        "rc_dir.mkdir(parents=True, exist_ok=True)\n"
        "rc_path = rc_dir / 'rclone.conf'\n"
        "rc_path.write_bytes(base64.b64decode(os.environ['RCLONE_CONF_B64']))\n"
        "os.chmod(rc_path, 0o600)\n"
        "for k, v in os.environ.items():\n"
        "    if k.startswith('FWD_'):\n"
        "        os.environ[k[4:]] = v\n"
        "if not shutil.which('rclone'):\n"
        "    subprocess.run(['bash', '-c', 'apt-get update -qq && apt-get install -y -qq fuse3 rclone >/dev/null'], check=True)\n"
        "mode = os.environ.get('MOUNT_MODE', 'random_seek')\n"
        "mp = os.environ.get('MOUNT_POINT', '/content/drive')\n"
        "remote = os.environ.get('RCLONE_REMOTE', 'gdrive_root')\n"
        "if mode != 'config_only':\n"
        "    os.makedirs(mp, exist_ok=True)\n"
        "    subprocess.run(['fusermount', '-u', mp], capture_output=True)\n"
        "    cmd = ['rclone', 'mount', f'{remote}:', mp, '--read-only', '--daemon', '--allow-other']\n"
        "    if mode == 'streaming':\n"
        "        cmd += ['--vfs-cache-mode', 'full', '--vfs-read-chunk-size', '256M', '--vfs-read-ahead', '256M', '--buffer-size', '256M', '--vfs-cache-max-size', '16G']\n"
        "    else:\n"
        "        cmd += ['--vfs-cache-mode', 'off', '--vfs-read-chunk-size', '8M', '--vfs-read-chunk-size-limit', '256M', '--buffer-size', '16M']\n"
        "    subprocess.run(cmd, check=True)\n"
        "    time.sleep(2)\n"
        "    ls = subprocess.run(['ls', mp], capture_output=True, text=True, check=True)\n"
        "    print('MOUNT_OK:' + str(len(ls.stdout.splitlines())) + ' entries')\n"
        "else:\n"
        "    ls = subprocess.run(['rclone', 'lsd', f'{remote}:', '--max-depth', '1'], capture_output=True, text=True, check=True)\n"
        "    print('RCLONE_OK:' + str(len(ls.stdout.splitlines())) + ' roots')\n"
    )
    res = subprocess.run(
        [_resolve_colab_bin(), "exec", "-s", session, *env_flags],
        input=setup_py,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if res.returncode != 0:
        raise RuntimeError(f"rclone mount failed: {(res.stderr or res.stdout).strip()}")

    out = {
        "session": session,
        "mode": mode,
        "remote": remote_name,
        "mount_point": mount_point if mode != "config_only" else None,
        "forwarded_env_keys": forwarded,
        "output": res.stdout.strip(),
    }
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def tool_colab_smart_transfer(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    direction = str(args["direction"])
    local_path = Path(str(args["local_path"])).expanduser()
    remote_path = str(args["remote_path"])
    session = str(args.get("session") or "")
    mode = str(args.get("mode", "auto"))
    drive_cache = str(args.get("drive_cache_path", "gdrive_root:Colab Notebooks/colab_pilot_cache"))
    conf_path = _find_rclone_conf()

    if direction == "upload":
        if not local_path.is_file():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        size_bytes = local_path.stat().st_size
        chosen = mode
        if chosen == "auto":
            if size_bytes <= 24 * 1024 * 1024 and session:
                chosen = "direct"
            elif conf_path and not session:
                chosen = "drive_stage"
            elif conf_path:
                chosen = "drive_stage"
            else:
                chosen = "chunked"

        if chosen == "direct":
            if not session:
                raise ValueError("`session` is required for direct upload")
            res = _run_colab(["upload", "-s", session, str(local_path), remote_path], timeout=300)
            if res.returncode != 0:
                raise RuntimeError(res.stderr or res.stdout)
            out = {"mode": "direct", "direction": "upload", "bytes": size_bytes, "remote_path": remote_path}
            return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}

        if chosen == "drive_stage":
            if not conf_path:
                raise FileNotFoundError("No rclone.conf found for 0-CU Drive staging")
            drive_target = f"{drive_cache.rstrip('/')}/{local_path.name}"
            transfer_id = str(args.get("transfer_id") or f"xfer-{uuid.uuid4().hex[:8]}")
            async_requested = bool(args.get("async_transfer", False)) or size_bytes > 100 * 1024 * 1024
            sync_wait_cap = float(os.environ.get("COLAB_SYNC_TRANSFER_WAIT_SEC", "115") or 115)

            transfers_dir = data_dir / "transfers"
            transfers_dir.mkdir(parents=True, exist_ok=True)
            log_file = transfers_dir / f"{transfer_id}.log"
            spec_file = transfers_dir / f"{transfer_id}.spec.json"
            spec_file.write_text(
                json.dumps(
                    {
                        "transfer_id": transfer_id,
                        "local_path": str(local_path),
                        "drive_cache": drive_cache,
                        "drive_target": drive_target,
                        "conf_path": str(conf_path),
                        "session": session,
                        "remote_path": remote_path,
                        "bytes": size_bytes,
                        "log_file": str(log_file),
                    }
                ),
                encoding="utf-8",
            )

            jobs = _read_json(data_dir / "transfer_jobs.json", {})
            if not isinstance(jobs, dict):
                jobs = {}
            init_rec = {
                "transfer_id": transfer_id,
                "job_id": transfer_id,
                "mode": "drive_stage",
                "direction": "upload",
                "session": session,
                "local_path": str(local_path),
                "drive_target": drive_target,
                "remote_path": remote_path if session else None,
                "bytes": size_bytes,
                "bytes_transferred": 0,
                "percent": 0.0,
                "state": "running",
                "phase": "drive_upload",
                "log_file": str(log_file),
                "started_at": time.time(),
            }
            jobs[transfer_id] = init_rec
            _write_json(data_dir / "transfer_jobs.json", jobs)

            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    os.path.abspath(__file__),
                    "--data-dir",
                    str(data_dir),
                    "--transfer-worker",
                    str(spec_file),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            if async_requested:
                out = dict(init_rec)
                out["async"] = True
                return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}

            wait_deadline = time.monotonic() + max(1.0, sync_wait_cap)
            while time.monotonic() < wait_deadline:
                cur_jobs = _read_json(data_dir / "transfer_jobs.json", {})
                cur = cur_jobs.get(transfer_id) if isinstance(cur_jobs, dict) else None
                if isinstance(cur, dict):
                    if cur.get("state") == "completed":
                        out = dict(cur)
                        out["async"] = False
                        return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}
                    if cur.get("state") == "failed":
                        raise RuntimeError(str(cur.get("error") or "drive_stage transfer failed"))
                time.sleep(0.25)

            cur_jobs = _read_json(data_dir / "transfer_jobs.json", {})
            cur = cur_jobs.get(transfer_id) if isinstance(cur_jobs, dict) else init_rec
            out = dict(cur if isinstance(cur, dict) else init_rec)
            out["async"] = True
            return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}

        # Chunked <=24 MiB non-dot upload
        if not session:
            raise ValueError("`session` is required for chunked upload")
        chunk_bytes = 24 * 1024 * 1024
        tag = uuid.uuid4().hex[:8]
        tmp_dir = Path(tempfile.mkdtemp(prefix="colab_chunk_"))
        colab_bin = _resolve_colab_bin()
        expected_sha = _sha256_file(local_path)
        try:
            parts: list[tuple[int, Path, str]] = []
            with open(local_path, "rb") as src:
                idx = 0
                while True:
                    buf = src.read(chunk_bytes)
                    if not buf:
                        break
                    lp = tmp_dir / f"part_{idx:04d}"
                    lp.write_bytes(buf)
                    parts.append((idx, lp, f"/content/colab_up_{tag}_{idx:04d}"))
                    idx += 1

            def _up(item: tuple[int, Path, str]) -> int:
                i, lp, rp = item
                for attempt in range(3):
                    r = subprocess.run([colab_bin, "upload", "-s", session, str(lp), rp], capture_output=True, text=True, timeout=180)
                    if r.returncode == 0:
                        return i
                    time.sleep(1.5 * (attempt + 1))
                raise RuntimeError(f"Chunk {i} failed")

            with ThreadPoolExecutor(max_workers=2) as ex:
                list(ex.map(_up, parts))

            reasm = (
                "import hashlib, os\n"
                f"rp = {remote_path!r}\n"
                f"tag = {tag!r}\n"
                f"n = {len(parts)}\n"
                f"exp = {expected_sha!r}\n"
                "os.makedirs(os.path.dirname(rp), exist_ok=True)\n"
                "h = hashlib.sha256()\n"
                "with open(rp, 'wb') as dst:\n"
                "    for i in range(n):\n"
                "        cp = f'/content/colab_up_{tag}_{i:04d}'\n"
                "        with open(cp, 'rb') as s:\n"
                "            while True:\n"
                "                b = s.read(4*1024*1024)\n"
                "                if not b: break\n"
                "                dst.write(b); h.update(b)\n"
                "        os.remove(cp)\n"
                "assert h.hexdigest() == exp, 'SHA256 mismatch'\n"
                "print('CHUNK_OK:' + str(os.path.getsize(rp)))\n"
            )
            r_res = subprocess.run([colab_bin, "exec", "-s", session], input=reasm, capture_output=True, text=True, timeout=300)
            if r_res.returncode != 0:
                raise RuntimeError(r_res.stderr or r_res.stdout)
            out = {"mode": "chunked", "direction": "upload", "bytes": size_bytes, "chunks": len(parts), "sha256": expected_sha, "remote_path": remote_path}
            return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Download
    if not session:
        raise ValueError("`session` is required for download")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    res = _run_colab(["download", "-s", session, remote_path, str(local_path)], timeout=600)
    if res.returncode != 0 or not local_path.is_file():
        raise RuntimeError(f"Download failed: {res.stderr or res.stdout}")
    out = {
        "mode": "direct",
        "direction": "download",
        "local_path": str(local_path),
        "bytes": local_path.stat().st_size,
        "sha256": _sha256_file(local_path),
    }
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


def build_detached_wrapper_source() -> str:
    """Return the standalone _wrapper.py source executed via nohup on Colab VMs."""
    return '''import fnmatch, json, os, shutil, subprocess, sys, threading, time
job_dir, script, s_args, job_id = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
sync_dir = sys.argv[5] if len(sys.argv) > 5 else ""
drive_sync_dest = sys.argv[6] if len(sys.argv) > 6 else ""
sync_include = json.loads(sys.argv[7]) if len(sys.argv) > 7 and sys.argv[7] else []
status_p = f"{job_dir}/status.json"
log_p = f"{job_dir}/job.log"

def write_st(st):
    tmp = status_p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, status_p)

def gpu_stats():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            u, used, tot = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            return f"{u}%", f"{round(float(used)/1024, 1)}/{round(float(tot)/1024, 1)} GB"
    except Exception:
        pass
    return None, None

def _read_proc_stat():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except Exception:
        return None, None

_cpu_prev = [None, None]
_t_a, _i_a = _read_proc_stat()
if _t_a is not None:
    time.sleep(0.15)
    _t_b, _i_b = _read_proc_stat()
    _cpu_prev = [_t_a, _i_a] if _t_b is None else [_t_a, _i_a]

def cpu_stats():
    t_now, i_now = _read_proc_stat()
    cpu_pct = None
    if t_now is not None and _cpu_prev[0] is not None and t_now > _cpu_prev[0]:
        dt = t_now - _cpu_prev[0]
        di = i_now - _cpu_prev[1]
        cpu_pct = round(max(0.0, min(100.0, 100.0 * (1.0 - di / dt))), 1)
        _cpu_prev[0], _cpu_prev[1] = t_now, i_now
    elif t_now is not None:
        _cpu_prev[0], _cpu_prev[1] = t_now, i_now
    try:
        load_1m = round(os.getloadavg()[0], 2)
    except Exception:
        load_1m = None
    return cpu_pct, os.cpu_count(), load_1m

synced_files = {}
sync_state = {"last_drive_sync_ts": None, "synced_file_count": 0, "last_sync_attempt_ts": 0.0}
rclone_conf_vm = os.environ.get("RCLONE_CONFIG", "/root/.config/rclone/rclone.conf")

def _matches_include(name):
    if not sync_include:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in sync_include)

def sync_checkpoints(final=False):
    if not sync_dir or not drive_sync_dest:
        return
    if not os.path.isdir(sync_dir) or not shutil.which("rclone") or not os.path.isfile(rclone_conf_vm):
        return
    now = time.time()
    if not final and (now - sync_state["last_sync_attempt_ts"]) < 15.0:
        return
    changed = False
    matched = []
    for root_dir, _dirs, files in os.walk(sync_dir):
        for fn in files:
            if not _matches_include(fn):
                continue
            fp = os.path.join(root_dir, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_size)
            matched.append((fp, sig))
            if synced_files.get(fp) != sig:
                changed = True
    if not changed and not final:
        return
    sync_state["last_sync_attempt_ts"] = now
    cmd = ["rclone", "copy", sync_dir, drive_sync_dest, "--config", rclone_conf_vm]
    for pat in sync_include:
        cmd.extend(["--include", pat])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            for fp, sig in matched:
                synced_files[fp] = sig
            if os.path.isfile(log_p):
                subprocess.run(
                    ["rclone", "copyto", log_p, drive_sync_dest.rstrip("/") + "/job.log", "--config", rclone_conf_vm],
                    capture_output=True, text=True, timeout=60,
                )
            sync_state["last_drive_sync_ts"] = time.time()
            sync_state["synced_file_count"] = len(synced_files)
    except Exception:
        pass

t0 = time.time()
proc = subprocess.Popen(
    [sys.executable, "-u", script, *s_args],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)
done_ev = threading.Event()

def hb():
    while not done_ev.is_set():
        gu, vr = gpu_stats()
        cp, cc, l1 = cpu_stats()
        sync_checkpoints(final=False)
        write_st({
            "job_id": job_id,
            "state": "running",
            "pid": proc.pid,
            "started_at": t0,
            "heartbeat_ts": time.time(),
            "elapsed_sec": round(time.time() - t0, 1),
            "gpu_util": gu,
            "vram_used": vr,
            "cpu_pct": cp,
            "cpu_count": cc,
            "load_1m": l1,
            "exit_code": None,
            "last_drive_sync_ts": sync_state["last_drive_sync_ts"],
            "synced_file_count": sync_state["synced_file_count"],
        })
        done_ev.wait(10.0)

th = threading.Thread(target=hb, daemon=True)
th.start()
with open(log_p, "w", encoding="utf-8", buffering=1) as lf:
    for ch in iter(lambda: proc.stdout.read(256), ""):
        lf.write(ch.replace("\\r", "\\n"))
rc = proc.wait()
done_ev.set()
th.join(timeout=2.0)
sync_checkpoints(final=True)
gu, vr = gpu_stats()
cp, cc, l1 = cpu_stats()
write_st({
    "job_id": job_id,
    "state": "completed" if rc == 0 else "failed",
    "pid": proc.pid,
    "started_at": t0,
    "heartbeat_ts": time.time(),
    "elapsed_sec": round(time.time() - t0, 1),
    "gpu_util": gu,
    "vram_used": vr,
    "cpu_pct": cp,
    "cpu_count": cc,
    "load_1m": l1,
    "exit_code": rc,
    "last_drive_sync_ts": sync_state["last_drive_sync_ts"],
    "synced_file_count": sync_state["synced_file_count"],
})
'''


def tool_colab_launch_detached(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    script_path = Path(str(args["script_path"])).expanduser()
    if not script_path.is_file():
        raise FileNotFoundError(f"Script not found: {script_path}")

    job_id = str(args.get("job_id") or f"job-{uuid.uuid4().hex[:8]}")
    script_args = [str(x) for x in (args.get("args") or [])]
    extra_env = {str(k): str(v) for k, v in (args.get("env") or {}).items()}
    auto_stop_vm = bool(args.get("auto_stop_vm", False))
    sync_dir = str(args.get("sync_dir") or "").strip()
    drive_sync_dest = str(args.get("drive_sync_dest") or "").strip()
    sync_include = [str(x) for x in (args.get("sync_include") or [])]

    remote_dir = f"/content/.colab_jobs/{job_id}"
    remote_script = f"{remote_dir}/{script_path.name}"
    res_up = _run_colab(["upload", "-s", session, str(script_path), remote_script], timeout=120)
    if res_up.returncode != 0:
        raise RuntimeError(f"Failed to upload script: {res_up.stderr or res_up.stdout}")

    wrapper_src = build_detached_wrapper_source()
    env_flags: list[str] = []
    if sync_dir and drive_sync_dest:
        conf_path = _find_rclone_conf()
        if conf_path:
            conf_b64 = base64.b64encode(conf_path.read_bytes()).decode("ascii")
            env_flags.extend(["--env", f"RCLONE_CONF_B64={conf_b64}"])

    runner_code = (
        "import base64, json, os, shutil, subprocess, sys\n"
        "from pathlib import Path\n"
        f"job_dir = {remote_dir!r}\n"
        f"script = {remote_script!r}\n"
        f"s_args = {script_args!r}\n"
        f"extra_env = {extra_env!r}\n"
        f"job_id = {job_id!r}\n"
        f"sync_dir = {sync_dir!r}\n"
        f"drive_sync_dest = {drive_sync_dest!r}\n"
        f"sync_include = {sync_include!r}\n"
        f"wrapper_src = {wrapper_src!r}\n"
        "os.makedirs(job_dir, exist_ok=True)\n"
        "if os.environ.get('RCLONE_CONF_B64'):\n"
        "    rc = Path('/root/.config/rclone/rclone.conf')\n"
        "    rc.parent.mkdir(parents=True, exist_ok=True)\n"
        "    rc.write_bytes(base64.b64decode(os.environ['RCLONE_CONF_B64']))\n"
        "    os.chmod(rc, 0o600)\n"
        "    if not shutil.which('rclone'):\n"
        "        subprocess.run(['bash', '-c', 'apt-get update -qq && apt-get install -y -qq rclone >/dev/null'], check=False)\n"
        "wrapper = f'{job_dir}/_wrapper.py'\n"
        "with open(wrapper, 'w', encoding='utf-8') as f: f.write(wrapper_src)\n"
        "env = os.environ.copy()\n"
        "env['PYTHONUNBUFFERED'] = '1'\n"
        "env['TQDM_MININTERVAL'] = '5'\n"
        "env.update(extra_env)\n"
        "cmd = ['nohup', sys.executable, '-u', wrapper, job_dir, script, json.dumps(s_args), job_id, sync_dir, drive_sync_dest, json.dumps(sync_include)]\n"
        "p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=open(f'{job_dir}/nohup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True)\n"
        "print('DETACHED_PID:' + str(p.pid))\n"
    )
    res_ex = subprocess.run([_resolve_colab_bin(), "exec", "-s", session, *env_flags], input=runner_code, capture_output=True, text=True, timeout=60)
    if res_ex.returncode != 0:
        raise RuntimeError(f"Detached launch failed: {res_ex.stderr or res_ex.stdout}")

    jobs = _read_json(data_dir / "detached_jobs.json", {})
    if not isinstance(jobs, dict):
        jobs = {}
    job_rec = {
        "job_id": job_id,
        "session": session,
        "script_name": script_path.name,
        "remote_dir": remote_dir,
        "state": "running",
        "auto_stop_vm": auto_stop_vm,
        "sync_dir": sync_dir or None,
        "drive_sync_dest": drive_sync_dest or None,
        "started_at": time.time(),
    }
    jobs[job_id] = job_rec
    _write_json(data_dir / "detached_jobs.json", jobs)
    keepalive = _ensure_keepalive_daemon(data_dir)
    job_rec["keepalive_pid"] = keepalive.get("pid")
    return {"content": [{"type": "text", "text": json.dumps(job_rec, indent=2)}], "structuredContent": job_rec}


def tool_colab_poll_job(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    job_id = str(args["job_id"])
    session = str(args.get("session") or "")
    tail_lines = int(args.get("tail_lines", 30) or 30)

    transfers = _read_json(data_dir / "transfer_jobs.json", {})
    if isinstance(transfers, dict) and job_id in transfers and isinstance(transfers[job_id], dict):
        rec = dict(transfers[job_id])
        lp = Path(str(rec.get("log_file") or ""))
        if lp.is_file() and tail_lines > 0:
            rec["tail_lines"] = lp.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]
        else:
            rec["tail_lines"] = []
        return {"content": [{"type": "text", "text": json.dumps(rec, indent=2)}], "structuredContent": rec}

    if not session:
        jobs = _read_json(data_dir / "detached_jobs.json", {})
        if isinstance(jobs, dict) and isinstance(jobs.get(job_id), dict):
            session = str(jobs[job_id].get("session") or "")
    if not session:
        raise ValueError("`session` is required when polling a remote Colab job")

    tmp_dir = Path(tempfile.mkdtemp(prefix="colab_poll_"))
    try:
        st_local = tmp_dir / "status.json"
        log_local = tmp_dir / "job.log"
        r_st = _run_colab(["download", "-s", session, f"/content/.colab_jobs/{job_id}/status.json", str(st_local)], timeout=30)
        if r_st.returncode != 0 or not st_local.is_file():
            out = {"job_id": job_id, "session": session, "state": "vm_unreachable", "detail": (r_st.stderr or r_st.stdout).strip()}
            return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}

        st_obj = json.loads(st_local.read_text(encoding="utf-8"))
        tail: list[str] = []
        r_lg = _run_colab(["download", "-s", session, f"/content/.colab_jobs/{job_id}/job.log", str(log_local)], timeout=30)
        if r_lg.returncode == 0 and log_local.is_file():
            lines = log_local.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-tail_lines:] if tail_lines > 0 else []

        st_obj["session"] = session
        st_obj["tail_lines"] = tail
        st_obj["tail_line"] = tail[-1] if tail else ""

        jobs = _read_json(data_dir / "detached_jobs.json", {})
        if isinstance(jobs, dict) and job_id in jobs:
            jobs[job_id].update(
                {
                    "state": st_obj.get("state", "running"),
                    "gpu_util": st_obj.get("gpu_util"),
                    "vram_used": st_obj.get("vram_used"),
                    "cpu_pct": st_obj.get("cpu_pct"),
                    "cpu_count": st_obj.get("cpu_count"),
                    "load_1m": st_obj.get("load_1m"),
                    "exit_code": st_obj.get("exit_code"),
                    "tail_line": st_obj.get("tail_line", ""),
                }
            )
            _write_json(data_dir / "detached_jobs.json", jobs)
        if st_obj.get("cpu_pct") is not None:
            _record_vm_cpu_telemetry(
                data_dir,
                session,
                {
                    "cpu_pct": st_obj.get("cpu_pct"),
                    "cpu_count": st_obj.get("cpu_count"),
                    "load_1m": st_obj.get("load_1m"),
                    "sampled_at": st_obj.get("heartbeat_ts") or time.time(),
                    "source": "detached_job",
                },
            )

        return {"content": [{"type": "text", "text": json.dumps(st_obj, indent=2)}], "structuredContent": st_obj}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def tool_colab_reset_vram(_data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    drop_globals = [str(x) for x in (args.get("drop_globals") or [])]
    code = (
        "import gc, json, sys\n"
        f"drop_names = {drop_globals!r}\n"
        "for attr in ('last_traceback', 'last_value', 'last_type'):\n"
        "    if hasattr(sys, attr): setattr(sys, attr, None)\n"
        "ip = get_ipython() if 'get_ipython' in globals() else None\n"
        "if ip is not None:\n"
        "    for k in ('Out', '_oh', '_', '__', '___'):\n"
        "        obj = ip.user_ns.get(k)\n"
        "        if isinstance(obj, dict): obj.clear()\n"
        "    for name in drop_names:\n"
        "        ip.user_ns.pop(name, None)\n"
        "gc.collect()\n"
        "out = {'cleared': True, 'vram_before_gb': None, 'vram_after_gb': None}\n"
        "try:\n"
        "    import torch\n"
        "    if torch.cuda.is_available():\n"
        "        out['vram_before_gb'] = round(torch.cuda.memory_allocated() / (1024**3), 3)\n"
        "        torch.cuda.empty_cache()\n"
        "        torch.cuda.ipc_collect()\n"
        "        out['vram_after_gb'] = round(torch.cuda.memory_allocated() / (1024**3), 3)\n"
        "except Exception: pass\n"
        "print('VRAM_RESET:' + json.dumps(out))\n"
    )
    res = subprocess.run([_resolve_colab_bin(), "exec", "-s", session], input=code, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr or res.stdout)
    out: dict[str, Any] = {"session": session, "cleared": True}
    for ln in res.stdout.splitlines():
        if ln.startswith("VRAM_RESET:"):
            out.update(json.loads(ln.split("VRAM_RESET:", 1)[1]))
            break
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


def tool_colab_keep_warm(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    minutes = float(args.get("minutes", 30) or 30)
    clear = bool(args.get("clear", False))
    reason = str(args.get("reason", "Manual keep-warm lease"))

    leases = _get_leases(data_dir)
    if clear:
        leases.pop(session, None)
        _write_json(data_dir / "kept_sessions.json", leases)
        out = {"session": session, "leased": False}
        return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}

    expires_at = time.time() + minutes * 60.0
    leases[session] = {"session": session, "minutes": minutes, "expires_at": expires_at, "reason": reason}
    _write_json(data_dir / "kept_sessions.json", leases)
    keepalive = _ensure_keepalive_daemon(data_dir)
    out = {
        "session": session,
        "leased": True,
        "minutes": minutes,
        "expires_at": expires_at,
        "reason": reason,
        "keepalive_pid": keepalive.get("pid"),
    }
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


def tool_colab_stop_all(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("session") or "").strip()
    stopped: list[str] = []
    leases = _get_leases(data_dir)

    if target:
        if target.startswith("untracked:"):
            ep = target.split("untracked:", 1)[1]
            _unassign_endpoint(ep)
            stopped.append(target)
        else:
            _run_colab(["stop", "-s", target], timeout=45)
            stopped.append(target)
            leases.pop(target, None)
    else:
        local = _read_json(_sessions_file(), {})
        names = list(local.keys()) if isinstance(local, dict) else []
        srv_sessions = _fetch_sessions_safe()
        orphan_endpoints: list[str] = []
        for s in srv_sessions:
            if s["name"] == "?":
                orphan_endpoints.append(s["endpoint"])
            elif s["name"] not in names:
                names.append(s["name"])
        for n in names:
            _run_colab(["stop", "-s", n], timeout=45)
            stopped.append(n)
            leases.pop(n, None)
        for ep in orphan_endpoints:
            _unassign_endpoint(ep)
            stopped.append(f"untracked:{ep}")

    _write_json(data_dir / "kept_sessions.json", leases)
    usage = _parse_usage(data_dir)
    out = {"stopped": stopped, "usage": usage}
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


def tool_colab_wakeup_alarm(_data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "status"))
    default_phrase = "Colab Pilot chirp test" if action == "test_chirp" else "0 0 0 0 0 Colab job complete"
    phrase = str(args.get("phrase") or default_phrase)
    ramp = float(args.get("ramp_seconds", 300) or 300)
    script = _plugin_root() / "scripts" / "colab_wakeup_alarm.py"
    if not script.is_file():
        raise FileNotFoundError(f"Missing alarm script: {script}")

    if action == "self_test":
        cli_action = "self-test"
    elif action == "test_chirp":
        cli_action = "test-chirp"
    else:
        cli_action = action
    cmd = [sys.executable, "-B", str(script), "--action", cli_action]
    if cli_action == "start":
        cmd.extend(["--daemon", "--phrase", phrase, "--ramp-seconds", str(ramp)])
    elif cli_action == "test-chirp":
        cmd.extend(["--phrase", phrase])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(res.stderr or res.stdout)
    out = json.loads(res.stdout) if res.stdout.strip() else {"ok": True}
    return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "structuredContent": out}


# ---------------------------------------------------------------------------
# Tool & Resource Registry
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "colab_dashboard",
        "description": "Open the interactive Colab Pilot dashboard showing live Compute Unit balance, burn rate, 1/5th monthly Ultra budget guardrail, active sessions, detached jobs, and alarm controls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ultra_monthly_cu": {"type": "number", "default": 25000},
                "budget_fraction": {"type": "number", "default": 0.20},
            },
        },
        "_meta": {
            "ui": {"resourceUri": APP_URI, "visibility": ["model", "app"]},
            "ui/resourceUri": APP_URI,
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "aux_pane", "title": "Colab Pilot"},
                    {"type": "global", "title": "Colab Pilot"},
                ]
            },
        },
    },
    {
        "name": "colab_allocate_cascade",
        "description": "Try GPU tiers in priority order (e.g. A100-highmem -> A100 -> G4 -> L4) within a hard <=145s wall-clock budget, skip unsupported accelerators immediately, verify BF16 support, probe VRAM/RAM/NVENC, and return safe DataLoader/crop sizing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "tiers": {"type": "array", "items": {"type": "string"}, "default": ["A100-highmem", "A100", "G4", "L4"]},
                "require_bf16": {"type": "boolean", "default": True},
                "keep_warm_minutes": {"type": "number", "default": 0},
                "max_wall_seconds": {"type": "number", "default": 140},
            },
            "required": ["session"],
        },
    },
    {
        "name": "colab_mount_rclone",
        "description": "Headlessly configure rclone and mount Google Drive on a Colab VM (zero interactive OAuth) with workload-tuned VFS flags (random_seek vs streaming) and optional .env secret forwarding (e.g. HF_TOKEN).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "mode": {"type": "string", "enum": ["random_seek", "streaming", "config_only"], "default": "random_seek"},
                "remote_name": {"type": "string"},
                "mount_point": {"type": "string", "default": "/content/drive"},
                "rclone_conf": {"type": "string"},
                "forward_env_keys": {"type": "array", "items": {"type": "string"}, "default": ["HF_TOKEN"]},
            },
            "required": ["session"],
        },
    },
    {
        "name": "colab_smart_transfer",
        "description": "Transfer files safely between Mac and Colab without Jupyter /api/contents 500 errors or 180s MCP timeouts: <=24 MiB direct, >24 MiB via 0-CU Drive staging (async background job for >100 MiB or async_transfer=true) or 24 MiB chunked upload with SHA-256 verification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "direction": {"type": "string", "enum": ["upload", "download"]},
                "local_path": {"type": "string"},
                "remote_path": {"type": "string"},
                "mode": {"type": "string", "enum": ["auto", "drive_stage", "chunked", "direct"], "default": "auto"},
                "drive_cache_path": {"type": "string", "default": "gdrive_root:Colab Notebooks/colab_pilot_cache"},
                "async_transfer": {"type": "boolean", "default": False},
                "transfer_id": {"type": "string"},
            },
            "required": ["direction", "local_path", "remote_path"],
        },
    },
    {
        "name": "colab_launch_detached",
        "description": "Launch a Python script detached via nohup on a Colab VM with TQDM_MININTERVAL=5, CR->NL log normalization, 10s GPU/VRAM heartbeat JSON, optional incremental Drive checkpoint mirroring (sync_dir + drive_sync_dest), and local runtime keepalive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "script_path": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "object"},
                "job_id": {"type": "string"},
                "auto_stop_vm": {"type": "boolean", "default": False},
                "sync_dir": {"type": "string"},
                "drive_sync_dest": {"type": "string"},
                "sync_include": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["session", "script_path"],
        },
    },
    {
        "name": "colab_poll_job",
        "description": "Poll a detached Colab job (via REST Contents API) or a background drive_stage transfer (transfer_jobs.json) without opening a WebSocket.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "job_id": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 30},
            },
            "required": ["job_id"],
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "colab_reset_vram",
        "description": "Clear sys.last_traceback, IPython Out caches, optional globals, and torch.cuda.empty_cache() to free zombie VRAM after a crashed cell.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "drop_globals": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["session"],
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "colab_keep_warm",
        "description": "Register or clear a timed keep-warm lease for a Colab session so the Stop CU-leak guardrail allows the turn to end intentionally.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "minutes": {"type": "number", "default": 30},
                "clear": {"type": "boolean", "default": False},
                "reason": {"type": "string"},
            },
            "required": ["session"],
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "colab_stop_all",
        "description": "Stop one named Colab session or all active Colab sessions, clear leases, and return verified 0.00/hr Compute Unit usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
            },
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "colab_wakeup_alarm",
        "description": "Control the escalating multi-voice macOS TTS + chirp wakeup alarm (start, stop, status, test_chirp, self_test).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "status", "test_chirp", "self_test"], "default": "status"},
                "phrase": {"type": "string", "default": "0 0 0 0 0 Colab job complete"},
                "ramp_seconds": {"type": "number", "default": 300},
            },
            "required": ["action"],
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
]

TOOL_DISPATCH = {
    "colab_dashboard": tool_colab_dashboard,
    "colab_allocate_cascade": tool_colab_allocate_cascade,
    "colab_mount_rclone": tool_colab_mount_rclone,
    "colab_smart_transfer": tool_colab_smart_transfer,
    "colab_launch_detached": tool_colab_launch_detached,
    "colab_poll_job": tool_colab_poll_job,
    "colab_reset_vram": tool_colab_reset_vram,
    "colab_keep_warm": tool_colab_keep_warm,
    "colab_stop_all": tool_colab_stop_all,
    "colab_wakeup_alarm": tool_colab_wakeup_alarm,
}


def _send(obj: dict[str, Any]) -> None:
    line = json.dumps(obj) + "\n"
    with _SEND_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _handle_request(req: dict[str, Any], data_dir: Path) -> None:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    # Notifications have no id
    if req_id is None:
        return

    try:
        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": {"name": "colab-pilot", "version": "1.3.2"},
                    },
                }
            )
            return

        if method == "ping":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return

        if method == "resources/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resources": [
                            {
                                "uri": APP_URI,
                                "name": "Colab Pilot Dashboard",
                                "description": "Interactive Compute Unit telemetry, session control, and detached job monitor.",
                                "mimeType": "text/html;profile=mcp-app",
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                }
            )
            return

        if method == "resources/read":
            uri = params.get("uri", "")
            if uri != APP_URI:
                _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"}})
                return
            html_path = _plugin_root() / "assets" / "dashboard.html"
            html_text = html_path.read_text(encoding="utf-8")
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [
                            {
                                "uri": APP_URI,
                                "mimeType": "text/html;profile=mcp-app",
                                "text": html_text,
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                }
            )
            return

        if method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
            return

        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            fn = TOOL_DISPATCH.get(name)
            if not fn:
                _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}})
                return
            op_id = uuid.uuid4().hex if name != "colab_dashboard" else ""
            if op_id:
                with _ACTIVE_OPS_LOCK:
                    _ACTIVE_OPS[op_id] = {
                        "op_id": op_id,
                        "tool": name,
                        "session": str(arguments.get("session") or ""),
                        "started_at": time.time(),
                    }
            try:
                res = fn(data_dir, arguments)
                _send({"jsonrpc": "2.0", "id": req_id, "result": res})
            except Exception as exc:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": f"Error in {name}: {exc}"}],
                        },
                    }
                )
            finally:
                if op_id:
                    with _ACTIVE_OPS_LOCK:
                        _ACTIVE_OPS.pop(op_id, None)
            return

        _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
    except Exception as exc:
        _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}})


def main() -> int:
    default_data_dir = (
        os.environ.get("CLAUDE_PLUGIN_DATA")
        or os.environ.get("PLUGIN_DATA")
        or str(Path.home() / ".cache" / "colab-pilot")
    )
    p = argparse.ArgumentParser(description="Colab Pilot MCP Server")
    p.add_argument("--data-dir", default=default_data_dir)
    p.add_argument("--keepalive-daemon", action="store_true")
    p.add_argument("--interval", type=float, default=float(os.environ.get("COLAB_KEEPALIVE_INTERVAL_SEC", "150") or 150))
    p.add_argument("--transfer-worker", type=Path)
    args = p.parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.transfer_worker:
        return _run_transfer_worker(data_dir, args.transfer_worker)
    if args.keepalive_daemon:
        return _run_keepalive_daemon(data_dir, args.interval)

    executor = ThreadPoolExecutor(max_workers=8)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}})
                continue
            if req.get("method") == "tools/call":
                executor.submit(_handle_request, req, data_dir)
            else:
                _handle_request(req, data_dir)
    finally:
        executor.shutdown(wait=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

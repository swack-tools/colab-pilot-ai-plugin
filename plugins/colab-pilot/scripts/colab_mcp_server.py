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
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

APP_URI = "ui://colab-pilot/dashboard.html"
VALID_TIERS = {"A100-highmem", "A100", "G4", "L4", "H100", "T4", "NONE"}
_SEND_LOCK = threading.Lock()
_STATE_FILE_LOCK = threading.Lock()
_ACTIVE_OPS_LOCK = threading.Lock()
_ACTIVE_OPS: dict[str, dict[str, Any]] = {}


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


def _run_colab(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    colab_bin = _resolve_colab_bin()
    return subprocess.run([colab_bin, *args], capture_output=True, text=True, timeout=timeout)


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

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_usage = pool.submit(_parse_usage, data_dir)
        f_sessions = pool.submit(_fetch_sessions_safe)
        f_alarm = pool.submit(_fetch_alarm_status)
        usage = f_usage.result()
        server_sessions = f_sessions.result()
        alarm_status = f_alarm.result()

    local_sessions = _read_json(_sessions_file(), {})
    if not isinstance(local_sessions, dict):
        local_sessions = {}

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

    jobs_raw = _read_json(data_dir / "detached_jobs.json", {})
    job_list = list(jobs_raw.values()) if isinstance(jobs_raw, dict) else []
    job_list.sort(key=lambda j: float(j.get("started_at", 0) or 0), reverse=True)
    active_ops = _list_active_ops()

    structured = {
        "usage": usage,
        "ultra_monthly_cu": ultra_monthly_cu,
        "budget_fraction": budget_fraction,
        "sessions": session_list,
        "jobs": job_list[:10],
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


def tool_colab_allocate_cascade(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    tiers = args.get("tiers") or ["A100-highmem", "A100", "G4", "L4"]
    require_bf16 = bool(args.get("require_bf16", True))
    keep_warm_minutes = float(args.get("keep_warm_minutes", 0) or 0)

    for t in tiers:
        if t not in VALID_TIERS:
            raise ValueError(f"Invalid tier {t!r}; valid: {sorted(VALID_TIERS)}")

    attempts: list[dict[str, Any]] = []
    assigned_tier = None

    for tier in tiers:
        _run_colab(["stop", "-s", session], timeout=30)
        cmd = ["new", "-s", session]
        if tier == "A100-highmem":
            cmd.extend(["--gpu", "A100", "--high-mem"])
        elif tier != "NONE":
            cmd.extend(["--gpu", tier])

        res = _run_colab(cmd, timeout=240)
        if res.returncode == 0:
            assigned_tier = tier
            attempts.append({"tier": tier, "ok": True})
            break
        attempts.append({"tier": tier, "ok": False, "detail": (res.stderr or res.stdout).strip()[:200]})
        time.sleep(2)

    if not assigned_tier:
        raise RuntimeError(f"All tiers failed: {json.dumps(attempts)}")

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
    p_res = _run_colab(["exec", "-s", session], timeout=120) if False else subprocess.run(
        [_resolve_colab_bin(), "exec", "-s", session],
        input=probe_code,
        capture_output=True,
        text=True,
        timeout=120,
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
            r_res = subprocess.run(
                ["rclone", "copy", str(local_path), drive_cache, "--config", str(conf_path), "--drive-chunk-size", "128M", "--transfers", "4"],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if r_res.returncode != 0:
                raise RuntimeError(f"rclone copy failed: {r_res.stderr}")
            drive_target = f"{drive_cache.rstrip('/')}/{local_path.name}"
            pulled_to_vm = False
            if session:
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
                    [_resolve_colab_bin(), "exec", "-s", session, "--env", f"RCLONE_CONF_B64={conf_b64}", "--env", f"SRC_DRIVE={drive_target}", "--env", f"DST_PATH={remote_path}"],
                    input=pull_py,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
                if p_res.returncode != 0:
                    raise RuntimeError(f"VM rclone pull failed: {p_res.stderr or p_res.stdout}")
                pulled_to_vm = True
            out = {
                "mode": "drive_stage",
                "direction": "upload",
                "bytes": size_bytes,
                "drive_target": drive_target,
                "pulled_to_vm": pulled_to_vm,
                "remote_path": remote_path if pulled_to_vm else None,
            }
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


def tool_colab_launch_detached(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    script_path = Path(str(args["script_path"])).expanduser()
    if not script_path.is_file():
        raise FileNotFoundError(f"Script not found: {script_path}")

    job_id = str(args.get("job_id") or f"job-{uuid.uuid4().hex[:8]}")
    script_args = [str(x) for x in (args.get("args") or [])]
    extra_env = {str(k): str(v) for k, v in (args.get("env") or {}).items()}
    auto_stop_vm = bool(args.get("auto_stop_vm", False))

    remote_dir = f"/content/.colab_jobs/{job_id}"
    remote_script = f"{remote_dir}/{script_path.name}"
    res_up = _run_colab(["upload", "-s", session, str(script_path), remote_script], timeout=120)
    if res_up.returncode != 0:
        raise RuntimeError(f"Failed to upload script: {res_up.stderr or res_up.stdout}")

    runner_code = (
        "import json, os, subprocess, sys, threading, time\n"
        f"job_dir = {remote_dir!r}\n"
        f"script = {remote_script!r}\n"
        f"s_args = {script_args!r}\n"
        f"extra_env = {extra_env!r}\n"
        f"job_id = {job_id!r}\n"
        "os.makedirs(job_dir, exist_ok=True)\n"
        "wrapper = f'{job_dir}/_wrapper.py'\n"
        "wrapper_src = '''import json, os, subprocess, sys, threading, time\n"
        "job_dir, script, s_args, job_id = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]\n"
        "status_p = f'{job_dir}/status.json'\n"
        "log_p = f'{job_dir}/job.log'\n"
        "def write_st(st):\n"
        "    tmp = status_p + '.tmp'\n"
        "    with open(tmp, 'w') as f: json.dump(st, f)\n"
        "    os.replace(tmp, status_p)\n"
        "def gpu_stats():\n"
        "    try:\n"
        "        r = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)\n"
        "        if r.returncode == 0 and r.stdout.strip():\n"
        "            u, used, tot = [x.strip() for x in r.stdout.strip().splitlines()[0].split(',')]\n"
        "            return f'{u}%', f'{round(float(used)/1024, 1)}/{round(float(tot)/1024, 1)} GB'\n"
        "    except Exception: pass\n"
        "    return None, None\n"
        "t0 = time.time()\n"
        "proc = subprocess.Popen([sys.executable, '-u', script, *s_args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)\n"
        "done_ev = threading.Event()\n"
        "def hb():\n"
        "    while not done_ev.is_set():\n"
        "        gu, vr = gpu_stats()\n"
        "        write_st({'job_id': job_id, 'state': 'running', 'pid': proc.pid, 'started_at': t0, 'heartbeat_ts': time.time(), 'elapsed_sec': round(time.time()-t0, 1), 'gpu_util': gu, 'vram_used': vr, 'exit_code': None})\n"
        "        done_ev.wait(10.0)\n"
        "th = threading.Thread(target=hb, daemon=True); th.start()\n"
        "with open(log_p, 'w', encoding='utf-8', buffering=1) as lf:\n"
        "    for ch in iter(lambda: proc.stdout.read(256), ''):\n"
        "        lf.write(ch.replace('\\\\r', '\\\\n'))\n"
        "rc = proc.wait()\n"
        "done_ev.set()\n"
        "gu, vr = gpu_stats()\n"
        "write_st({'job_id': job_id, 'state': 'completed' if rc == 0 else 'failed', 'pid': proc.pid, 'started_at': t0, 'heartbeat_ts': time.time(), 'elapsed_sec': round(time.time()-t0, 1), 'gpu_util': gu, 'vram_used': vr, 'exit_code': rc})\n"
        "'''\n"
        "with open(wrapper, 'w') as f: f.write(wrapper_src)\n"
        "env = os.environ.copy()\n"
        "env['PYTHONUNBUFFERED'] = '1'\n"
        "env['TQDM_MININTERVAL'] = '5'\n"
        "env.update(extra_env)\n"
        "p = subprocess.Popen(['nohup', sys.executable, '-u', wrapper, job_dir, script, json.dumps(s_args), job_id], env=env, stdin=subprocess.DEVNULL, stdout=open(f'{job_dir}/nohup.out', 'w'), stderr=subprocess.STDOUT, start_new_session=True)\n"
        "print('DETACHED_PID:' + str(p.pid))\n"
    )
    res_ex = subprocess.run([_resolve_colab_bin(), "exec", "-s", session], input=runner_code, capture_output=True, text=True, timeout=60)
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
        "started_at": time.time(),
    }
    jobs[job_id] = job_rec
    _write_json(data_dir / "detached_jobs.json", jobs)
    return {"content": [{"type": "text", "text": json.dumps(job_rec, indent=2)}], "structuredContent": job_rec}


def tool_colab_poll_job(data_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    session = str(args["session"])
    job_id = str(args["job_id"])
    tail_lines = int(args.get("tail_lines", 30) or 30)

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
                    "exit_code": st_obj.get("exit_code"),
                    "tail_line": st_obj.get("tail_line", ""),
                }
            )
            _write_json(data_dir / "detached_jobs.json", jobs)

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
    out = {"session": session, "leased": True, "minutes": minutes, "expires_at": expires_at, "reason": reason}
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
        "description": "Try GPU tiers in priority order (e.g. A100-highmem -> A100 -> G4 -> L4), verify BF16 support, probe VRAM/RAM/NVENC, and return safe DataLoader/crop sizing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "tiers": {"type": "array", "items": {"type": "string"}, "default": ["A100-highmem", "A100", "G4", "L4"]},
                "require_bf16": {"type": "boolean", "default": True},
                "keep_warm_minutes": {"type": "number", "default": 0},
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
        "description": "Transfer files safely between Mac and Colab without Jupyter /api/contents 500 errors: <=24 MiB direct, >24 MiB via 0-CU Drive staging or 24 MiB chunked upload with SHA-256 verification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "direction": {"type": "string", "enum": ["upload", "download"]},
                "local_path": {"type": "string"},
                "remote_path": {"type": "string"},
                "mode": {"type": "string", "enum": ["auto", "drive_stage", "chunked", "direct"], "default": "auto"},
                "drive_cache_path": {"type": "string", "default": "gdrive_root:Colab Notebooks/colab_pilot_cache"},
            },
            "required": ["direction", "local_path", "remote_path"],
        },
    },
    {
        "name": "colab_launch_detached",
        "description": "Launch a Python script detached via nohup on a Colab VM with TQDM_MININTERVAL=5, CR->NL log normalization, and 10s GPU/VRAM heartbeat JSON so WebSocket drops never kill the job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "script_path": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "object"},
                "job_id": {"type": "string"},
                "auto_stop_vm": {"type": "boolean", "default": False},
            },
            "required": ["session", "script_path"],
        },
    },
    {
        "name": "colab_poll_job",
        "description": "Poll a detached Colab job via REST Contents API (status.json + job.log tail) without opening a WebSocket.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "job_id": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 30},
            },
            "required": ["session", "job_id"],
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
                        "serverInfo": {"name": "colab-pilot", "version": "1.1.3"},
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
    p = argparse.ArgumentParser(description="Colab Pilot MCP Server")
    p.add_argument("--data-dir", default=str(Path.home() / ".cache" / "colab-pilot"))
    args = p.parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

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

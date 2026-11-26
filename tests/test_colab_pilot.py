#!/usr/bin/env python3
"""Cross-platform hermetic unit & MCP protocol test suite for colab-pilot."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "colab-pilot"
SCRIPTS_DIR = PLUGIN_DIR / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_mock_colab(bin_dir: Path, active_count: int = 1) -> Path:
    mock = bin_dir / "colab"
    mock.write_text(
        f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if "--config" in args:
    i = args.index("--config")
    args = args[:i] + args[i+2:]
cmd = args[0] if args else ""
if cmd == "usage":
    print("Current balance: 5163.47 compute units")
    print("Usage rate: {'6.73/hr' if active_count else '0.00/hr'}")
    print("Active assignments: {active_count}")
elif cmd == "sessions":
    if {active_count} > 0:
        print("test-vm | A100 | HIGH_MEM | IDLE")
    else:
        print("[colab] No active sessions found on server.")
elif cmd == "url":
    print("https://colab.research.google.com/drive/mock-session")
elif cmd == "stop":
    print("[colab] Stopped session.")
elif cmd == "download":
    remote_p, local_p = args[-2], args[-1]
    if remote_p.endswith("status.json"):
        open(local_p, "w").write(json.dumps({{"job_id": "job-1", "state": "completed", "exit_code": 0, "gpu_util": "94%", "vram_used": "41.2/79.3 GB"}}))
    else:
        open(local_p, "w").write("Epoch 1/1 done\\nDONE: finished\\n")
elif cmd == "ls":
    print("sample_data")
else:
    print("ok")
""",
        encoding="utf-8",
    )
    mock.chmod(mock.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return mock


class ColabPilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="colab_pilot_test_")
        self.tmp_path = Path(self.tmp.name)
        self.home_dir = self.tmp_path / "home"
        self.data_dir = self.tmp_path / "data"
        self.bin_dir = self.tmp_path / "bin"
        self.home_dir.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)
        self.bin_dir.mkdir(parents=True)
        self.mock_colab = _make_mock_colab(self.bin_dir, active_count=1)

        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home_dir)
        self.env["PLUGIN_DATA"] = str(self.data_dir)
        self.env["COLAB_BIN"] = str(self.mock_colab)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_hook(self, event: str, payload: dict) -> dict:
        res = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "colab_hooks.py"), event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_pre_tool_use_guards(self) -> None:
        for cmd in (
            "colab drivemount -s vm1",
            "colab auth -s vm1",
            "colab pay",
            "colab repl -s vm1",
            "colab ssh -s vm1",
            "colab new -s vm1 --gpu V100",
        ):
            out = self._run_hook("pre-tool-use", {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
            self.assertEqual(out.get("decision"), "deny", f"Expected deny for {cmd!r}")

        big = self.tmp_path / "big.tar"
        with open(big, "wb") as f:
            f.truncate(31 * 1024 * 1024)
        out = self._run_hook(
            "pre-tool-use",
            {"toolCall": {"name": "run_command", "args": {"CommandLine": f"colab upload -s vm1 {big} /content/big.tar"}}},
        )
        self.assertEqual(out.get("decision"), "deny")

        for cmd in (
            "colab usage",
            "colab ssh -s vm1 -- 'nvidia-smi' </dev/null",
            "echo 'colab drivemount'",
            "echo 'x' | colab repl -s vm1",
            "colab new -s vm1 --gpu A100 --high-mem",
        ):
            out = self._run_hook("pre-tool-use", {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
            self.assertEqual(out.get("decision"), "allow", f"Expected allow for {cmd!r}")

    def test_session_start_and_stop_cu_guardrail(self) -> None:
        # Empty sessions -> silent
        self.assertEqual(self._run_hook("session-start", {}), {})
        self.assertEqual(
            self._run_hook("stop", {"terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "continuationActive": False}).get("decision"),
            "stop",
        )

        # Active unleased session -> SessionStart injects summary and Stop blocks with continue
        sess_file = self.home_dir / ".config" / "colab-cli" / "sessions.json"
        sess_file.parent.mkdir(parents=True)
        sess_file.write_text(json.dumps({"test-vm": {"name": "test-vm", "accelerator": "A100", "machine_shape": "HIGH_MEM"}}))

        ss = self._run_hook("session-start", {})
        self.assertIn("injectSteps", ss)

        st = self._run_hook("stop", {"terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "continuationActive": False})
        self.assertEqual(st.get("decision"), "continue")
        self.assertIn("test-vm", st.get("reason", ""))

    def test_mcp_server_protocol_and_tools(self) -> None:
        sess_file = self.home_dir / ".config" / "colab-cli" / "sessions.json"
        sess_file.parent.mkdir(parents=True)
        sess_file.write_text(json.dumps({"test-vm": {"name": "test-vm", "accelerator": "A100", "machine_shape": "HIGH_MEM"}}))

        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://colab-pilot/dashboard.html"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "colab_dashboard", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "colab_keep_warm", "arguments": {"session": "test-vm", "minutes": 20}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "colab_poll_job", "arguments": {"session": "test-vm", "job_id": "job-1"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "colab_stop_all", "arguments": {"session": "test-vm"}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "colab_wakeup_alarm", "arguments": {"action": "self_test"}}},
        ]
        res = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "colab_mcp_server.py"), "--data-dir", str(self.data_dir)],
            input="\n".join(json.dumps(r) for r in reqs) + "\n",
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        by_id = {m["id"]: m for m in (json.loads(ln) for ln in res.stdout.splitlines() if ln.strip())}

        self.assertEqual(len(by_id[2]["result"]["tools"]), 10)
        self.assertEqual(by_id[3]["result"]["contents"][0]["mimeType"], "text/html;profile=mcp-app")
        dash = by_id[4]["result"]["structuredContent"]
        self.assertEqual(dash["usage"]["balance"], 5163.47)
        self.assertEqual(len(dash["sessions"]), 1)
        self.assertTrue(by_id[5]["result"]["structuredContent"]["leased"])
        self.assertEqual(by_id[6]["result"]["structuredContent"]["state"], "completed")
        self.assertIn("test-vm", by_id[7]["result"]["structuredContent"]["stopped"])
        self.assertTrue(by_id[8]["result"]["structuredContent"]["ok"])

    def test_dashboard_nonblocking_while_tool_runs_and_parses_sessions(self) -> None:
        mcp = _load_module("colab_mcp_server", SCRIPTS_DIR / "colab_mcp_server.py")
        parsed = mcp._parse_sessions_output(
            "[train-1] m-s-abc123 | Hardware: A100 | Shape: High-RAM | Variant: GPU\n"
            "[?] m-s-orphan99 | Hardware: H100 | Shape: High-RAM | Variant: GPU\n"
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "train-1")
        self.assertEqual(parsed[1]["name"], "?")
        self.assertIn("m-s-orphan99", mcp._colab_web_url("m-s-orphan99"))

        # Make fake `colab` sleep on `new` to simulate an active GPU allocation
        slow_bin = self.tmp_path / "slow_colab"
        slow_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "if cmd == 'new':\n"
            "    time.sleep(2.5)\n"
            "    print('READY')\n"
            "elif cmd == 'usage':\n"
            "    print('Current balance: 5000.00\\nUsage rate: 12.50/hr\\nActive assignments: 2')\n"
            "elif cmd == 'sessions':\n"
            "    print('[train-1] m-s-abc123 | Hardware: A100 | Shape: High-RAM | Variant: GPU')\n"
            "    print('[?] m-s-orphan99 | Hardware: H100 | Shape: High-RAM | Variant: GPU')\n"
            "elif cmd == 'stop':\n"
            "    print('stopped')\n"
        )
        slow_bin.chmod(0o755)
        env = dict(self.env)
        env["COLAB_BIN"] = str(slow_bin)

        proc = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPTS_DIR / "colab_mcp_server.py"), "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}) + "\n")
            proc.stdin.flush()
            init_resp = json.loads(proc.stdout.readline())
            self.assertEqual(init_resp["id"], 1)

            # Start slow allocation in background
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "colab_allocate_cascade", "arguments": {"session": "slow-vm", "tiers": ["A100"], "require_bf16": False}}}) + "\n")
            proc.stdin.flush()

            # Immediately request dashboard HTML + dashboard telemetry while id=2 is sleeping 2.5s
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://colab-pilot/dashboard.html"}}) + "\n")
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "colab_dashboard", "arguments": {}}}) + "\n")
            proc.stdin.flush()

            first = json.loads(proc.stdout.readline())
            second = json.loads(proc.stdout.readline())
            got = {first["id"]: first, second["id"]: second}
            self.assertEqual(set(got.keys()), {3, 4}, f"Dashboard blocked behind slow tool! got={got}")
            dash = got[4]["result"]["structuredContent"]
            self.assertEqual(len(dash["sessions"]), 2)
            self.assertTrue(any(s["untracked"] and s["name"] == "untracked:m-s-orphan99" for s in dash["sessions"]))
            self.assertTrue(any(op["tool"] == "colab_allocate_cascade" for op in dash["active_operations"]))
        finally:
            proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_watchdog_and_shard_merge(self) -> None:
        wd = _load_module("colab_watchdog", SCRIPTS_DIR / "colab_watchdog.py")
        self.assertEqual(wd.scan_log_state("epoch 1\nDONE: ok", "DONE:")[0], "done")
        self.assertEqual(wd.scan_log_state("CUDA out of memory", "DONE:")[0], "error")
        self.assertEqual(wd.scan_log_state("CUDA out of memory\n[retry] Retrying\nDONE: ok", "DONE:")[0], "done")

        alarm = _load_module("colab_wakeup_alarm", SCRIPTS_DIR / "colab_wakeup_alarm.py")
        chirp = alarm.run_test_chirp("Colab Pilot chirp test", play_audio=False)
        self.assertTrue(chirp["ok"])
        self.assertFalse(chirp["played_audio"])
        self.assertEqual(chirp["phrase"], "Colab Pilot chirp test")
        if sys.platform == "darwin":
            self.assertEqual(chirp["voice"], "Samantha (English (US))")

    def test_deterministic_packaging(self) -> None:
        import zipfile
        pkg = _load_module("package_release", ROOT / "scripts" / "package_release.py")
        d1 = self.tmp_path / "dist1"
        d2 = self.tmp_path / "dist2"
        c1 = pkg.build_release(d1)
        c2 = pkg.build_release(d2)
        self.assertEqual(c1, c2)
        self.assertEqual((d1 / "SHA256SUMS").read_text(), (d2 / "SHA256SUMS").read_text())
        self.assertLess((PLUGIN_DIR / "assets" / "dashboard.html").stat().st_size, 256 * 1024)
        for zname in ("colab-pilot-1.3.2.zip", "colab-pilot-claude.zip", "colab-pilot-codex.zip"):
            with zipfile.ZipFile(d1 / zname) as zf:
                self.assertIn(".mcp.json", zf.namelist(), f".mcp.json missing in {zname}")

    def test_async_drive_stage_transfer_and_concurrent_dashboard(self) -> None:
        rclone_conf = self.home_dir / ".config" / "rclone" / "colab_gdrive_root.conf"
        rclone_conf.parent.mkdir(parents=True, exist_ok=True)
        rclone_conf.write_text("[gdrive_root]\ntype = drive\n", encoding="utf-8")

        fake_bin = self.tmp_path / "fake_rclone_bin"
        fake_bin.mkdir()
        rclone_bin = fake_bin / "rclone"
        rclone_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "print(json.dumps({'stats': {'bytes': 512, 'totalBytes': 1024, 'speed': 512}}), flush=True)\n"
            "time.sleep(1.2)\n"
            "print(json.dumps({'stats': {'bytes': 1024, 'totalBytes': 1024, 'speed': 1024}}), flush=True)\n",
            encoding="utf-8",
        )
        rclone_bin.chmod(0o755)

        payload_file = self.tmp_path / "dataset.tar"
        payload_file.write_bytes(b"x" * 1024)

        env = dict(self.env)
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

        proc = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPTS_DIR / "colab_mcp_server.py"), "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}) + "\n")
            proc.stdin.flush()
            self.assertEqual(json.loads(proc.stdout.readline())["id"], 1)

            t0 = time.monotonic()
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "colab_smart_transfer",
                            "arguments": {
                                "direction": "upload",
                                "mode": "drive_stage",
                                "local_path": str(payload_file),
                                "remote_path": "/content/dataset.tar",
                                "async_transfer": True,
                            },
                        },
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            xfer_resp = json.loads(proc.stdout.readline())["result"]["structuredContent"]
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(xfer_resp["state"], "running")
            xfer_id = xfer_resp["transfer_id"]

            t_dash = time.monotonic()
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "colab_dashboard", "arguments": {}}}) + "\n")
            proc.stdin.flush()
            dash_resp = json.loads(proc.stdout.readline())["result"]["structuredContent"]
            self.assertLess(time.monotonic() - t_dash, 2.0)
            self.assertTrue(any(t["transfer_id"] == xfer_id for t in dash_resp["transfers"]))

            deadline = time.monotonic() + 5.0
            final_state = "running"
            while time.monotonic() < deadline:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "colab_poll_job", "arguments": {"job_id": xfer_id}}}) + "\n")
                proc.stdin.flush()
                poll_resp = json.loads(proc.stdout.readline())["result"]["structuredContent"]
                final_state = poll_resp.get("state", "running")
                if final_state == "completed":
                    break
                time.sleep(0.25)
            self.assertEqual(final_state, "completed")
        finally:
            proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_allocate_cascade_wall_clock_budget_and_400_fast_skip(self) -> None:
        mcp = _load_module("colab_mcp_server", SCRIPTS_DIR / "colab_mcp_server.py")
        self.assertLessEqual(mcp.MAX_CASCADE_BUDGET_SEC, 145.0)
        self.assertLessEqual(mcp.DEFAULT_CASCADE_TIER_TIMEOUT_SEC, 28.0)

        fake_colab = self.tmp_path / "cascade_colab"
        fake_colab.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "args = sys.argv[1:]\n"
            "if 'new' in args:\n"
            "    if 'H100' in args:\n"
            "        sys.stderr.write(\"[colab] Backend rejected accelerator 'H100' (HTTP 400)\\n\")\n"
            "        sys.exit(1)\n"
            "    time.sleep(20)\n"
            "elif 'stop' in args:\n"
            "    print('stopped')\n",
            encoding="utf-8",
        )
        fake_colab.chmod(0o755)
        old_env = {k: os.environ.get(k) for k in ("COLAB_BIN", "COLAB_CASCADE_TIER_TIMEOUT_SEC")}
        os.environ["COLAB_BIN"] = str(fake_colab)
        os.environ["COLAB_CASCADE_TIER_TIMEOUT_SEC"] = "1.5"
        try:
            t0 = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                mcp.tool_colab_allocate_cascade(
                    self.data_dir,
                    {"session": "s1", "tiers": ["H100", "A100", "L4"], "max_wall_seconds": 5.0},
                )
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 6.5)
            msg = str(ctx.exception)
            self.assertIn('"rejected": true', msg)
            self.assertIn('"timed_out": true', msg)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_keepalive_daemon_idempotent_pings_autostop_and_exit(self) -> None:
        mcp = _load_module("colab_mcp_server", SCRIPTS_DIR / "colab_mcp_server.py")
        log_path = self.tmp_path / "ka_calls.log"
        fake_colab = self.tmp_path / "ka_colab"
        status_src = self.tmp_path / "remote_status.json"
        status_src.write_text(json.dumps({"job_id": "job-ka", "state": "completed", "exit_code": 0}), encoding="utf-8")

        fake_colab.write_text(
            f"""#!/usr/bin/env python3
import shutil, sys
args = sys.argv[1:]
with open({str(log_path)!r}, "a") as f:
    f.write(" ".join(args) + "\\n")
if args and args[0] == "download":
    shutil.copyfile({str(status_src)!r}, args[-1])
elif args and args[0] == "exec":
    print('KA_CPU:{{"cpu_pct": 12.5, "cpu_count": 12, "load_1m": 0.42}}')
""",
            encoding="utf-8",
        )
        fake_colab.chmod(0o755)

        sess_file = self.home_dir / ".config" / "colab-cli" / "sessions.json"
        sess_file.parent.mkdir(parents=True, exist_ok=True)
        sess_file.write_text(json.dumps({"ka-vm": {"name": "ka-vm"}}), encoding="utf-8")

        old_env = {k: os.environ.get(k) for k in ("HOME", "COLAB_BIN", "COLAB_KEEPALIVE_INTERVAL_SEC")}
        os.environ["HOME"] = str(self.home_dir)
        os.environ["COLAB_BIN"] = str(fake_colab)
        os.environ["COLAB_KEEPALIVE_INTERVAL_SEC"] = "0.2"
        pid = 0
        try:
            r1 = mcp.tool_colab_keep_warm(self.data_dir, {"session": "ka-vm", "minutes": 0.03})["structuredContent"]
            r2 = mcp.tool_colab_keep_warm(self.data_dir, {"session": "ka-vm", "minutes": 0.03})["structuredContent"]
            pid = int(r1["keepalive_pid"])
            self.assertEqual(pid, int(r2["keepalive_pid"]))

            # Register a completed detached job with auto_stop_vm=True, then clear lease so reaper triggers stop
            mcp._write_json(
                self.data_dir / "detached_jobs.json",
                {"job-ka": {"job_id": "job-ka", "session": "ka-vm", "state": "running", "auto_stop_vm": True, "started_at": time.time()}},
            )
            time.sleep(0.7)
            mcp.tool_colab_keep_warm(self.data_dir, {"session": "ka-vm", "clear": True})

            deadline = time.monotonic() + 5.0
            stopped_ok = False
            exited_ok = False
            while time.monotonic() < deadline:
                calls = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
                jobs = mcp._read_json(self.data_dir / "detached_jobs.json", {})
                if "stop -s ka-vm" in calls and jobs.get("job-ka", {}).get("vm_auto_stopped"):
                    stopped_ok = True
                if not (self.data_dir / "keepalive_daemon.json").exists():
                    exited_ok = True
                if stopped_ok and exited_ok:
                    break
                time.sleep(0.15)

            calls = log_path.read_text(encoding="utf-8")
            self.assertIn("ls -s ka-vm content", calls)
            self.assertIn("exec -s ka-vm", calls)
            self.assertTrue(stopped_ok, f"Expected auto_stop_vm to run stop; calls={calls!r}")
            self.assertTrue(exited_ok, "Expected keepalive daemon to self-terminate after leases & jobs cleared")
            vm_telem = mcp._read_json(self.data_dir / "vm_telemetry.json", {})
            self.assertEqual(vm_telem.get("ka-vm", {}).get("cpu_pct"), 12.5)
            self.assertEqual(vm_telem.get("ka-vm", {}).get("cpu_count"), 12)
        finally:
            if pid > 0:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_detached_wrapper_incremental_drive_checkpoint_mirroring(self) -> None:
        mcp = _load_module("colab_mcp_server", SCRIPTS_DIR / "colab_mcp_server.py")
        wrapper_path = self.tmp_path / "_wrapper.py"
        wrapper_path.write_text(mcp.build_detached_wrapper_source(), encoding="utf-8")

        sync_dir = self.tmp_path / "out"
        sync_dir.mkdir()
        job_dir = self.tmp_path / "job"
        job_dir.mkdir()

        fake_bin = self.tmp_path / "rclone_wrap_bin"
        fake_bin.mkdir()
        rclone_log = self.tmp_path / "rclone_sync.log"
        rclone_bin = fake_bin / "rclone"
        rclone_bin.write_text(
            f"""#!/usr/bin/env python3
import sys
with open({str(rclone_log)!r}, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
""",
            encoding="utf-8",
        )
        rclone_bin.chmod(0o755)

        rclone_conf = self.tmp_path / "rclone.conf"
        rclone_conf.write_text("[gdrive_root]\ntype = drive\n", encoding="utf-8")
        train_script = self.tmp_path / "train.py"
        train_script.write_text(
            f"from pathlib import Path\n"
            f"(Path({str(sync_dir)!r}) / 'best.pt').write_bytes(b'weights')\n"
            f"(Path({str(sync_dir)!r}) / 'skip.bin').write_bytes(b'ignored')\n"
            f"print('done')\n",
            encoding="utf-8",
        )
        wrap_env = dict(self.env)
        wrap_env["PATH"] = f"{fake_bin}:{wrap_env.get('PATH', '')}"
        wrap_env["RCLONE_CONFIG"] = str(rclone_conf)
        res = subprocess.run(
            [
                sys.executable,
                "-B",
                str(wrapper_path),
                str(job_dir),
                str(train_script),
                "[]",
                "job-sync",
                str(sync_dir),
                "gdrive_root:ckpts/run1",
                json.dumps(["*.pt", "*.json"]),
            ],
            capture_output=True,
            text=True,
            env=wrap_env,
            timeout=20,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        st = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(st["state"], "completed")
        self.assertEqual(st["exit_code"], 0)
        self.assertIsNotNone(st["last_drive_sync_ts"])
        self.assertEqual(st["synced_file_count"], 1)
        self.assertIn("cpu_count", st)
        self.assertIn("cpu_pct", st)
        rc_calls = rclone_log.read_text(encoding="utf-8")
        self.assertIn("copy ", rc_calls)
        self.assertIn("--include *.pt", rc_calls)
        self.assertIn("copyto ", rc_calls)

    def test_dashboard_http_resources_telemetry_zero_kernel_exec(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        mcp = _load_module("colab_mcp_server", SCRIPTS_DIR / "colab_mcp_server.py")
        mcp._RESOURCES_MEM_CACHE.clear()
        hits = {"resources": 0, "kernels": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/api/colab/resources":
                    hits["resources"] += 1
                    payload = {
                        "memory": {"totalBytes": 16 * 1024**3, "freeBytes": 12 * 1024**3},
                        "disks": [{"filesystem": {"label": "kernel", "totalBytes": 100 * 1024**3, "usedBytes": 25 * 1024**3}}],
                        "gpus": [{"name": "NVIDIA A100-SXM4-80GB", "memoryUsedBytes": 20 * 1024**3, "memoryTotalBytes": 80 * 1024**3, "gpuUtilization": 0.72, "memoryUtilization": 0.45, "everUsed": True}],
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write((")]}'\n" + json.dumps(payload)).encode("utf-8"))
                elif self.path == "/api/kernels":
                    hits["kernels"] += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps([{"id": "k1", "execution_state": "idle"}]).encode("utf-8"))
                else:
                    self.send_response(404)
                    self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv_th = threading.Thread(target=srv.serve_forever, daemon=True)
        srv_th.start()

        exec_log = self.tmp_path / "dash_colab_calls.log"
        dash_colab = self.tmp_path / "dash_colab"
        dash_colab.write_text(
            f"""#!/usr/bin/env python3
import sys
args = sys.argv[1:]
with open({str(exec_log)!r}, "a") as f:
    f.write(" ".join(args) + "\\n")
cmd = args[0] if args else ""
if cmd == "usage":
    print("Current balance: 5000.00 compute units\\nUsage rate: 10.99/hr\\nActive assignments: 2")
elif cmd == "sessions":
    print("[vm-live] ep-live | Hardware: A100 | Shape: High-RAM | Variant: GPU")
    print("[vm-down] ep-down | Hardware: T4 | Shape: Standard | Variant: GPU")
""",
            encoding="utf-8",
        )
        dash_colab.chmod(0o755)

        sess_file = self.home_dir / ".config" / "colab-cli" / "sessions.json"
        sess_file.parent.mkdir(parents=True, exist_ok=True)
        sess_file.write_text(
            json.dumps(
                {
                    "vm-live": {"name": "vm-live", "endpoint": "ep-live", "url": f"http://127.0.0.1:{srv.server_address[1]}", "token": "tok-1", "accelerator": "A100", "machine_shape": "HIGH_RAM"},
                    "vm-down": {"name": "vm-down", "endpoint": "ep-down", "url": "http://127.0.0.1:9", "token": "tok-2", "accelerator": "T4", "machine_shape": "STANDARD"},
                }
            ),
            encoding="utf-8",
        )
        mcp._write_json(self.data_dir / "vm_telemetry.json", {"vm-live": {"cpu_pct": 23.4, "cpu_count": 12, "load_1m": 1.15, "sampled_at": time.time(), "source": "keepalive"}})

        old_env = {k: os.environ.get(k) for k in ("HOME", "COLAB_BIN")}
        os.environ["HOME"] = str(self.home_dir)
        os.environ["COLAB_BIN"] = str(dash_colab)
        try:
            t0 = time.monotonic()
            dash1 = mcp.tool_colab_dashboard(self.data_dir, {})["structuredContent"]
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 3.0)
            by_name = {s["name"]: s for s in dash1["sessions"]}
            telem = by_name["vm-live"]["telemetry"]
            self.assertIsNotNone(telem)
            self.assertEqual(telem["ram_used_gb"], 4.0)
            self.assertEqual(telem["ram_total_gb"], 16.0)
            self.assertEqual(telem["vram_total_gb"], 80.0)
            self.assertEqual(telem["gpu_util_pct"], 72.0)
            self.assertEqual(telem["disk_used_gb"], 25.0)
            self.assertEqual(telem["kernel_state"], "idle")
            self.assertEqual(telem["cpu_pct"], 23.4)
            self.assertIsNone(by_name["vm-down"]["telemetry"])
            self.assertEqual(hits["resources"], 1)

            # Second immediate call hits 8s memory cache
            dash2 = mcp.tool_colab_dashboard(self.data_dir, {})["structuredContent"]
            self.assertEqual(hits["resources"], 1)
            self.assertEqual({s["name"]: s for s in dash2["sessions"]}["vm-live"]["telemetry"]["gpu_util_pct"], 72.0)

            calls = exec_log.read_text(encoding="utf-8")
            self.assertNotIn("exec", calls)
            self.assertNotIn("ssh", calls)
        finally:
            srv.shutdown()
            srv.server_close()
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_mcp_json_and_hooks_work_for_both_claude_and_codex_roots(self) -> None:
        mcp_cfg = json.loads((PLUGIN_DIR / ".mcp.json").read_text(encoding="utf-8"))
        hooks_cfg = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        pilot = mcp_cfg["mcpServers"]["pilot"]
        raw_cmd = pilot["args"][1]
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT:-${", raw_cmd)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT:-${", json.dumps(hooks_cfg))

        for mode in ("codex_subst", "claude_env"):
            env = {k: v for k, v in self.env.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA")}
            args = list(pilot["args"])
            if mode == "codex_subst":
                args = [
                    a.replace("${PLUGIN_ROOT}", str(PLUGIN_DIR)).replace("${PLUGIN_DATA}", str(self.data_dir))
                    for a in args
                ]
            else:
                env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)
                env["CLAUDE_PLUGIN_DATA"] = str(self.data_dir)

            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
            res = subprocess.run(
                [pilot["command"], *args],
                input="\n".join(json.dumps(r) for r in reqs) + "\n",
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, f"{mode}: {res.stderr}")
            by_id = {m["id"]: m for m in (json.loads(ln) for ln in res.stdout.splitlines() if ln.strip())}
            self.assertEqual(by_id[1]["result"]["serverInfo"]["version"], "1.3.2")
            tool_names = [t["name"] for t in by_id[2]["result"]["tools"]]
            self.assertIn("colab_dashboard", tool_names)


if __name__ == "__main__":
    unittest.main()

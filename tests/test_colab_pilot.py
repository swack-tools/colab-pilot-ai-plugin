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

    def test_watchdog_and_shard_merge(self) -> None:
        wd = _load_module("colab_watchdog", SCRIPTS_DIR / "colab_watchdog.py")
        self.assertEqual(wd.scan_log_state("epoch 1\nDONE: ok", "DONE:")[0], "done")
        self.assertEqual(wd.scan_log_state("CUDA out of memory", "DONE:")[0], "error")
        self.assertEqual(wd.scan_log_state("CUDA out of memory\n[retry] Retrying\nDONE: ok", "DONE:")[0], "done")

    def test_deterministic_packaging(self) -> None:
        pkg = _load_module("package_release", ROOT / "scripts" / "package_release.py")
        d1 = self.tmp_path / "dist1"
        d2 = self.tmp_path / "dist2"
        c1 = pkg.build_release(d1)
        c2 = pkg.build_release(d2)
        self.assertEqual(c1, c2)
        self.assertEqual((d1 / "SHA256SUMS").read_text(), (d2 / "SHA256SUMS").read_text())


if __name__ == "__main__":
    unittest.main()

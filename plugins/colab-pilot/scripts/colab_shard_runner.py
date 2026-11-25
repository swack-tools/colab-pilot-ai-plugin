#!/usr/bin/env python3
"""Parallel CPU/GPU shard orchestrator for Google Colab VMs.

Uses isolated `--config /tmp/colab_shard_<run>_<i>.json` per worker so parallel
`colab new`/`exec`/`download`/`stop` processes never contend on
~/.config/colab-cli/sessions.json. Guarantees cleanup in `finally`.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence


def _resolve_colab_bin(explicit: str) -> str:
    if explicit and os.path.exists(explicit):
        return explicit
    for cand in ("/Users/allen/.local/bin/colab", shutil.which("colab") or ""):
        if cand and os.path.exists(cand):
            return cand
    raise FileNotFoundError("Could not find `colab` CLI binary")


def _run_worker(
    idx: int,
    total: int,
    run_id: str,
    args: argparse.Namespace,
    colab_bin: str,
    rclone_b64: str,
    out_dir: Path,
) -> dict[str, object]:
    sess = f"{args.prefix}-{idx}"
    cfg_path = f"/tmp/colab_shard_{run_id}_{idx}.json"
    log_path = out_dir / f"shard_{idx}.log"
    shard_out = out_dir / f"shard_{idx}{Path(args.remote_output).suffix or '.tar'}"

    def _c(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [colab_bin, "--config", cfg_path, *cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    started = False
    try:
        new_cmd = ["new", "-s", sess]
        if args.gpu and args.gpu.upper() != "NONE":
            new_cmd.extend(["--gpu", args.gpu.upper()])

        res_new = _c(new_cmd, timeout=300)
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"[shard {idx}] colab {' '.join(new_cmd)} -> rc={res_new.returncode}\n")
            lf.write(res_new.stdout + res_new.stderr + "\n")
        if res_new.returncode != 0:
            return {"idx": idx, "session": sess, "ok": False, "error": f"new failed: {res_new.stderr.strip()}"}
        started = True

        exec_cmd = [
            "exec",
            "-s",
            sess,
            "--timeout",
            "0",
            "--env",
            f"SHARD_INDEX={idx}",
            "--env",
            f"SHARD_TOTAL={total}",
        ]
        if rclone_b64:
            exec_cmd.extend(["--env", f"RCLONE_CONF_B64={rclone_b64}"])
        for kv in args.env:
            exec_cmd.extend(["--env", kv])
        exec_cmd.extend(["-f", args.script])

        res_exec = _c(exec_cmd, timeout=args.timeout_sec)
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"[shard {idx}] exec rc={res_exec.returncode}\n")
            lf.write(res_exec.stdout + res_exec.stderr + "\n")
        if res_exec.returncode != 0:
            return {"idx": idx, "session": sess, "ok": False, "error": f"exec failed (rc={res_exec.returncode})"}

        if args.remote_output:
            res_dl = _c(["download", "-s", sess, args.remote_output, str(shard_out)], timeout=600)
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"[shard {idx}] download {args.remote_output} -> rc={res_dl.returncode}\n")
            if res_dl.returncode != 0 or not shard_out.is_file():
                return {"idx": idx, "session": sess, "ok": False, "error": f"download failed: {res_dl.stderr.strip()}"}

        return {
            "idx": idx,
            "session": sess,
            "ok": True,
            "output_path": str(shard_out) if args.remote_output else None,
            "log_path": str(log_path),
        }
    finally:
        if started and not args.keep:
            try:
                _c(["stop", "-s", sess], timeout=60)
            except Exception:
                pass
        try:
            if os.path.exists(cfg_path):
                os.remove(cfg_path)
        except OSError:
            pass


def merge_tar_shards(shard_paths: list[Path], merged_tar: Path) -> dict[str, object]:
    """Merge shard tar archives (and optional extract_manifest.json) into one tar."""
    train_movies: list[dict] = []
    eval_movies: list[dict] = []
    cfg: dict = {}
    has_manifest = False

    merged_tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(merged_tar, "w") as dst:
        for sp in sorted(shard_paths):
            with tarfile.open(sp, "r") as src:
                try:
                    m_mem = src.extractfile("extract_manifest.json")
                except KeyError:
                    m_mem = None
                if m_mem is not None:
                    has_manifest = True
                    m = json.load(m_mem)
                    if not cfg:
                        cfg = m.get("config", {})
                    train_movies.extend(m.get("train_movies", []))
                    eval_movies.extend(m.get("eval_movies", []))
                for mem in src.getmembers():
                    if mem.name == "extract_manifest.json":
                        continue
                    fobj = src.extractfile(mem) if mem.isfile() else None
                    dst.addfile(mem, fobj)
        if has_manifest:
            combined = {
                "version": 1,
                "run_id": uuid.uuid4().hex[:12],
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "complete",
                "config": cfg,
                "train_movies": train_movies,
                "eval_movies": eval_movies,
            }
            raw = json.dumps(combined, indent=2).encode("utf-8")
            ti = tarfile.TarInfo("extract_manifest.json")
            ti.size = len(raw)
            dst.addfile(ti, io.BytesIO(raw))
    return {
        "merged_tar": str(merged_tar),
        "shards": len(shard_paths),
        "has_manifest": has_manifest,
        "train_movies": len(train_movies),
        "eval_movies": len(eval_movies),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parallel CPU/GPU shard orchestrator for Colab")
    p.add_argument("--workers", type=int, default=5, help="Number of parallel Colab worker VMs")
    p.add_argument("--prefix", default="colab-shard", help="Session name prefix (<prefix>-0..N-1)")
    p.add_argument("--script", required=True, help="Local Python script to execute on each shard")
    p.add_argument("--gpu", default="NONE", help="GPU tier (default NONE = CPU-only ~0.08 CU/hr)")
    p.add_argument("--rclone-conf", default="", help="Optional rclone.conf path to forward as RCLONE_CONF_B64")
    p.add_argument("--env", action="append", default=[], help="Extra KEY=VAL env vars (repeatable)")
    p.add_argument("--remote-output", default="", help="Optional remote file path to download from each shard")
    p.add_argument("--out-dir", default="/tmp/colab_shards", help="Local directory for shard logs and outputs")
    p.add_argument("--merge-tar", default="", help="Optional path to merge downloaded shard .tar files into")
    p.add_argument("--timeout-sec", type=int, default=3600)
    p.add_argument("--colab-bin", default="/Users/allen/.local/bin/colab")
    p.add_argument("--keep", action="store_true", help="Keep worker VMs alive after finishing")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not os.path.isfile(args.script):
        print(f"ERROR: Script not found: {args.script}", file=sys.stderr)
        return 2

    colab_bin = _resolve_colab_bin(args.colab_bin)
    rclone_b64 = ""
    if args.rclone_conf:
        rc_p = Path(args.rclone_conf).expanduser()
        if not rc_p.is_file():
            print(f"ERROR: rclone.conf not found: {rc_p}", file=sys.stderr)
            return 2
        rclone_b64 = base64.b64encode(rc_p.read_bytes()).decode("ascii")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = [
            pool.submit(_run_worker, i, args.workers, run_id, args, colab_bin, rclone_b64, out_dir)
            for i in range(args.workers)
        ]
        for fut in as_completed(futs):
            results.append(fut.result())

    results.sort(key=lambda r: int(r["idx"]))
    failed = [r for r in results if not r.get("ok")]

    merge_info = None
    if not failed and args.merge_tar and args.remote_output:
        shard_paths = [Path(str(r["output_path"])) for r in results if r.get("output_path")]
        merge_info = merge_tar_shards(shard_paths, Path(args.merge_tar))

    usage_res = subprocess.run([colab_bin, "usage"], capture_output=True, text=True, timeout=15)
    summary = {
        "ok": len(failed) == 0,
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "workers": args.workers,
        "gpu": args.gpu,
        "results": results,
        "merged": merge_info,
        "usage": usage_res.stdout.strip(),
    }
    print(json.dumps(summary, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build deterministic release archives (.zip, .plugin, .tar.gz, SHA256SUMS)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "colab-pilot"
FIXED_ZIP_DT = (2026, 1, 1, 0, 0, 0)
FORBIDDEN_PARTS = {"__pycache__", ".DS_Store", ".data", ".env", "node_modules"}


def _iter_plugin_files() -> list[Path]:
    files: list[Path] = []
    for p in sorted(PLUGIN_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(PLUGIN_DIR)
        if any(part in FORBIDDEN_PARTS or part.endswith(".pyc") for part in rel.parts):
            continue
        files.append(p)
    return files


def _file_mode(rel: Path) -> int:
    if rel.parts[0] == "scripts" and rel.suffix == ".py":
        return 0o755
    return 0o644


def build_release(out_dir: Path) -> dict[str, str]:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    name = str(manifest["name"])
    version = str(manifest["version"])

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{name}-{version}.zip"
    plugin_path = out_dir / f"{name}-{version}.plugin"
    tgz_path = out_dir / f"{name}-{version}.tar.gz"
    sums_path = out_dir / "SHA256SUMS"

    files = _iter_plugin_files()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            rel = p.relative_to(PLUGIN_DIR)
            zi = zipfile.ZipInfo(rel.as_posix(), date_time=FIXED_ZIP_DT)
            mode = _file_mode(rel)
            zi.external_attr = (0o100000 | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, p.read_bytes())

    shutil.copyfile(zip_path, plugin_path)

    with open(tgz_path, "wb") as raw_f:
        with gzip.GzipFile(fileobj=raw_f, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tf:
                for p in files:
                    rel = p.relative_to(PLUGIN_DIR)
                    data = p.read_bytes()
                    ti = tarfile.TarInfo(f"{name}/{rel.as_posix()}")
                    ti.size = len(data)
                    ti.mode = _file_mode(rel)
                    ti.mtime = 0
                    ti.uid = 0
                    ti.gid = 0
                    ti.uname = ""
                    ti.gname = ""
                    tf.addfile(ti, io.BytesIO(data))

    checksums: dict[str, str] = {}
    lines: list[str] = []
    for artifact in sorted((zip_path, plugin_path, tgz_path), key=lambda a: a.name):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        checksums[artifact.name] = digest
        lines.append(f"{digest}  {artifact.name}\n")
    sums_path.write_text("".join(lines), encoding="utf-8")
    return checksums


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (ROOT / "dist")
    checksums = build_release(out_dir)
    print(json.dumps({"out_dir": str(out_dir), "artifacts": checksums}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

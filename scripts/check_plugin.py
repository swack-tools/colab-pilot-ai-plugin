#!/usr/bin/env python3
"""Offline package contract and schema checks for colab-pilot (stdlib only)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "colab-pilot"
EXPECTED_SKILLS = {"colab-pilot", "colab-train", "colab-shard-cpu", "colab-watch"}
FORBIDDEN = {".DS_Store", "Thumbs.db", "__pycache__", ".env", ".data", "node_modules"}
errors: list[str] = []


def check(cond: object, msg: str) -> None:
    if not cond:
        errors.append(msg)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"{path.relative_to(ROOT)}: {exc}")
        return {}


agy = load_json(PLUGIN / ".antigravity-plugin" / "plugin.json")
claude = load_json(PLUGIN / ".claude-plugin" / "plugin.json")
codex = load_json(PLUGIN / ".codex-plugin" / "plugin.json")

check(not (PLUGIN / "plugin.json").exists(), "root plugin.json must not exist alongside multi-client manifests")
check(agy.get("$schema") == "https://antigravity.google/schemas/v1/plugin.json", ".antigravity-plugin/plugin.json: invalid $schema")
check(agy.get("displayName") == "Colab Pilot", ".antigravity-plugin/plugin.json: displayName must be 'Colab Pilot'")

for label, m in (("antigravity", agy), ("claude", claude), ("codex", codex)):
    check(m.get("name") == "colab-pilot", f"{label}: name must be 'colab-pilot'")
    check(re.fullmatch(r"\d+\.\d+\.\d+", str(m.get("version", ""))) is not None, f"{label}: version must be semver")
    check(m.get("version") == agy.get("version"), f"{label}: version mismatch")
    check(str(m.get("repository", "")).startswith("https://"), f"{label}: repository must be https:// URL")

for png_rel in ("assets/icon.png", "assets/logo.png"):
    pp = PLUGIN / png_rel
    check(pp.is_file() and pp.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{png_rel} missing or invalid PNG")

logo_path = PLUGIN / "assets" / "logo.svg"
check(logo_path.is_file() and "<svg" in logo_path.read_text(encoding="utf-8"), "assets/logo.svg missing or invalid")

dash_path = PLUGIN / "assets" / "dashboard.html"
check(dash_path.is_file() and 10_000 < dash_path.stat().st_size < 10 * 1024 * 1024, "assets/dashboard.html missing or bad size")

check((PLUGIN / "rules" / "AGENTS.md").is_file(), "rules/AGENTS.md missing")
agent_path = PLUGIN / "agents" / "colab-operator.md"
check(agent_path.is_file() and "name: colab-operator" in agent_path.read_text(encoding="utf-8"), "agents/colab-operator.md invalid")

agy_hooks = load_json(PLUGIN / "hooks.json")
check(list(agy_hooks) == ["colab-pilot"], f"hooks.json: expected ['colab-pilot'], got {list(agy_hooks)}")
cc_hooks = load_json(PLUGIN / "hooks" / "hooks.json")
check(isinstance(cc_hooks.get("hooks"), dict) and set(cc_hooks["hooks"]) == {"SessionStart", "PreToolUse", "Stop"}, "hooks/hooks.json invalid")

for mcp_rel in ("mcp_config.json", ".mcp.json"):
    mcp = load_json(PLUGIN / mcp_rel)
    check("pilot" in mcp.get("mcpServers", {}), f"{mcp_rel}: missing 'pilot' server")

found_skills = {p.name for p in (PLUGIN / "skills").iterdir() if p.is_dir()}
check(found_skills == EXPECTED_SKILLS, f"skills mismatch: {sorted(found_skills)} != {sorted(EXPECTED_SKILLS)}")
for name in sorted(found_skills):
    sp = PLUGIN / "skills" / name / "SKILL.md"
    check(sp.is_file(), f"skills/{name}/SKILL.md missing")
    if sp.is_file():
        m = re.match(r"---\nname:\s*(.+)\ndescription:", sp.read_text(encoding="utf-8"))
        check(m is not None and m.group(1).strip() == name, f"skills/{name}/SKILL.md frontmatter name mismatch")

scripts = sorted((PLUGIN / "scripts").glob("*.py"))
check(len(scripts) == 5, f"expected 5 scripts, found {len(scripts)}")
for sc in scripts:
    check((sc.stat().st_mode & 0o111) != 0, f"{sc.relative_to(ROOT)}: must be executable (0755)")

for p in PLUGIN.rglob("*"):
    rel = p.relative_to(PLUGIN)
    check(not p.is_symlink(), f"symlink forbidden: {rel}")
    check(not any(part in FORBIDDEN or part.startswith("._") or part.endswith(".pyc") for part in rel.parts), f"forbidden file/dir: {rel}")

if errors:
    print("\n".join(f"ERROR: {e}" for e in errors), file=sys.stderr)
    sys.exit(1)

print(f"OK: colab-pilot v{agy.get('version')} (3 client manifests, {len(found_skills)} skills, {len(scripts)} scripts, hooks, mcp, UI)")

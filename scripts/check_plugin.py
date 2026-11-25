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


manifest = load_json(PLUGIN / "plugin.json")
check(manifest.get("$schema") == "https://antigravity.google/schemas/v1/plugin.json", "plugin.json: missing/invalid $schema")
check(manifest.get("name") == "colab-pilot", "plugin.json: name must be 'colab-pilot'")
check(manifest.get("displayName") == "Colab Pilot", "plugin.json: displayName must be 'Colab Pilot'")
check(re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", ""))) is not None, "plugin.json: version must be semver")
desc = str(manifest.get("description", ""))
check(50 <= len(desc) <= 220, f"plugin.json: description length {len(desc)} out of bounds")
prompts = manifest.get("suggestedPrompts", [])
check(isinstance(prompts, list) and 1 <= len(prompts) <= 3, "plugin.json: suggestedPrompts must have 1..3 entries")
check(str(manifest.get("repository", "")).startswith("https://"), "plugin.json: repository must be https:// URL")

logo_rel = str(manifest.get("logo", ""))
check(logo_rel == "assets/logo.svg", f"plugin.json: unexpected logo {logo_rel!r}")
logo_path = PLUGIN / logo_rel
check(logo_path.is_file() and "<svg" in logo_path.read_text(encoding="utf-8"), "assets/logo.svg missing or invalid")

dash_path = PLUGIN / "assets" / "dashboard.html"
check(dash_path.is_file(), "assets/dashboard.html missing")
if dash_path.is_file():
    size = dash_path.stat().st_size
    check(10_000 < size < 10 * 1024 * 1024, f"assets/dashboard.html size out of bounds: {size}")

check((PLUGIN / "rules" / "AGENTS.md").is_file(), "rules/AGENTS.md missing")

agent_path = PLUGIN / "agents" / "colab-operator.md"
check(agent_path.is_file(), "agents/colab-operator.md missing")
if agent_path.is_file():
    atext = agent_path.read_text(encoding="utf-8")
    check(atext.startswith("---\n") and "name: colab-operator" in atext, "agents/colab-operator.md missing valid frontmatter")

hooks = load_json(PLUGIN / "hooks.json")
check(list(hooks) == ["colab-pilot"], f"hooks.json: expected top-level key ['colab-pilot'], got {list(hooks)}")
group = hooks.get("colab-pilot", {})
for ev in ("SessionStart", "Stop"):
    entries = group.get(ev, [])
    check(isinstance(entries, list) and len(entries) >= 1, f"hooks.json: {ev} must be non-empty list")
    for h in entries:
        cmd = str(h.get("command", ""))
        check("./scripts/colab_hooks.py" in cmd, f"hooks.json {ev}: unexpected command {cmd!r}")
for grp in group.get("PreToolUse", []):
    check("matcher" in grp and isinstance(grp.get("hooks"), list), "hooks.json PreToolUse: missing matcher/hooks")

mcp = load_json(PLUGIN / "mcp_config.json")
servers = mcp.get("mcpServers", {})
check("pilot" in servers, "mcp_config.json: missing 'pilot' server")
if "pilot" in servers:
    srv = servers["pilot"]
    check("env" not in srv or ("PLUGIN_ROOT" not in srv["env"] and "PLUGIN_DATA" not in srv["env"]),
          "mcp_config.json: must not duplicate PLUGIN_ROOT/PLUGIN_DATA in env")

found_skills = {p.name for p in (PLUGIN / "skills").iterdir() if p.is_dir()}
check(found_skills == EXPECTED_SKILLS, f"skills mismatch: {sorted(found_skills)} != {sorted(EXPECTED_SKILLS)}")
for name in sorted(found_skills):
    sp = PLUGIN / "skills" / name / "SKILL.md"
    check(sp.is_file(), f"skills/{name}/SKILL.md missing")
    if sp.is_file():
        st = sp.read_text(encoding="utf-8")
        m = re.match(r"---\nname:\s*(.+)\ndescription:", st)
        check(m is not None and m.group(1).strip() == name, f"skills/{name}/SKILL.md frontmatter name mismatch")

scripts = sorted((PLUGIN / "scripts").glob("*.py"))
check(len(scripts) == 5, f"expected 5 scripts, found {len(scripts)}")
for sc in scripts:
    check((sc.stat().st_mode & 0o111) != 0, f"{sc.relative_to(ROOT)}: must be executable (0755)")

for p in PLUGIN.rglob("*"):
    rel = p.relative_to(PLUGIN)
    check(not p.is_symlink(), f"symlink forbidden: {rel}")
    check(not any(part in FORBIDDEN or part.startswith("._") or part.endswith(".pyc") for part in rel.parts),
          f"forbidden file/dir: {rel}")

if errors:
    print("\n".join(f"ERROR: {e}" for e in errors), file=sys.stderr)
    sys.exit(1)

print(f"OK: colab-pilot v{manifest.get('version')} ({len(found_skills)} skills, {len(scripts)} scripts, hooks, mcp, UI)")

#!/usr/bin/env python3
"""Render the Colab Pilot documentation site into an empty output directory; stdlib only."""
import argparse
from html import escape
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit

BASE_URL = os.environ.get("DOCS_BASE_URL", "https://colab.ai.swacktech.com").rstrip("/")
if (
    urlsplit(BASE_URL).scheme != "https"
    or not urlsplit(BASE_URL).hostname
    or urlsplit(BASE_URL).path
    or urlsplit(BASE_URL).query
    or urlsplit(BASE_URL).fragment
    or urlsplit(BASE_URL).username
    or urlsplit(BASE_URL).password
):
    raise SystemExit("DOCS_BASE_URL must be an HTTPS origin")

CNAME_HOST = urlsplit(BASE_URL).hostname
ROOT = Path(__file__).resolve().parent
PAGES = {
    "index": (
        "Colab Pilot — Zero-waste Google Colab orchestration",
        "Orchestrate Google Colab GPUs and CPU shards from Antigravity, Claude Code, and Codex with zero wasted Compute Units.",
    ),
    "install": (
        "Install and connect — Colab Pilot",
        "Install Colab Pilot in Antigravity, Claude Code, Claude Desktop, and Codex, and configure headless Google Drive staging.",
    ),
    "workflows": (
        "Workflows and guardrails — Colab Pilot",
        "Run the 6-phase zero-waste GPU training protocol, parallel CPU sharding, detached watchdogs, and OOM recovery.",
    ),
    "reference": (
        "Technical reference — Colab Pilot",
        "Complete reference for all 10 pilot MCP tools, lifecycle hooks, interactive dashboard UI, and release verification.",
    ),
}

NAV_ITEMS = [
    ("index", "Overview"),
    ("install", "Install"),
    ("workflows", "Workflows"),
    ("reference", "Reference"),
]


def render_shell(slug: str, title: str, description: str, body: str) -> str:
    nav_links = "".join(
        f'<a href="{page}.html"'
        + (' aria-current="page"' if page == slug else "")
        + f">{escape(label)}</a>"
        for page, label in NAV_ITEMS
    )
    canonical_suffix = "" if slug == "index" else f"{slug}.html"
    canonical_url = f"{BASE_URL}/{canonical_suffix}"
    return (
        f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<meta name="description" content="{escape(description, quote=True)}">'
        f'<meta name="theme-color" content="#f7f6f1">'
        f'<meta property="og:title" content="{escape(title, quote=True)}">'
        f'<meta property="og:description" content="{escape(description, quote=True)}">'
        f'<meta property="og:type" content="website">'
        f'<meta property="og:url" content="{escape(canonical_url, quote=True)}">'
        f'<link rel="canonical" href="{escape(canonical_url, quote=True)}">'
        f'<link rel="icon" href="assets/favicon.svg" type="image/svg+xml">'
        f'<link rel="stylesheet" href="assets/style.css">'
        f'<script src="assets/site.js" defer></script></head><body>'
        f'<a class="skip" href="#main">Skip to content</a>'
        f'<header><div class="header-inner">'
        f'<a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true">⚡</span>Colab Pilot</a>'
        f'<nav aria-label="Main navigation">{nav_links}'
        f'<a href="https://ai.swacktech.com/plugins/colab-pilot/">Marketplace ↗</a>'
        f'<a href="https://github.com/swack-tools/colab-pilot-ai-plugin">GitHub ↗</a></nav>'
        f"</div></header>"
        f'<main id="main">{body}</main>'
        f'<footer><div><a class="brand" href="index.html">Colab Pilot</a>'
        f"<p>Zero-waste Google Colab orchestration for AI coding agents.</p></div>"
        f'<p><a href="https://ai.swacktech.com/plugins/colab-pilot/">Swack Tools Marketplace</a> · '
        f'<a href="https://github.com/swack-tools/colab-pilot-ai-plugin">Source on GitHub</a> · '
        f'<a href="https://github.com/swack-tools/colab-pilot-ai-plugin/releases/latest">Releases</a> · '
        f'<a href="https://github.com/swack-tools/colab-pilot-ai-plugin/blob/main/LICENSE">Apache-2.0 License</a><br>'
        f"Community project by Swack Tools. Not affiliated with Google, Anthropic, or OpenAI.</p></footer>"
        f"</body></html>\n"
    )


def build(destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit("Output directory must be empty; choose a fresh directory.")
    destination.mkdir(parents=True, exist_ok=True)
    for name, (title, description) in PAGES.items():
        body = (ROOT / "pages" / f"{name}.html").read_text(encoding="utf-8")
        (destination / f"{name}.html").write_text(
            render_shell(name, title, description, body), encoding="utf-8"
        )
    not_found_body = (
        '<p class="eyebrow">404 · Not found</p>'
        "<h1>Page not found</h1>"
        '<p class="lead">The requested documentation page does not exist on this site.</p>'
        '<div class="actions"><a class="button" href="index.html">Return to overview →</a>'
        '<a class="button secondary" href="reference.html">Open technical reference</a></div>'
    )
    (destination / "404.html").write_text(
        render_shell(
            "404",
            "Page not found — Colab Pilot",
            "The requested Colab Pilot documentation page could not be found.",
            not_found_body,
        ),
        encoding="utf-8",
    )
    shutil.copytree(ROOT / "assets", destination / "assets")
    (destination / "CNAME").write_text(f"{CNAME_HOST}\n", encoding="utf-8")
    (destination / ".nojekyll").touch()
    print(f"Built {len(PAGES) + 1} pages in {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)

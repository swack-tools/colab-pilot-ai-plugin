#!/usr/bin/env python3
"""Validate generated Colab Pilot site links, landmarks, canonicals, and CNAME."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from build import BASE_URL, CNAME_HOST, PAGES


class Page(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.h1_count = 0
        self.landmarks: set[str] = set()
        self.canonical: list[str | None] = []
        self.errors: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "link" and attr_map.get("rel") == "canonical":
            self.canonical.append(attr_map.get("href"))
        if "id" in attr_map and attr_map["id"] is not None:
            if attr_map["id"] in self.ids:
                self.errors.append(f"Duplicate ID: {attr_map['id']}")
            self.ids.add(attr_map["id"])
        if tag == "h1":
            self.h1_count += 1
        if tag in {"main", "header", "footer", "nav", "title"}:
            self.landmarks.add(tag)
        for key in ("href", "src"):
            value = attr_map.get(key)
            if value is not None:
                self.links.append(value)


def check(root: Path) -> None:
    root = root.resolve()
    pages = {p: Page(p.read_text(encoding="utf-8")) for p in root.rglob("*.html")}
    expected_pages = set(PAGES) | {"404"}
    assert {x.stem for x in pages} == expected_pages, "Generated page set mismatch"
    assert (root / "CNAME").read_text(encoding="utf-8") == f"{CNAME_HOST}\n", "Invalid CNAME"
    assert (root / ".nojekyll").is_file(), "Missing .nojekyll"
    checked = 0
    for path, page in pages.items():
        assert not page.errors, f"{path}: {page.errors}"
        canonical_path = "" if path.name == "index.html" else path.name
        assert page.canonical == [f"{BASE_URL}/{canonical_path}"], f"{path}: incorrect canonical URL"
        assert page.h1_count == 1, f"{path}: expected exactly one h1"
        assert page.landmarks == {"main", "header", "footer", "nav", "title"}, f"{path}: missing landmarks"
        for link in page.links:
            parts = urlsplit(link)
            if parts.scheme or parts.netloc:
                assert parts.scheme == "https", f"Unexpected external link scheme: {link}"
                continue
            assert not parts.path.startswith("/"), f"Use relative paths: {link}"
            target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            assert target.is_relative_to(root), f"Link escapes site: {link}"
            assert target.is_file(), f"{path.name}: missing target {link}"
            if parts.fragment:
                assert target in pages and unquote(parts.fragment) in pages[target].ids, (
                    f"{path.name}: missing anchor {link}"
                )
            checked += 1
    for path in root.rglob("*"):
        assert not path.is_symlink(), f"Pages artifact must not contain symlinks: {path}"
    print(f"Validated {len(pages)} pages, {checked} local links, CNAME, and landmarks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    check(parser.parse_args().site)

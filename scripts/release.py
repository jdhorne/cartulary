#!/usr/bin/env python3
"""Release helper for cartulary — keeps the version string in sync everywhere.

A release version lives in several places that must agree, and they drift easily
(they did, 0.1.0 → 0.1.1): the package version, the README's pre-commit `rev:`
and Action `uses:` pins, and the pre-commit hook's usage example. This script is
the single source of truth for bumping them together and for catching drift.

Usage
-----
  python scripts/release.py X.Y.Z     # bump every site to X.Y.Z, then print the
                                       # git/gh commands to finish the release
  python scripts/release.py --check    # verify all sites already agree (exit 1
                                       # if not) — good as a CI / pre-commit guard

It intentionally does NOT run git/gh for you: it edits files and tells you the
exact commands, so tagging and pushing stay a deliberate step you control.
"""

import argparse
import pathlib
import re
import sys

# Each site: (relative path, compiled regex with one capture group for the
# X.Y.Z digits, replacement template). The pyproject pin is bare; the rest carry
# a leading `v`. re.sub replaces every occurrence in the file.
_SEMVER = r"(\d+\.\d+\.\d+)"
SITES = [
    ("pyproject.toml", re.compile(r'(?m)^version = "' + _SEMVER + r'"'), 'version = "{v}"'),
    ("README.md", re.compile(r"rev: v" + _SEMVER), "rev: v{v}"),
    ("README.md", re.compile(r"cartulary@v" + _SEMVER), "cartulary@v{v}"),
    (".pre-commit-hooks.yaml", re.compile(r"rev: v" + _SEMVER), "rev: v{v}"),
]


def find_versions(root: pathlib.Path) -> list[tuple[str, str]]:
    """Every (site-label, version) pair found across the tracked sites."""
    out: list[tuple[str, str]] = []
    for rel, pat, _repl in SITES:
        text = (root / rel).read_text()
        for m in pat.finditer(text):
            out.append((rel, m.group(1)))
    return out


def check(root: pathlib.Path) -> tuple[bool, set[str]]:
    """(all_sites_agree, set_of_distinct_versions_seen)."""
    versions = {v for _, v in find_versions(root)}
    return len(versions) == 1, versions


def bump(root: pathlib.Path, new_version: str) -> list[str]:
    """Rewrite every site to *new_version*. Returns the changed file names."""
    changed: list[str] = []
    for rel, pat, repl in SITES:
        path = root / rel
        text = path.read_text()
        new_text = pat.sub(repl.format(v=new_version), text)
        if new_text != text:
            path.write_text(new_text)
            if rel not in changed:
                changed.append(rel)
    return changed


def _next_steps(version: str) -> str:
    tag = f"v{version}"
    return (
        "\nNext steps (review the diff, then):\n"
        f"  git commit -am \"Release {tag}\"\n"
        f"  git tag -a {tag} -m \"{tag}\"\n"
        f"  git push origin main --tags\n"
        f"  gh release create {tag} --generate-notes\n"
        "  # then, if listing the Action: publish the release to the Marketplace\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump/verify cartulary's release version.")
    parser.add_argument("version", nargs="?", help="new version, e.g. 0.2.0")
    parser.add_argument("--check", action="store_true",
                        help="verify all sites already agree; exit 1 on drift")
    args = parser.parse_args(argv)

    root = pathlib.Path(__file__).resolve().parents[1]

    if args.check:
        ok, versions = check(root)
        if ok:
            print(f"OK: all version pins agree ({next(iter(versions))}).")
            return 0
        print(f"DRIFT: version pins disagree: {sorted(versions)}", file=sys.stderr)
        for site, v in find_versions(root):
            print(f"  {site}: {v}", file=sys.stderr)
        return 1

    if not args.version:
        parser.error("provide a version (e.g. 0.2.0) or use --check")
    if not re.fullmatch(_SEMVER, args.version):
        parser.error(f"'{args.version}' is not a valid X.Y.Z version")

    changed = bump(root, args.version)
    print(f"Bumped to {args.version} in: {', '.join(changed) or '(no changes)'}")
    ok, versions = check(root)
    if not ok:
        print(f"WARNING: sites still disagree after bump: {sorted(versions)}", file=sys.stderr)
        return 1
    print(_next_steps(args.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

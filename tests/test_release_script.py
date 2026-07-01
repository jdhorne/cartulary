"""Tests for the release helper's version-sync logic.

The release script's whole job is to keep the version string in agreement across
pyproject, the README pin/`uses:` ref, and the pre-commit hook example — the
exact thing that drifted during the 0.1.0 → 0.1.1 release. These tests pin the
bump + consistency-check behaviour on throwaway fixtures (no git involved).
"""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_release():
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release = _load_release()


def _fixture(root, *, pyproject="0.1.1", readme_rev="0.1.1", readme_uses="0.1.1", hook="0.1.1"):
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "cartulary"\nversion = "{pyproject}"\nrequires-python = ">=3.10"\n'
    )
    (root / "README.md").write_text(
        f"    rev: v{readme_rev}\n      - uses: jdhorne/cartulary@v{readme_uses}\n"
        "Some SARIF 2.1.0 mention that must not be treated as a version pin.\n"
    )
    (root / ".pre-commit-hooks.yaml").write_text(f"#       rev: v{hook}\n")


def test_find_versions_reads_every_site(tmp_path):
    _fixture(tmp_path)
    versions = {v for _, v in release.find_versions(tmp_path)}
    assert versions == {"0.1.1"}
    # all four sites are found (pyproject, README rev, README uses, hook)
    assert len(release.find_versions(tmp_path)) == 4


def test_bump_updates_all_sites_consistently(tmp_path):
    _fixture(tmp_path)
    changed = release.bump(tmp_path, "0.2.0")
    assert set(changed) == {"pyproject.toml", "README.md", ".pre-commit-hooks.yaml"}
    assert {v for _, v in release.find_versions(tmp_path)} == {"0.2.0"}


def test_check_passes_when_consistent(tmp_path):
    _fixture(tmp_path)
    ok, versions = release.check(tmp_path)
    assert ok
    assert versions == {"0.1.1"}


def test_check_detects_drift(tmp_path):
    # README pin left behind at 0.1.0 while everything else is 0.1.1 — the exact
    # mistake the script exists to prevent.
    _fixture(tmp_path, readme_rev="0.1.0")
    ok, versions = release.check(tmp_path)
    assert not ok
    assert versions == {"0.1.0", "0.1.1"}


def test_unrelated_version_like_text_is_ignored(tmp_path):
    # "SARIF 2.1.0" in the README must not be picked up as a version pin.
    _fixture(tmp_path)
    assert "2.1.0" not in {v for _, v in release.find_versions(tmp_path)}

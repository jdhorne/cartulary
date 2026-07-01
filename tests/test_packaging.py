"""Smoke tests for the distribution surface: pre-commit hook and GitHub Action.

These aren't behavioural — they guard against the config drifting out of sync
with the package (e.g. the hook entry no longer matching the console script) and
against the YAML simply being malformed.
"""

import pathlib
from importlib.metadata import entry_points

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_yaml(name):
    return yaml.safe_load((ROOT / name).read_text())


def _console_scripts():
    # The scripts the *installed* package actually exposes — a stronger drift
    # check than reading pyproject, and version-portable (no tomllib on 3.10).
    return {ep.name for ep in entry_points(group="console_scripts")}


def test_pre_commit_hooks_file_exists_and_parses():
    hooks = _load_yaml(".pre-commit-hooks.yaml")
    assert isinstance(hooks, list) and hooks


def test_pre_commit_hook_entry_matches_console_script():
    hooks = _load_yaml(".pre-commit-hooks.yaml")
    hook = next(h for h in hooks if h["id"] == "cartulary")
    # The entry point must be a real console script the package installs.
    assert hook["entry"] in _console_scripts()
    assert hook["language"] == "python"


def test_pre_commit_hook_validates_whole_corpus():
    # Referential integrity is a whole-graph property, so the hook must NOT be
    # driven by pre-commit's changed-file list — it validates the whole corpus.
    hook = next(h for h in _load_yaml(".pre-commit-hooks.yaml") if h["id"] == "cartulary")
    assert hook.get("pass_filenames") is False


def test_action_is_valid_composite_with_required_inputs():
    action = _load_yaml("action.yml")
    assert action["name"] and action["description"]
    assert action["runs"]["using"] == "composite"
    assert action["runs"]["steps"]
    assert "schema" in action["inputs"]


def test_action_emits_and_uploads_sarif():
    text = (ROOT / "action.yml").read_text()
    assert "--sarif" in text
    assert "codeql-action/upload-sarif" in text


def test_ci_workflow_parses_and_runs_pytest():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest" in text
    # must parse as YAML
    assert yaml.safe_load(text)


def test_ci_workflow_guards_version_pin_drift():
    # CI must run the release drift check, so a version bump that forgets a pin
    # fails the build instead of shipping.
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "release.py --check" in text


def test_pypi_publish_workflow_uses_tokenless_trusted_publishing():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert yaml.safe_load(text)  # parses
    assert "pypa/gh-action-pypi-publish" in text
    assert "id-token: write" in text          # OIDC — the trusted-publishing signal
    # No long-lived credential should ever be committed.
    low = text.lower()
    assert "password:" not in low
    assert "pypi_api_token" not in low and "secrets." not in low

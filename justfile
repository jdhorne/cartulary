# cartulary dev tasks — run with `just <task>` (https://just.systems).
# PYTHON is overridable: `just PYTHON=python3 test`.

PYTHON := ".venv/bin/python"

# list available tasks
default:
    @just --list

# run the test suite
test:
    {{PYTHON}} -m pytest -q

# verify every release version pin agrees (fails on drift)
check-version:
    {{PYTHON}} scripts/release.py --check

# bump all version pins to VERSION, then print the release steps
release VERSION:
    {{PYTHON}} scripts/release.py {{VERSION}}

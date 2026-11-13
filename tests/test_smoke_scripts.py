"""Runs every reference smoke-test script against the INSTALLED package (python -I),
so `pytest` is the single command for the whole suite (E-14). Each script keeps working
on its own: python reference/smoke_test_<name>.py"""
import subprocess
import sys
from pathlib import Path

import pytest

REFERENCE = Path(__file__).resolve().parents[1] / "reference"
SCRIPTS = sorted(REFERENCE.glob("smoke_test_*.py"))
# Scripts that need an optional third-party library are skipped when it is not installed.
OPTIONAL = {"smoke_test_multimodal_concurrency": "langgraph"}


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.stem for s in SCRIPTS])
def test_smoke_script(script):
    if script.stem in OPTIONAL:
        pytest.importorskip(OPTIONAL[script.stem])
    done = subprocess.run([sys.executable, "-I", "-W", "always", script.name], cwd=REFERENCE,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    # v1.3 preparation (V13-01): no script may run on E2A's v1.1 adapters
    assert "removed in v1.3" not in done.stderr, [l for l in done.stderr.splitlines() if "removed in v1.3" in l]
    assert "checks passed" in done.stdout.splitlines()[-1]

"""2026-11-16: the console launcher pip writes into the Scripts folder is an
unsigned binary, and Windows Smart App Control blocked it; the package must
run as a module so `python -m midas ...` is a complete stand-in for `midas ...`
in a terminal, a scheduled task and the operator scripts."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_python_dash_m_midas_is_the_cli():
    out = subprocess.run(
        [sys.executable, "-m", "midas", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "midas monitor [--loop]" in out.stdout  # the CLI's own help text

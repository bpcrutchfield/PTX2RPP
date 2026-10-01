"""Tests for the PTX2RPP command-line interface."""

import subprocess
import sys

from ptx2rpp import __version__


def run_ptx2rpp(*args: str) -> subprocess.CompletedProcess[str]:
    """Run PTX2RPP through the installed Python package."""

    return subprocess.run(
        [sys.executable, "-m", "ptx2rpp", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_help() -> None:
    result = run_ptx2rpp("--help")

    assert result.returncode == 0
    assert "Convert a Pro Tools .ptx session to a REAPER .rpp project." in result.stdout
    assert "--playlists-to-lanes" in result.stdout
    assert "--max-gap-heal-ms" in result.stdout
    assert "--strict" in result.stdout


def test_version() -> None:
    result = run_ptx2rpp("--version")

    assert result.returncode == 0
    assert __version__ in result.stdout
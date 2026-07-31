"""Every tutorial must actually run.

Examples rot silently: a rename or a changed default leaves prose that still
reads plausibly but no longer works, which is worse than no example at all.
Running them under CI turns that into a failing build.

Discovery is by glob, so a new tutorial is covered the moment it's added — there
is no list here to forget to update.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TUTORIALS = sorted((Path(__file__).parent.parent / "tutorials").glob("[0-9]*.py"))


def test_tutorials_are_discovered():
    """Guards the glob itself: an empty list would make the suite below vacuous
    and every tutorial silently untested."""
    assert TUTORIALS, "no tutorials found — has the directory moved?"


@pytest.mark.parametrize("script", TUTORIALS, ids=lambda p: p.stem)
def test_tutorial_runs(script: Path):
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout[-3000:]}\n"
        f"--- stderr ---\n{result.stderr[-3000:]}"
    )


@pytest.mark.parametrize("script", TUTORIALS, ids=lambda p: p.stem)
def test_tutorial_is_documented(script: Path):
    """Each one needs a module docstring saying what it covers, and a row in the
    index — a tutorial nobody can find is not a tutorial."""
    source = script.read_text(encoding="utf-8")
    assert source.startswith('"""'), f"{script.name} has no module docstring"
    index = (script.parent / "README.md").read_text(encoding="utf-8")
    assert script.name in index, f"{script.name} is missing from tutorials/README.md"

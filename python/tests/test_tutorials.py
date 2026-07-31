"""The tutorials' code must actually run.

Documentation rots silently: a rename or a changed default leaves prose that
still reads plausibly but no longer works, which is worse than no example at
all. So every ``python`` block in every tutorial is extracted, concatenated in
document order, and executed as one script.

That shapes how the tutorials are written, deliberately. Blocks build on each
other rather than each standing alone, and claims the prose makes are spelled as
``assert`` statements in the code — so running them checks the narrative, not
just the imports.

Blocks that are illustrative rather than runnable (a snippet quoted from
elsewhere in the repo, say) are fenced as ``text`` or ``py`` and skipped.

Discovery is by glob, so a new tutorial is covered the moment it's added.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

TUTORIAL_DIR = Path(__file__).parent.parent / "tutorials"
TUTORIALS = sorted(p for p in TUTORIAL_DIR.glob("[0-9]*.md"))

# ```python ... ``` only. Other fences are documentation, not executable.
BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def code_of(tutorial: Path) -> str:
    blocks = BLOCK.findall(tutorial.read_text(encoding="utf-8"))
    return "\n".join(blocks)


def test_tutorials_are_discovered():
    """Guards the glob itself.

    An empty list would make every parametrized test below pass vacuously, and
    the whole suite would go quietly green while testing nothing.
    """
    assert TUTORIALS, f"no tutorials found in {TUTORIAL_DIR}"


@pytest.mark.parametrize("tutorial", TUTORIALS, ids=lambda p: p.stem)
def test_tutorial_has_runnable_code(tutorial: Path):
    """A tutorial with no executable blocks would also pass `runs` vacuously."""
    assert code_of(tutorial).strip(), f"{tutorial.name} has no ```python blocks"


@pytest.mark.parametrize("tutorial", TUTORIALS, ids=lambda p: p.stem)
def test_tutorial_code_runs(tutorial: Path, tmp_path: Path):
    script = tmp_path / f"{tutorial.stem}.py"
    script.write_text(code_of(tutorial), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )
    assert result.returncode == 0, (
        f"{tutorial.name}: extracted code exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout[-2000:]}\n"
        f"--- stderr ---\n{result.stderr[-3000:]}"
    )


@pytest.mark.parametrize("tutorial", TUTORIALS, ids=lambda p: p.stem)
def test_tutorial_is_indexed(tutorial: Path):
    """A tutorial nobody can find is not a tutorial."""
    index = (TUTORIAL_DIR / "README.md").read_text(encoding="utf-8")
    assert tutorial.name in index, (
        f"{tutorial.name} is missing from tutorials/README.md"
    )


@pytest.mark.parametrize("tutorial", TUTORIALS, ids=lambda p: p.stem)
def test_tutorial_links_resolve(tutorial: Path):
    """Catches a renamed file leaving dangling 'Next:' links behind."""
    text = tutorial.read_text(encoding="utf-8")
    for target in re.findall(r"\]\((\d[^)#]*\.md)\)", text):
        assert (TUTORIAL_DIR / target).exists(), (
            f"{tutorial.name} links to {target}, which does not exist"
        )

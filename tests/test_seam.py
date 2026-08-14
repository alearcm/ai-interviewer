"""THE SEAM, enforced.

The engine must contain no scenario vocabulary. If any of these
substrings shows up anywhere in engine source, scenario logic has
leaked out of the packs and this build is wrong.
"""

import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Engine scope: the harness package, the regrade entry point, and the
# run-capture wrapper.
ENGINE_FILES = sorted(
    glob.glob(os.path.join(ROOT, "harness", "**", "*.py"), recursive=True)
    + [os.path.join(ROOT, "regrade.py"), os.path.join(ROOT, "tools", "irun")]
)

FORBIDDEN = ["python", "idiom", "code", "leetcode"]


def test_engine_has_no_scenario_vocabulary():
    assert len(ENGINE_FILES) >= 15, "engine files went missing: %r" % ENGINE_FILES
    offenders = []
    for path in ENGINE_FILES:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().lower()
        for word in FORBIDDEN:
            if word in text:
                for i, line in enumerate(text.splitlines(), 1):
                    if word in line:
                        offenders.append("%s:%d contains %r" % (os.path.relpath(path, ROOT), i, word))
    assert not offenders, "\n".join(offenders)


def test_engine_never_imports_from_packs():
    # Pack discovery by directory name is fine; importing pack contents
    # as modules would not be.
    for path in ENGINE_FILES:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "import packs" not in text and "from packs" not in text, (
            "%s imports from the packs directory" % path
        )

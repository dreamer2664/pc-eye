#!/usr/bin/env python3
"""Give a measurement script the tree it benchmarks against, or tell it to skip — never let it fail
because the baseline SHA happens not to be in this checkout (which is exactly what CI is)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_SHA = os.environ.get("PCEYE_BASE_SHA", "bb68297")


def ensure(tag="base"):
    """Returns a path containing the pre-fix server.py, or None. Never raises."""
    repo = Path(__file__).resolve().parent.parent
    dest = Path(tempfile.gettempdir()) / f"pceye-{tag}-{BASE_SHA[:8]}"
    if (dest / "server.py").exists():
        return dest
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", BASE_SHA],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None                      # shallow clone / CI checkout: the A/B is not available here
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "-C", str(repo), "show", f"{BASE_SHA}:server.py"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    (dest / "server.py").write_text(r.stdout)
    return dest


def skip(what="the before/after comparison"):
    print(f"  SKIPPED {what}: baseline {BASE_SHA} is not in this checkout "
          f"(shallow clone or CI). Set PCEYE_BASE_SHA to a tag in this repo.")

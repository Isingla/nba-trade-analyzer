"""site_Data checkout helpers: file paths + git commit dates (Phase 2A).

Option ``status_as_of`` and the staleness guard both key off the CSV's git
COMMIT date, never the run date — a regenerated-but-stale source keeps its
true vintage (databallr Phase 0, Path 3a: regen date != data date).
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path


def site_data_root() -> Path:
    return Path(os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data")))


def csv_git_date(root: Path, filename: str) -> datetime | None:
    """Last git commit date touching ``filename`` in the site_Data checkout.

    Returns None when the checkout isn't a git repo or the file is untracked;
    callers surface that as a staleness warning rather than failing the run.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%cI", "--", filename],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stamp = out.stdout.strip()
    if out.returncode != 0 or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None

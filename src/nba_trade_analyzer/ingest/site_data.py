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


# Where the gabriel1200/site_Data checkout actually lives on this machine.
# The old default, ~/site_Data, was a dead path that only worked through a
# symlink (07-11 sweep: seven code sites each carried their own copy of it);
# this helper is now the SINGLE source of that default — nothing else may
# hardcode a site_Data location.
DEFAULT_SITE_DATA_ROOT = "/Users/Ising/Databallr Work/site_Data"


def site_data_root() -> Path:
    """Resolve the site_Data checkout: $SITE_DATA_ROOT first, then the default.

    Fail-loud house style: a missing DIRECTORY raises immediately, naming both
    locations tried — never a silent empty-data read. (Missing individual CSVs
    inside an existing checkout keep their per-loader semantics.)
    """
    env = os.environ.get("SITE_DATA_ROOT")
    root = Path(env) if env else Path(DEFAULT_SITE_DATA_ROOT)
    if not root.is_dir():
        raise FileNotFoundError(
            f"site_Data checkout not found at {root} — tried SITE_DATA_ROOT "
            f"({env or 'unset'}) then the default {DEFAULT_SITE_DATA_ROOT}. "
            "Clone https://github.com/gabriel1200/site_Data there or set "
            "SITE_DATA_ROOT to an existing checkout."
        )
    return root


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

"""Owner-only permissions for run output files."""
from __future__ import annotations

import os
from pathlib import Path

RESTRICTIVE_UMASK = 0o077
OWNER_ONLY_FILE_MODE = 0o600


def apply_restrictive_umask() -> int:
    """Hide group/other bits on files created after this call. Returns the prior umask."""
    return os.umask(RESTRICTIVE_UMASK)


def chmod_owner_only(path: Path | str) -> None:
    """``chmod 600`` so manifest / summary / log files stay owner-readable."""
    os.chmod(path, OWNER_ONLY_FILE_MODE)

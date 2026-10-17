"""Owner-only permissions for run output files that hold filenames."""
from __future__ import annotations

import os
from pathlib import Path

OWNER_ONLY_FILE_MODE = 0o600


def chmod_owner_only(path: Path | str) -> None:
    """``chmod 600`` so manifest / summary / prompt dumps stay owner-readable."""
    os.chmod(path, OWNER_ONLY_FILE_MODE)

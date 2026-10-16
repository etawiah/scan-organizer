#!/usr/bin/env python3
"""scan-organizer entry point.

Merged replacement for the former ``scan_organizer.py`` /
``scan_organizer_secure.py`` pair. All functionality lives in the
``scanorganizer`` package; this module just wires the CLI together.
"""
from __future__ import annotations

import sys

from scanorganizer.cli import main

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Hook entry point for both runtimes. Installed with an absolute interpreter
path, because a hook that cannot start is a gate that is open (S8-g2)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xsm.receive import main  # noqa: E402

# Never 2: Claude Code reads that status from a hook as "block" (2026-10-01).
status = main()
sys.exit(1 if status == 2 else status)

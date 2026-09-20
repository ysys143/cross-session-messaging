#!/usr/bin/env python3
"""Hook entry point for both runtimes. Installed with an absolute interpreter
path, because a hook that cannot start is a gate that is open (S8-g2)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xsm.receive import main  # noqa: E402

sys.exit(main())

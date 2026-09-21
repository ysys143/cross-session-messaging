#!/usr/bin/env python3
"""Forced-command entry for paired remotes (ADR-0007). authorized_keys runs
this with the peer label as its only argument; the request comes on stdin."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xsm.remote import main  # noqa: E402

sys.exit(main())

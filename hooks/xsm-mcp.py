#!/usr/bin/env python3
"""MCP server entry point for both runtimes. Registered with an absolute
interpreter path, like the hook, so it never depends on PATH."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xsm.mcp import main  # noqa: E402

sys.exit(main())

#!/usr/bin/env python3
"""Live smoke test wrapper for the search router.

Equivalent to ``python scripts/selfcheck.py --live``.
"""

from __future__ import annotations

import sys

from selfcheck import main

if __name__ == "__main__":
    argv = list(sys.argv[1:])
    if "--live" not in argv:
        argv.insert(0, "--live")
    raise SystemExit(main(argv))

#!/usr/bin/env python3
"""Convenience entry point. Equivalent to `python -m dust_collector`.

    python collect.py scan
    python collect.py estimate
    python collect.py collect --live --yes
    python collect.py withdraw --to 0xYOUR_MAIN_WALLET --live
"""

import sys

from dust_collector.cli import main

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Multi-variant DRY-RUN simulator. One data pipeline, several paper portfolios. Never places orders."""
import sys

from bot.multi import main

if __name__ == "__main__":
    sys.exit(main())

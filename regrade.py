"""Rebuild a session report from its transcript, fully offline.

    regrade.py <session-dir> [--stdout | --out FILE]
"""

import sys

from harness.regrade import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

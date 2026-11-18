#!/usr/bin/env python3
"""Forced SSH status command for the unprivileged RAGSpark pairing account."""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rsinst import pair_transport


def main():
    if len(sys.argv) != 1 or os.geteuid() == 0:
        return 1
    try:
        reply = pair_transport.node_status(os.environ.get('SSH_ORIGINAL_COMMAND', ''),
                                           os.environ.get('SSH_CONNECTION', ''))
    except (OSError, ValueError):
        print('RAGSpark pair status refused', file=sys.stderr)
        return 1
    sys.stdout.buffer.write(reply)
    return 0


if __name__ == '__main__':
    sys.exit(main())

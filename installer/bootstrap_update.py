#!/usr/bin/env python3
"""Provision reviewed update code during an administrator's maintenance window."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rsinst import update_bootstrap
from rsinst.update_source import Busy, PreparationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', required=True, action='store_true',
                        help='confirm that RAGSpark services and timers are stopped for provisioning')
    parser.parse_args()
    try:
        update_bootstrap.bootstrap(Path(__file__).resolve().parent)
    except (OSError, ValueError, PreparationError, Busy) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('Update runner installed. Reload systemd in the maintenance window before starting RAGSpark.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

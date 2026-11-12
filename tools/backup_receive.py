#!/usr/bin/env python3
"""Forced SSH command for the dedicated receiving account; never run with sudo."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "installer"))
from rsinst.backup_remote import dispatch

if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(dispatch(os.environ.get("SSH_ORIGINAL_COMMAND", "")))

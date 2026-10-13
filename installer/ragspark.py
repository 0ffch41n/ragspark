#!/usr/bin/env python3
"""RAGSpark installer and control tool.

  sudo ./install.sh                 install (from a clone of the repository)
  sudo ragspark status              after installation: state of the stack
  sudo ragspark --help              all commands

Python standard library only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rsinst.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

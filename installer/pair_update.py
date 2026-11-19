#!/usr/bin/env python3
"""Forced SSH command for the unprivileged RAGSpark update account."""

import os
from pathlib import Path
import pwd
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rsinst import pair_transport as pair, pair_update_transport as updates, release as r


def main():
    try:
        r.need(len(sys.argv) == 1 and os.geteuid() != 0
               and pwd.getpwnam(updates.USER).pw_uid == os.geteuid(), 'invalid update receiver account')
        updates.verify_connection(os.environ.get('SSH_ORIGINAL_COMMAND', ''), os.environ.get('SSH_CONNECTION', ''))
        sudo = '/usr/bin/sudo'
        os.execve(sudo, [sudo, '-n', '--', *updates.RUNNER], pair.COMMAND_ENV)
    except (OSError, ValueError, KeyError):
        print('RAGSpark pair update refused', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

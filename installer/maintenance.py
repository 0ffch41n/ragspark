#!/usr/bin/env python3
"""Stable, root-controlled entry point copied outside the installed release.

Run with -I -B. Keep its lock while the installed command or systemd job runs,
and handle recovery entirely with this pinned copy of the installer modules.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rsinst import cli, paths, release as r, ui, update_apply, update_transaction as txn
from rsinst.update_source import Busy, PreparationError
from rsinst.sysutil import retain_update_lock


def installed(tree, args, fd, cache):
    command = [sys.executable, '-I', '-B', '-X', 'pycache_prefix=' + str(cache),
               str(tree / 'installer/ragspark.py'), *args]
    return subprocess.run(command, pass_fds=(fd,)).returncode


def unit(tree, args, fd, cache):
    kind, docker, role = args
    r.choice(kind, ('unit-start', 'unit-stop'))
    r.choice(role, ('head', 'node'))
    docker = Path(docker)
    r.need(docker.is_absolute(), 'systemd Docker path must be absolute')
    cwd = tree / 'compose' / 'node' if role == 'node' else tree / 'compose'
    if kind == 'unit-start':
        code = installed(tree, ['prestart'], fd, cache)
        if code:
            return code
        compose = ['up', '-d', '--wait', '--wait-timeout', '2400']
    else:
        compose = ['stop']
    return subprocess.run([docker, 'compose', *compose], cwd=cwd, pass_fds=(fd,)).returncode


def is_update(argv):
    iterator = iter(argv)
    for value in iterator:
        if value == '--lang':
            next(iterator, None)
        elif value not in ('--yes', '-y') and not value.startswith('--lang='):
            return value == 'update'
    return False


def main(argv):
    systemd = bool(argv) and argv[0] in ('unit-start', 'unit-stop')
    if systemd and len(argv) != 3:
        return 2
    update = is_update(argv)
    args = cli.build_parser().parse_args(argv) if update else None
    recovery = update and (args.resume or args.rollback)
    ui.set_lang((args.lang if args else None) or ui.detect_lang())
    if os.geteuid() != 0 and os.getenv('RAGSPARK_ALLOW_NONROOT') != '1':
        ui.status('FAIL', ui.t('need_root', cmd='sudo ragspark'))
        return 5
    root = Path(paths.host(paths.UPDATE_DIR))
    tree = Path(paths.install_dir())
    try:
        if argv == ['update-peer']:
            from rsinst import pair_update_transport
            return pair_update_transport.accept(root, tree, sys.stdin.buffer)
        if argv and argv[0] == 'update-node':
            from rsinst import update_node
            return update_node.command(root, tree, argv[1:])
        with txn.lock(root / 'maintenance.lock', exclusive=update and not args.check) as fd:
            if not recovery and txn.pending(root):
                ui.status('FAIL', ui.t('up_recover'))
                return 7
            if update:
                with retain_update_lock(fd):
                    return update_apply.command(args, gated=True)
            with tempfile.TemporaryDirectory(prefix='run-', dir=root) as cache:
                if systemd:
                    return unit(tree, argv, fd, cache)
                return installed(tree, argv, fd, cache)
    except Busy:
        ui.status('FAIL', ui.t('up_busy'))
        return 4
    except (OSError, ValueError, PreparationError) as exc:
        ui.status('FAIL', ui.t('up_prepare', detail=str(exc)))
        return 5
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

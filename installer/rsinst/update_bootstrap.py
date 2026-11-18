"""One-time provisioning of the independent runner and systemd entry points."""
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from . import backup_remote, paths, release as r, release_verify as v
from . import update_host as host, update_transaction as txn
from .manifest import Manifest


WRAPPER = '#!/bin/sh\nexec /usr/bin/python3 -I -B ' + paths.UPDATE_RUNNER + ' "$@"\n'


def jobs(role='head'):
    main = paths.UNIT_NAME
    return {
        main: (('ExecStartPre', ''),
               ('ExecStart', 'unit-start /usr/bin/docker ' + role),
               ('ExecStop', 'unit-stop /usr/bin/docker ' + role)),
        paths.WATCH_SERVICE: (('ExecStart', 'watch'),),
        paths.BACKUP_SERVICE: (('ExecStart', 'backup --scheduled'),),
        backup_remote.SYNC_SERVICE: (('ExecStart', 'backup --sync'),),
        backup_remote.RECEIVE_SERVICE: (('ExecStart', 'backup --receive'),),
    }


def unit_exec(args):
    return '/usr/bin/python3 -I -B ' + paths.UPDATE_RUNNER + ' ' + args if args else ''


def unit_config(edits):
    result = '[Service]\n'
    for prop, args in edits:
        result += prop + '=\n'
        if args:
            result += prop + '=' + unit_exec(args) + '\n'
    return result


def unit_state(name, properties):
    args = ['systemctl', 'show', '--all', *['--property=' + p for p in properties], name]
    raw = host.output(args).decode()
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def ensure_ready(role='head'):
    # Checking actual loaded definitions avoids a race with an operator who
    # has installed the files but has not reloaded the manager yet.
    wrapper = Path(paths.host(paths.CLI_PATH))
    r.need(v.bounded_read(wrapper, 4096, trusted=True) == WRAPPER.encode(),
           'independent update entry point is not installed')
    for name, edits in jobs(role).items():
        props = ('LoadState', 'ActiveState', 'SubState', *(prop for prop, _ in edits))
        configured = unit_state(name, props)
        if name != paths.UNIT_NAME and configured.get('LoadState') == 'not-found':
            continue
        r.need(configured.get('LoadState') == 'loaded', 'systemd unit unavailable: ' + name)
        r.need(configured.get('ActiveState') not in ('activating', 'deactivating', 'reloading')
               and configured.get('SubState') != 'running', 'systemd job is in progress: ' + name)
        for prop, args in edits:
            # systemctl may omit a cleared command, including with --all.
            actual = configured.get(prop, '')
            needed = [unit_exec(args)] if args else []
            argv = [m.strip() for m in re.findall(r'argv\[\]=([^;]+);', actual)]
            r.need(argv == needed and bool(actual) == bool(needed),
                   'loaded systemd command differs for ' + name + ' ' + prop)


def publish_engine(root, installer):
    engine = root / 'engine'
    if os.path.lexists(engine):
        txn.secure_directory(engine)
        r.need((engine / 'maintenance.py').is_file(), 'incomplete existing update runner')
        return
    with tempfile.TemporaryDirectory(prefix='engine-', dir=root) as work:
        new = Path(work) / 'engine'
        new.mkdir(mode=0o700)
        shutil.copyfile(installer / 'maintenance.py', new / 'maintenance.py')
        shutil.copytree(installer / 'rsinst', new / 'rsinst',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # The isolated admin copy has no group/world writable paths.
        for directory, _, names in os.walk(new, topdown=False):
            os.chmod(directory, 0o700)
            for name in names:
                with open(Path(directory) / name, 'rb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    os.fsync(stream.fileno())
            txn.sync_dir(directory)
        new.rename(engine)
        txn.sync_dir(root)


def bootstrap(installer):
    r.need(os.geteuid() == 0 or os.getenv('RAGSPARK_ALLOW_NONROOT') == '1', 'administrator required')
    tree = Path(paths.install_dir())
    root = Path(paths.host(paths.UPDATE_DIR))
    manifest = Manifest.load()
    r.need(manifest is not None and manifest.status == 'installed', 'installation is not ready')
    from .install import CLI_SCRIPT
    llm = r.string(manifest.config.get('llm'), r'[a-z0-9][a-z0-9.-]{0,79}')
    r.need(manifest.config.get('role', 'head') == 'head'
           and not (tree / 'compose/node/llm' / (llm + '.yaml')).is_file(), 'bootstrap is single-mode only')
    root.mkdir(mode=0o700, exist_ok=True)
    txn.secure_directory(root)
    r.need((root.stat().st_mode & 0o777) == 0o700, 'update directory must be 0700')
    with txn.lock(root / 'maintenance.lock'):
        r.need(not txn.pending(root), 'a saved update requires recovery')
        for name in jobs():
            status = unit_state(name, ('LoadState', 'ActiveState'))
            r.need(status.get('LoadState') in ('loaded', 'not-found')
                   and status.get('ActiveState') in ('inactive', 'failed'), 'stop systemd service first: ' + name)
        from .update_apply import observe
        r.need(not observe(tree), 'stop the RAGSpark containers first')
        wrapper = Path(paths.host(paths.CLI_PATH))
        old = v.bounded_read(wrapper, 4096, trusted=True)
        legacy = CLI_SCRIPT.format(install_dir=paths.INSTALL_DIR).encode()
        r.need(old in (WRAPPER.encode(), legacy) and str(wrapper) in manifest.data['files'],
               'the installed launcher differs from the managed RAGSpark launcher')
        publish_engine(root, installer)
        for name, edits in jobs().items():
            target = Path(paths.host('/etc/systemd/system/' + name + '.d/90-ragspark-update.conf'))
            data = unit_config(edits).encode()
            if os.path.lexists(target):
                r.need(v.bounded_read(target, 8192, trusted=True) == data, 'existing drop-in differs: ' + name)
            else:
                target.parent.mkdir(mode=0o755, exist_ok=True)
                txn.secure_directory(target.parent)
                txn.replace(target, data, 0o644)
            if str(target) not in manifest.data['files']:
                manifest.data['files'].append(str(target))
        # Track the unit files for ordinary uninstall and keep the version marker.
        txn.replace(tree / 'manifest.json', r.canonical(manifest.data),
                    (tree / 'manifest.json').stat().st_mode & 0o777)
        txn.replace(wrapper, WRAPPER.encode(), 0o755)

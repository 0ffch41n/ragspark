"""Crash drills against the existing installation layout in disposable directories."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from release_fixtures import file_record
from rsinst import release as r, release_inventory as inventory, update_transaction as txn
from rsinst.update_source import Busy

# Load the recovery process from a separate copy of the older modules.
# SIGKILL bypasses Python handlers, lock cleanup and finally blocks.
RUNNER = """
import os
import signal
import sys
from pathlib import Path
from rsinst import update_transaction as txn

state, tree, stop_at, direction = sys.argv[1:]
def checkpoint(name):
    if name == stop_at:
        os.kill(os.getpid(), signal.SIGKILL)
def verify(tree, side):
    expected = 'new' if side == 'target' else 'old'
    assert (tree / 'installer/feature.py').read_text() == expected
    assert (tree / 'compose/.env').read_text() == 'private=untouched\\n'
    assert (tree / 'manifest.json').exists()
with txn.lock(Path(state) / 'maintenance.lock'):
    print(txn.run(Path(state), Path(tree), verify,
                  rollback=direction == 'rollback', checkpoint=checkpoint))
"""

VERSION = 'abc0123'
STATE = {'version': VERSION, 'status': 'installed', 'schema': 1,
         'config': {'llm': 'default', 'secret': 'fixture'},
         'created_dirs': ['/opt/ragspark'], 'models_downloaded': ['keep'],
         'images_pulled': ['fixture'], 'backup_timer': None}
OLD = {'installer/feature.py': b'old', 'installer/legacy/old.py': b'legacy',
       'compose/compose.yaml': b'unchanged'}
NEW = {'installer/feature.py': b'new', 'installer/new/new.py': b'', 'compose/compose.yaml': b'unchanged'}


class Transaction(unittest.TestCase):
    def setUp(self):
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runner = self.base / 'older-runner'
        rsinst = Path(txn.__file__).resolve().parent
        shutil.copytree(rsinst, self.runner / 'rsinst', ignore=shutil.ignore_patterns('__pycache__'))
        before = [file_record(name, data) for name, data in OLD.items()]
        after = [file_record(name, data, mode='0755' if name.endswith('new.py') else '0644')
                 for name, data in NEW.items()]
        self.before, self.after = before, after
        self.old_source = inventory.legacy_baseline(VERSION + '0' * 33, before)
        self.next_source = {'version': '0.2.0', 'manifest_sha256': 'a' * 64}

    def setup_install(self, folder):
        root = self.base / folder
        state = root / 'var/lib/ragspark-update'
        tree = root / 'opt/ragspark'
        staged = root / 'candidate'
        state.mkdir(parents=True, mode=0o700)
        tree.mkdir(parents=True)
        staged.mkdir()
        for parent, payload in [(tree, OLD), (staged, NEW)]:
            for name, data in payload.items():
                f = parent / name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
                f.chmod(0o644)
        (tree / 'VERSION').write_text(VERSION + '\n')
        (tree / 'VERSION').chmod(0o644)
        (tree / 'manifest.json').write_bytes(r.canonical(STATE))
        (tree / 'manifest.json').chmod(0o600)
        (tree / 'compose/.env').write_text('private=untouched\n')
        (tree / 'compose/.env').chmod(0o600)
        (tree / 'install.log').write_text('keep\n')
        self.untouched = {str(f.relative_to(tree)): (f.stat().st_ino, f.stat().st_mtime_ns)
                          for f in (tree / 'compose/.env', tree / 'compose/compose.yaml', tree / 'install.log')}
        with txn.lock(state / 'maintenance.lock'):
            txn.prepare(state, tree, staged, self.before, self.after, self.old_source, self.next_source)
        # Resume depends on the sealed transaction, not the candidate or bundle.
        shutil.rmtree(staged)
        return state, tree

    def execution(self, state, tree, stop='', rollback=False):
        return subprocess.run([sys.executable, '-B', '-c', RUNNER, str(state), str(tree), stop,
                               'rollback' if rollback else 'resume'],
                              cwd=self.runner, env=dict(os.environ, PYTHONPATH=str(self.runner)),
                              capture_output=True, text=True, timeout=10)

    def verify_publication(self, state, tree, phase):
        target = phase == 'committed'
        source, records = ((self.next_source, self.after) if target else (self.old_source, self.before))
        self.assertEqual(txn.load_plan(state / txn.TRANSACTION)['phase'], phase)
        self.assertTrue(inventory.compare(tree, records, source)['matches'])
        self.assertEqual(json.loads((tree / 'manifest.json').read_bytes()),
                         dict(STATE, version=self.next_source['version'] if target else VERSION))
        self.assertEqual(stat.S_IMODE((tree / 'manifest.json').stat().st_mode), 0o600)
        for name, expected in self.untouched.items():
            observed = (tree / name).stat()
            self.assertEqual((observed.st_ino, observed.st_mtime_ns), expected)
        self.assertFalse((tree / '.update-publication').exists())

    def test_sigkill_at_each_apply_checkpoint_then_resume(self):
        points = ['publishing', 'target:installer/feature.py', 'target:installer/legacy/old.py',
                  'target:installer/new/new.py', 'target:VERSION', 'target:manifest.json', 'checking',
                  'verified:target', 'committed']
        for index, point in enumerate(points):
            with self.subTest(point=point):
                state, tree = self.setup_install(str(index))
                result = self.execution(state, tree, stop=point)
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                result = self.execution(state, tree)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'committed')
                self.verify_publication(state, tree, 'committed')

    def test_sigkill_during_rollback_followed_by_resume(self):
        points = ['reverting', 'previous:installer/feature.py', 'previous:installer/legacy/old.py',
                  'previous:installer/new/new.py', 'previous:VERSION', 'previous:manifest.json',
                  'verified:previous', 'reverted']
        for index, point in enumerate(points):
            with self.subTest(point=point):
                state, tree = self.setup_install(str(index))
                self.assertEqual(self.execution(state, tree).returncode, 0)
                result = self.execution(state, tree, stop=point, rollback=True)
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                result = self.execution(state, tree)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'reverted')
                self.verify_publication(state, tree, 'reverted')

    def test_bad_health_then_rollback(self):
        state, tree = self.setup_install('health')
        with txn.lock(state / 'maintenance.lock'):
            def unhealthy(*_):
                raise RuntimeError('fixture health failure')
            with self.assertRaisesRegex(RuntimeError, 'health failure'):
                txn.run(state, tree, unhealthy)
        self.assertEqual(txn.load_plan(state / txn.TRANSACTION)['phase'], 'checking')
        result = self.execution(state, tree, rollback=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.verify_publication(state, tree, 'reverted')

    def test_committed_rollback_cannot_discard_manifest_changes(self):
        state, tree = self.setup_install('mutable')
        self.assertEqual(self.execution(state, tree).returncode, 0)
        data = json.loads((tree / 'manifest.json').read_bytes())
        data['config']['llm'] = 'changed after apply'
        (tree / 'manifest.json').write_bytes(r.canonical(data))
        result = self.execution(state, tree, rollback=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((tree / 'manifest.json').read_bytes()), data)
        self.assertEqual(txn.load_plan(state / txn.TRANSACTION)['phase'], 'committed')

    def test_concurrent_process_fails_to_acquire_same_lock(self):
        state, tree = self.setup_install('lock')
        with txn.lock(state / 'maintenance.lock'):
            with self.assertRaises(Busy), txn.lock(state / 'maintenance.lock'):
                pass
            result = self.execution(state, tree)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Busy', result.stderr)
        self.assertEqual(txn.load_plan(state / txn.TRANSACTION)['phase'], 'prepared')

    def test_lock_survives_coordinator_crash_while_child_is_mutating(self):
        state, _ = self.setup_install('child')
        started, release = self.base / 'child-started', self.base / 'child-release'
        self.addCleanup(release.touch)
        child = """
import sys
import time
from pathlib import Path
started, stop = map(Path, sys.argv[1:])
started.touch()
for _ in range(200):
    if stop.exists():
        break
    time.sleep(0.025)
"""
        coordinator = """
import sys
from pathlib import Path
from rsinst import sysutil
from rsinst import update_transaction as txn
lock, source, started, stop = sys.argv[1:]
with txn.lock(Path(lock)) as fd, sysutil.retain_update_lock(fd):
    sysutil.run([sys.executable, '-B', '-c', source, started, stop])
"""
        lock = state / 'maintenance.lock'
        actor = subprocess.Popen([sys.executable, '-B', '-c', coordinator, str(lock), child,
                                  str(started), str(release)], cwd=self.runner,
                                 env=dict(os.environ, PYTHONPATH=str(self.runner)),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = time.monotonic() + 4
            while not started.exists() and time.monotonic() < until:
                time.sleep(0.025)
            self.assertTrue(started.exists())
        finally:
            actor.kill()
            actor.wait(timeout=5)
        with self.assertRaises(Busy), txn.lock(lock):
            pass
        release.touch()
        until = time.monotonic() + 4
        while True:
            try:
                with txn.lock(lock):
                    break
            except Busy:
                if time.monotonic() >= until:
                    self.fail('child did not release the update lock')
                time.sleep(0.025)

    def test_corrupt_staging_and_symlink_targets_refuse(self):
        state, tree = self.setup_install('stage')
        filename = state / txn.TRANSACTION / 'target' / 'installer/feature.py'
        filename.write_bytes(b'bad')
        result = self.execution(state, tree)
        self.assertIn('staged file does not match journal', result.stderr)
        self.assertEqual((tree / 'installer/feature.py').read_text(), 'old')
        filename.write_bytes(b'new')
        outside = self.base / 'outside'
        outside.write_text('leave me')
        (tree / 'installer/feature.py').unlink()
        (tree / 'installer/feature.py').symlink_to(outside)
        result = self.execution(state, tree)
        self.assertIn('unsafe installed file', result.stderr)
        self.assertEqual(outside.read_text(), 'leave me')

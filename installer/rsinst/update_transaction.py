"""Local code-publication journal. Entry-point and release-policy checks live outside it.

Only a verified, private staging tree may be passed to prepare. The installed
tree and all callers must be under the administrator's control, with lifecycle
commands serialized by the same external lock. Test entry points use temporary
installations. This module deliberately has no systemd or Docker dependencies.
"""

import fcntl
import hashlib
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import release as r, release_inventory as inventory, release_verify as v
from .update_source import Busy

SCHEMA = 1
TRANSACTION = 'transaction'
RECEIPT = {'installed-release.json': r.MAX_MANIFEST, 'installed-release.json.sig': v.MAX_SIGNATURE}
TERMINAL = ('committed', 'reverted')
METADATA = {'VERSION', 'manifest.json'}
PHASES = ('prepared', 'publishing', 'checking', 'verified', 'committed', 'reverting', 'reverted')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def secure_directory(path):
    st = path.lstat()
    r.need(stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid()
           and not st.st_mode & 0o022, 'unsafe update directory')


@contextmanager
def lock(path, exclusive=True):
    """All entry points must use this same stable file. Never remove its inode."""
    secure_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDWR, 0o600)
    try:
        info = os.fstat(fd)
        r.need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
               and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, 'unsafe update lock')
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy() from None
        yield fd
    finally:
        os.close(fd)


def replace(path, data, mode, scratch=None):
    """The temporary file stays outside managed code paths during publication."""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=scratch or path.parent, delete=False) as stream:
            temp = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
        if temp.parent != path.parent:
            sync_dir(temp.parent)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_snapshot(path, record):
    """Read ordinary bytes within the inventory limit, including empty files."""
    with v.regular_file(path) as stream:
        data = stream.read(record['bytes'] + 1)
    r.need(len(data) == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
           'staged file does not match journal: ' + record['path'])
    return data


def metadata(tree, name):
    limit = (4 << 20) if name == 'manifest.json' else 100
    return v.bounded_read(tree / name, limit)


def record_bytes(path, data, mode):
    return {'path': path, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'mode': format(mode, '04o')}


def changes(before, after):
    old = {rec['path']: rec for rec in before}
    new = {rec['path']: rec for rec in after}
    return [name for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name)]


def ancestors(tree, name):
    rel = Path(name)
    return list(reversed((tree / rel).parents[:len(rel.parts) - 1]))


def safe_target(tree, name):
    parent_device = tree.stat().st_dev
    for directory in ancestors(tree, name):
        if os.path.lexists(directory):
            secure_directory(directory)
            r.need(directory.stat().st_dev == parent_device, 'code spans filesystem boundaries')
    path = tree / name
    if os.path.lexists(path):
        st = path.lstat()
        r.need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid(),
               'unsafe installed file: ' + name)
    return path


def clean_empty_parents(tree, name):
    for directory in reversed(ancestors(tree, name)):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            sync_dir(directory.parent)


def operation_path(root):
    active = root / TRANSACTION
    return active if os.path.lexists(active) else root / 'completed'


def pending(root):
    operation = operation_path(root)
    if not os.path.lexists(operation):
        return False
    plan = load_plan(operation)
    if plan['phase'] not in TERMINAL:
        return True
    if 'service_update' in plan:
        from . import update_services
        return update_services.status(operation) != 'finished'
    return False


def activate(root, prepared):
    """Install a sealed journal on the same filesystem under the external lock."""
    operation = root / TRANSACTION
    r.need(not pending(root), 'previous update requires recovery')
    if os.path.lexists(operation):
        completed = root / 'completed'
        if os.path.lexists(completed):
            secure_directory(completed)
            shutil.rmtree(completed)
        os.rename(operation, completed)
        sync_dir(root)
    os.rename(prepared, operation)
    sync_dir(root)
    if prepared.parent != root:
        sync_dir(prepared.parent)
    return operation


def pair_control(control):
    from . import pair_transport
    r.fields(control, 'peer decision')
    pair_transport.binding(control['peer'])
    r.need(control['decision'] in (None, 'commit', 'rollback'), 'invalid pair decision')


def prepare(root, tree, staged, before, after, current, target, receipts=None, containers=None,
            service_update=None, *, pair_operation=None, pair=None, bundle=None):
    """Seal previous and candidate bytes before publishing the prepared phase.

    Caller owns the external lock and has verified the signed transition. Keeps a
    recoverable copy of the last terminal operation while preparing its successor.
    """
    for path in (root, tree, staged):
        secure_directory(path)
    r.need(not pending(root), 'previous update requires recovery')
    r.need(inventory.compare(tree, before, current)['matches'], 'installed inventory changed')
    r.inventory(after, 'code')
    # A changed leaf can be added or removed, but cannot become an ancestor of
    # another file. Validate the combined paths before creating any journal.
    union = {rec['path']: rec for rec in before + after}
    for name in union:
        parts = Path(name).parts
        r.need(not any(str(Path(*parts[:i])) in union for i in range(1, len(parts))),
               'file/directory conversion in the release')
        safe_target(tree, name)
    r.source(target)
    r.need('version' in target, 'target must be a signed release identity')
    if pair_operation is not None:
        r.string(pair_operation, r'[0-9a-f]{32}')
        r.need(service_update is None, 'pair journal cannot contain a service update')
    r.need((pair is None) == (bundle is None), 'pair coordinator requires its code bundle')
    if pair is not None:
        r.need(pair_operation is not None and receipts is not None, 'invalid pair coordinator journal')
        pair_control(pair)
    changed = set(changes(before, after))
    pending_dir = Path(tempfile.mkdtemp(prefix='prepare-', dir=root))
    try:
        version = metadata(tree, 'VERSION')
        manifest = metadata(tree, 'manifest.json')
        next_manifest = r.parse(manifest)
        next_manifest['version'] = target['version']
        previous_meta = {'VERSION': version, 'manifest.json': manifest}
        target_meta = {'VERSION': (target['version'] + '\n').encode(),
                       'manifest.json': r.canonical(next_manifest)}
        plan = {'schema': SCHEMA, 'phase': 'prepared',
                'previous_source': current, 'target_source': target, 'containers': containers}
        if service_update is not None:
            plan['service_update'] = service_update
        if pair_operation is not None:
            plan['pair_operation'] = pair_operation
        if pair is not None:
            plan['pair'] = pair
            release = r.manifest(receipts['target'][0])
            archive = next(item for item in release['artifacts'] if item['kind'] == 'code')
            dest = pending_dir / 'bundle' / archive['path']
            dest.parent.mkdir(parents=True, mode=0o700)
            with v.regular_file(bundle / archive['path']) as src, dest.open('xb') as out:
                shutil.copyfileobj(src, out, v.CHUNK)
                out.flush()
                os.fsync(out.fileno())
            for directory, _, _ in os.walk(pending_dir / 'bundle', topdown=False):
                sync_dir(directory)
        for side, base, files, marks in [('previous', tree, before, previous_meta),
                                         ('target', staged, after, target_meta)]:
            snapshot = pending_dir / side
            snapshot.mkdir(mode=0o700)
            meta = []
            for name, content in marks.items():
                mode = stat.S_IMODE(safe_target(tree, name).stat().st_mode)
                r.need(mode in (0o600, 0o640, 0o644), 'unexpected installation metadata permissions')
                meta.append(record_bytes(name, content, mode))
                replace(snapshot / name, content, 0o600)
            for rec in files:
                if rec['path'] in changed:
                    dest = snapshot / rec['path']
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    replace(dest, read_snapshot(base / rec['path'], rec), 0o600)
            for directory, _, _ in os.walk(snapshot, topdown=False):
                sync_dir(directory)
            proof = None
            if receipts is not None:
                envelope = receipts[side]
                proof = []
                for name, content in zip(RECEIPT, envelope or ()):
                    proof.append(record_bytes(name, content, 0o600))
                    replace(snapshot / name, content, 0o600)
            plan[side] = {'inventory': files, 'metadata': meta, 'receipt': proof}
        replace(pending_dir / 'journal.json', r.canonical(plan), 0o600)
        activate(root, pending_dir)
        return plan
    finally:
        if pending_dir.exists():
            shutil.rmtree(pending_dir)


def load_plan(operation):
    secure_directory(operation)
    # Each inventory can occupy the same bound as a signed release. This closed
    # journal is written by the recovery runner, not supplied by an archive.
    plan = r.parse(v.bounded_read(operation / 'journal.json', 2 * r.MAX_MANIFEST + 4096,
                                  trusted=True), limit=2 * r.MAX_MANIFEST + 4096)
    fields = 'schema phase previous_source target_source previous target containers'
    if isinstance(plan, dict) and 'service_update' in plan:
        from . import update_services
        update_services.validate_config(plan['service_update'])
        fields += ' service_update'
    if isinstance(plan, dict) and 'pair_operation' in plan:
        r.string(plan['pair_operation'], r'[0-9a-f]{32}')
        r.need('service_update' not in plan, 'pair journal cannot contain a service update')
        fields += ' pair_operation'
        if 'pair' in plan:
            pair_control(plan['pair'])
            fields += ' pair'
    r.fields(plan, fields)
    containers = plan['containers']
    if containers is not None:
        r.need(isinstance(containers, dict) and len(containers) <= 1000, 'invalid saved container identities')
        for service, ident in containers.items():
            r.string(service, r'[a-zA-Z0-9_.-]{1,128}')
            r.string(ident, r'[a-f0-9]{12,64}')
    r.need(r.integer(plan['schema']) == SCHEMA, 'unsupported update journal')
    r.choice(plan['phase'], PHASES)
    r.need(plan['phase'] != 'verified' or 'pair_operation' in plan, 'verified phase requires a pair operation')
    r.source(plan['previous_source'])
    r.source(plan['target_source'])
    for side in ('previous', 'target'):
        snapshot = plan[side]
        r.fields(snapshot, 'inventory metadata receipt')
        r.inventory(snapshot['inventory'], 'code')
        names = set()
        for rec in r.array(snapshot['metadata'], 2, 2):
            r.fields(rec, 'path bytes sha256 mode')
            name = r.choice(rec['path'], tuple(METADATA))
            r.need(name not in names, 'duplicate update metadata')
            names.add(name)
            r.integer(rec['bytes'], 1, (4 << 20) if name == 'manifest.json' else 100)
            r.digest(rec['sha256'])
            r.choice(rec['mode'], ('0600', '0640', '0644'))
        receipt = snapshot['receipt']
        if receipt is not None:
            r.need(type(receipt) is list and len(receipt) in (0, len(RECEIPT)), 'invalid saved release proof')
            for rec, (name, limit) in zip(receipt, RECEIPT.items()):
                r.fields(rec, 'path bytes sha256 mode')
                r.need(rec['path'] == name and rec['mode'] == '0600', 'invalid saved release proof')
                r.integer(rec['bytes'], 1, limit)
                r.digest(rec['sha256'])
    return plan


def mark(operation, plan, phase):
    next_plan = dict(plan, phase=phase)
    replace(operation / 'journal.json', r.canonical(next_plan), 0o600)
    plan['phase'] = phase


def commit_pair(operation, plan):
    r.need('pair_operation' in plan and plan['phase'] in ('verified', 'committed'),
           'pair code has not been verified')
    r.need('pair' not in plan or plan['pair']['decision'] == 'commit', 'pair commit decision is missing')
    if plan['phase'] != 'committed':
        mark(operation, plan, 'committed')
    return plan['phase']


def project(tree, operation, current, other, side, scratch, checkpoint):
    inventory_target = {rec['path']: rec for rec in current['inventory']}
    different = changes(current['inventory'], other['inventory'])
    for name in different:
        path = safe_target(tree, name)
        if name in inventory_target:
            rec = inventory_target[name]
            for parent in ancestors(tree, name):
                if not parent.exists():
                    parent.mkdir(mode=0o755)
                    sync_dir(parent.parent)
            replace(path, read_snapshot(operation / side / name, rec), int(rec['mode'], 8), scratch)
        elif path.exists():
            path.unlink()
            sync_dir(path.parent)
        if name not in inventory_target:
            clean_empty_parents(tree, name)
        checkpoint(side + ':' + name)
    for rec in current['metadata']:
        name = rec['path']
        path = safe_target(tree, name)
        replace(path, read_snapshot(operation / side / name, rec), int(rec['mode'], 8), scratch)
        checkpoint(side + ':' + name)


def publish_receipt(root, operation, side, proof, checkpoint):
    if proof is None:
        return
    if proof:
        for rec in proof:
            name = rec['path']
            replace(root / name, read_snapshot(operation / side / name, rec), 0o600)
            checkpoint(side + ':' + name)
    else:
        for name in RECEIPT:
            (root / name).unlink(missing_ok=True)
            sync_dir(root)
            checkpoint(side + ':' + name)


def run(root, tree, verify, rollback=False, checkpoint=lambda _: None, *, defer_commit=False):
    """Resume the durable direction, or select rollback under an external lock.

    A stopped process leaves the journal pending. Ordinary verification failure
    raises so the caller can request a verified rollback with the old runner.
    """
    operation = operation_path(root)
    plan = load_plan(operation)
    if 'pair_operation' in plan:
        r.need(defer_commit or rollback, 'pair publication requires a coordinated decision')
        if rollback and 'pair' in plan:
            r.need(plan['pair']['decision'] == 'rollback', 'pair rollback decision is missing')
    else:
        r.need(not defer_commit, 'deferred commit requires a pair operation')
    if plan['phase'] == 'reverted' or (plan['phase'] == 'committed' and not rollback):
        return plan['phase']
    if plan['phase'] == 'verified' and not rollback:
        return plan['phase']
    if plan['phase'] == 'prepared' and 'pair_operation' in plan and not rollback:
        r.need(inventory.compare(tree, plan['previous']['inventory'], plan['previous_source'])['matches'],
               'installed inventory changed after pair preparation')
        for rec in plan['previous']['metadata']:
            read_snapshot(tree / rec['path'], rec)
    if rollback and plan['phase'] != 'reverting':
        if plan['phase'] == 'committed':
            r.need(inventory.compare(tree, plan['target']['inventory'], plan['target_source'])['matches'],
                   'installed release changed since applying')
            for rec in plan['target']['metadata']:
                read_snapshot(tree / rec['path'], rec)
            for rec in plan['target']['receipt'] or ():
                read_snapshot(root / rec['path'], rec)
        mark(operation, plan, 'reverting')
        checkpoint('reverting')
    reverting = plan['phase'] == 'reverting'
    side, other = ('previous', 'target') if reverting else ('target', 'previous')
    different = set(changes(plan[side]['inventory'], plan[other]['inventory']))
    for label in (side, other):
        entries = [rec for rec in plan[label]['inventory'] if rec['path'] in different]
        for rec in entries + plan[label]['metadata'] + (plan[label]['receipt'] or []):
            read_snapshot(operation / label / rec['path'], rec)
    if plan['phase'] == 'prepared':
        mark(operation, plan, 'publishing')
        checkpoint('publishing')
    scratch = tree / '.update-publication'
    if os.path.lexists(scratch):
        secure_directory(scratch)
        r.need(stat.S_IMODE(scratch.stat().st_mode) == 0o700, 'unsafe publication scratch')
        # Only an orphan left by this runner can be inside the reserved directory.
        shutil.rmtree(scratch)
    scratch.mkdir(mode=0o700)
    try:
        project(tree, operation, plan[side], plan[other], side, scratch, checkpoint)
        if not reverting and plan['phase'] != 'checking':
            mark(operation, plan, 'checking')
            checkpoint('checking')
        r.need(inventory.compare(tree, plan[side]['inventory'], plan[side + '_source'])['matches'],
               'published inventory differs')
        verify(tree, side)
        publish_receipt(root, operation, side, plan[side]['receipt'], checkpoint)
        checkpoint('verified:' + side)
    finally:
        shutil.rmtree(scratch)
        sync_dir(tree)
    phase = 'reverted' if reverting else 'verified' if defer_commit else 'committed'
    mark(operation, plan, phase)
    checkpoint(phase)
    return phase

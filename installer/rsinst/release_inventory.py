"""Compare a local installation to records supplied by a trusted caller.

No Git, Docker, network or writes. Only inspect paths in the code inventory;
never emit configuration values, secret contents or arbitrary manifest fields.
"""
import hashlib
import json
import os
import re
import stat

from . import release as r


def inventory_sha(files):
    r.inventory(files, 'code')
    ordered = sorted(files, key=lambda item: item['path'])
    return hashlib.sha256(r.canonical(ordered)).hexdigest()


def legacy_baseline(commit, files):
    r.commit(commit)
    return {'legacy_commit': commit, 'inventory_sha256': inventory_sha(files)}


def _file_check(parent, name, record):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=parent), 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            return 'type'
        if stat.S_IMODE(s.st_mode) != int(record['mode'], 8):
            return 'mode'
        if s.st_size != record['bytes']:
            return 'size'
        digest = hashlib.sha256()
        left = s.st_size
        while left:
            block = f.read(min(1 << 20, left))
            if not block:
                return 'changed-during-read'
            digest.update(block)
            left -= len(block)
        after = os.fstat(f.fileno())
        if f.read(1) or after.st_ctime_ns != s.st_ctime_ns:
            return 'changed-during-read'
        if digest.hexdigest() != record['sha256']:
            return 'sha256'
    return None


def _small_file(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(fd)
        r.need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= limit,
               'invalid installation metadata')
        data = f.read(limit + 1)
        r.need(len(data) == st.st_size, 'installation metadata changed')
        return data


def compare(root, files, source):
    """Return path-level differences. Matching files alone cannot settle a version conflict."""
    r.source(source)
    actual_baseline = inventory_sha(files)
    r.need('legacy_commit' in source and actual_baseline == source['inventory_sha256'],
           'trusted legacy commit and inventory required')
    expected = {f['path']: f for f in files}
    parents = set()
    for name in expected:
        parts = name.split('/')
        parents.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    differences = {}
    seen = set()
    directory_flags = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW

    def visit(parent, prefix, names):
        for name in names:
            path = prefix + name
            if path in ('compose/.env', 'compose/node/.env'):
                continue
            if name == '__pycache__' and path not in expected:
                # Only ignore a real cache directory, not a symlink masquerading as one.
                if stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    continue
            if path in expected:
                seen.add(path)
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                issue = 'type'
                if stat.S_ISREG(st.st_mode):
                    issue = _file_check(parent, name, expected[path])
                if issue:
                    differences[path] = issue
            elif path in parents:
                if not stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    differences[path] = 'type'
                    continue
                child = os.open(name, directory_flags, dir_fd=parent)
                try:
                    visit(child, path + '/', os.listdir(child))
                finally:
                    os.close(child)
            else:
                differences[path] = 'unexpected'

    fd = os.open(root, directory_flags)
    try:
        names = sorted(set(os.listdir(fd)) & (r.CODE_FILES | r.CODE_DIRS))
        visit(fd, '', names)
        installed = _small_file(fd, 'VERSION', 100).decode('ascii').strip()
        state = json.loads(_small_file(fd, 'manifest.json', 4 << 20))
        r.need(isinstance(state, dict), 'invalid installation manifest')
        recorded = state.get('version')
    finally:
        os.close(fd)
    for missing in set(expected) - seen:
        differences[missing] = 'missing'
    version_ok = bool(re.fullmatch('[0-9a-f]{7,40}', installed)) and source['legacy_commit'].startswith(installed)
    if not version_ok:
        differences['VERSION'] = 'version-mismatch'
    if recorded != installed:
        differences['manifest.json'] = 'version-mismatch'
    return {'matches': not differences, 'file_count': len(expected), 'differences': dict(sorted(differences.items()))}

"""Build release-owned bytes from Git objects, independent of checkout files."""
import gzip
import hashlib
from pathlib import Path
import subprocess
import tarfile

from rsinst import release as r


def git(repo, *args):
    return subprocess.check_output(['git', '--no-replace-objects', '-C', str(repo), *args])


def snapshot(repo, commit, destination):
    """Materialize regular release files once into a new private directory."""
    r.commit(commit)
    r.need(git(repo, 'cat-file', '-t', commit).strip() == b'commit', 'expected a commit object')
    roots = sorted(r.CODE_DIRS | r.CODE_FILES)
    tree = git(repo, 'ls-tree', '-rlz', commit, '--', *roots)
    records = []
    total = 0
    destination.mkdir()
    for entry in sorted(tree.rstrip(b'\0').split(b'\0'), key=lambda v: v.partition(b'\t')[2]):
        header, _, name = entry.partition(b'\t')
        mode, kind, oid, size = header.split()
        path = r.code_path(name.decode('utf-8'))
        parts = Path(path).parts
        r.need(not any(p.lower() in ('secrets', 'credentials', 'id_rsa', 'id_ed25519', 'id_spark')
                       or p.startswith('credentials') or p.endswith(('.pem', '.key', '.env.local'))
                       for p in parts), 'private file in release tree')
        r.need(mode in (b'100644', b'100755') and kind == b'blob', 'unsupported release file type')
        total += int(size)
        r.need(total <= r.MAX_CODE_BYTES and len(records) < r.MAX_CODE_FILES, 'code inventory exceeds limits')
        out = destination / path
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('xb') as f:
            subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', 'blob', oid.decode('ascii')],
                           stdout=f, check=True)
        r.need(out.stat().st_size == int(size), 'Git blob size mismatch')
        records.append({'path': path, 'mode': mode[2:].decode(), 'bytes': int(size),
                        'sha256': file_sha(out)})
    r.inventory(records, 'code')
    return records


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def pack(directory, records, output):
    path = 'payload/code.tar.gz'
    filename = output / path
    filename.parent.mkdir()
    with filename.open('xb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0, compresslevel=6) as gz:
        with tarfile.open(fileobj=gz, mode='w|', format=tarfile.USTAR_FORMAT) as archive:
            for record in records:
                info = tarfile.TarInfo(record['path'])
                info.size = record['bytes']
                info.mode = int(record['mode'], 8)
                with (directory / info.name).open('rb') as f:
                    archive.addfile(info, f)
    return {'kind': 'code', 'id': 'code', 'path': path, 'bytes': filename.stat().st_size,
            'expanded_bytes': sum(f['bytes'] for f in records), 'files': records,
            'sha256': file_sha(filename), 'urls': []}

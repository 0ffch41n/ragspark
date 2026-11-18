"""Read-only host probes and comparison of installed/candidate service definitions."""
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from . import release as r, release_verify as v
from .sysutil import UPDATE_ENV, version_tuple
from .update_source import PreparationError


def output(argv, cwd=None):
    try:
        return subprocess.check_output(argv, cwd=cwd, timeout=60, stderr=subprocess.DEVNULL,
                                       env=UPDATE_ENV)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreparationError('cannot inspect ' + ' '.join(argv[:2])) from exc


def triple(raw):
    version = version_tuple(raw.decode())
    r.need(len(version) == 3, 'unrecognized host version')
    return version


def hardware(req):
    r.need(platform.system() == 'Linux' and platform.machine() == 'aarch64', 'requires Linux/aarch64')
    gpu = output(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader']).strip().split(b',')
    r.need(len(gpu) == 2 and b'GB10' in gpu[0], 'requires NVIDIA GB10')
    versions = {'python_min': sys.version_info[:3], 'driver_min': triple(gpu[1]),
                'docker_min': triple(output(['docker', 'version', '--format', '{{.Server.Version}}'])),
                'compose_min': triple(output(['docker', 'compose', 'version', '--short']))}
    for name, actual in versions.items():
        r.need(actual >= tuple(req[name]), 'host version requirement: ' + name)
    mem = Path('/proc/meminfo').read_text()
    total = int(re.search(r'^MemTotal:\s+(\d+)', mem, re.M)[1]) * 1024
    available = int(re.search(r'^MemAvailable:\s+(\d+)', mem, re.M)[1]) * 1024
    r.need(total >= req['min_total_ram_bytes'], 'host RAM below requirement')
    return available


def canonical_image(ref):
    first = ref.partition('/')[0]
    if '/' not in ref:
        return 'docker.io/library/' + ref
    if '.' not in first and ':' not in first and first != 'localhost':
        return 'docker.io/' + ref
    return ref


def installed_image(a):
    try:
        raw = output(['docker', 'image', 'inspect', a['image_ref']])
    except PreparationError:
        return False
    item, = json.loads(raw)
    repo, digest = a['image_ref'].split('@')
    repo = repo.rsplit('/', 1)[0] + '/' + repo.rsplit('/', 1)[1].split(':')[0]
    return (item.get('Id') == a['config_digest'] and item.get('Os') == 'linux'
            and item.get('Architecture') == 'arm64'
            and repo + '@' + digest in {canonical_image(x) for x in item.get('RepoDigests') or []})


def installed_model(cache, a):
    repo = cache / 'hub' / ('models--' + a['repo'].replace('/', '--'))
    snap = repo / 'snapshots' / a['revision']
    expected = {f['path']: f for f in a['files']}
    try:
        if snap.resolve(strict=True) != snap:
            return False
        found = set()
        for base, dirs, files in os.walk(snap, onerror=lambda exc: _raise(exc)):
            if any((Path(base) / name).is_symlink() for name in dirs):
                return False
            for name in files:
                path = Path(base) / name
                rel = path.relative_to(snap).as_posix()
                record = expected.get(rel)
                if record is None:
                    return False
                target = path.resolve(strict=True)
                if target != path and target.parent != repo / 'blobs':
                    return False
                with v.regular_file(target) as src:
                    st = os.fstat(src.fileno())
                    if st.st_size != record['bytes']:
                        return False
                    h = hashlib.sha256()
                    left = st.st_size
                    while left:
                        block = src.read(min(v.CHUNK, left))
                        if not block:
                            return False
                        h.update(block)
                        left -= len(block)
                    if (src.read(1) or h.hexdigest() != record['sha256']
                            or st.st_ctime_ns != os.fstat(src.fileno()).st_ctime_ns):
                        return False
                found.add(rel)
        return found == set(expected)
    except (OSError, RuntimeError, r.InvalidRelease):
        return False


def _raise(exc):
    raise exc


def materialize(archive, directory, code):
    # Only called after verify_artifact on a private staged copy. Create files
    # explicitly; never import candidate Python or restore archive permissions.
    expected = {rec['path'] for rec in code['files']}
    with tarfile.open(archive, 'r|gz') as tar:
        for member in tar:
            if member.isdir():
                continue
            r.need(member.isfile() and member.name in expected, 'invalid code member')
            dest = directory / member.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, dest.open('xb') as out:
                shutil.copyfileobj(src, out, v.CHUNK)


def render(directory, files):
    # Same real installation settings for both renders, no credential-bearing
    # stdout/stderr in logs. Relative mounts compare independent of staging path.
    args = ['docker', 'compose', '--env-file', '.env']
    for file in files:
        args.extend(['-f', file])
    raw = output([*args, 'config', '--no-path-resolution', '--format', 'json'], cwd=directory)
    return json.loads(raw)


def units(root):
    result = {}
    for name, funcs in [('install', {'step_service', 'step_watch', '_write_unit'}),
                        ('backup', {'schedule'}), ('backup_setup', {'_timers'})]:
        file = root / 'installer' / 'rsinst' / (name + '.py')
        if file.exists():
            for node in ast.parse(file.read_bytes()).body:
                if isinstance(node, ast.Assign):
                    for key in node.targets:
                        if isinstance(key, ast.Name) and 'UNIT' in key.id:
                            result[name, key.id] = ast.dump(node.value)
                if isinstance(node, ast.FunctionDef) and node.name in funcs:
                    result[name, node.name] = ast.dump(node)
    return result


def impact(oldroot, candidate, llm, mode, role, transition, profile, artifacts, changed, apply=False):
    relative = 'compose/node' if role == 'node' else 'compose'
    installed = oldroot / relative
    new = candidate / relative
    # COMPOSE_FILE and env_file resolve through the same existing settings.
    (new / '.env').symlink_to(installed / '.env')
    files = ['compose.yaml']
    if role == 'head' and mode == 'stack':
        files.append('stack.yaml')
    files.append('llm/' + llm + '.yaml')
    env = output(['docker', 'compose', '--env-file', '.env', 'config', '--environment'], cwd=installed)
    selected = [s for s in env.splitlines() if s.startswith(b'COMPOSE_FILE=')]
    r.need(selected == [('COMPOSE_FILE=' + ':'.join(files)).encode()], 'installed Compose selection differs')
    old, target = render(installed, files), render(new, files)
    if apply and transition['impact'] == 'services':
        from . import update_services
        update_services.compatible(old, target, changed, transition['restart'][role], llm)
    old_services, services = old.pop('services'), target.pop('services')
    pins = profile['roles'][role]
    images = {k: canonical_image(s['image']) for k, s in services.items()}
    r.need(images == {k: artifacts[i]['image_ref'] for k, i in pins['images'].items()},
           'effective Compose images differ from profile')
    model_services = {'llm': 'vllm-llm' if mode == 'single' else 'vllm-llm-rank'}
    if mode == 'single' or role == 'node':
        model_services.update(embedding='vllm-embed', rerank='vllm-rerank')
    r.need(set(pins['models']) == set(model_services), 'incomplete model profile')
    for logical, service in model_services.items():
        svc = services[service]
        args = (svc.get('entrypoint') or []) + (svc.get('command') or [])
        model = artifacts[pins['models'][logical]]
        r.need(args.count('serve') == 1 and args.count('--revision') == 1
               and args[args.index('serve')+1:][:1] == [model['repo']]
               and args[args.index('--revision')+1:][:1] == [model['revision']],
               'effective model differs from profile')
    affected = {k for k in set(old_services) | set(services) if old_services.get(k) != services.get(k)}
    if old != target:
        affected.update(services)
    for name, svc in services.items():
        for mount in svc.get('volumes', []):
            if mount['type'] == 'bind':
                source = Path(mount['source'])
                absolute = (installed / source).resolve()
                if absolute.is_relative_to(oldroot):
                    path = absolute.relative_to(oldroot).as_posix()
                    if any(f == path or f.startswith(path + '/') for f in changed):
                        affected.add(name)
    changed_units = (units(oldroot) != units(candidate) or
                     any(f.endswith(('.service', '.timer')) for f in changed))
    r.need(not changed_units or transition['impact'] == 'systemd', 'systemd maintenance missing from transition')
    r.need(affected <= set(transition['restart'][role]), 'service restart missing from transition')


class Space:
    def __init__(self):
        self.filesystems = {}

    def add(self, path, size):
        existing = Path(path)
        while not existing.exists():
            existing = existing.parent
        device = existing.stat().st_dev
        if device not in self.filesystems:
            mount = existing.resolve()
            while mount.parent != mount and mount.parent.stat().st_dev == device:
                mount = mount.parent
            self.filesystems[device] = [str(mount), 0, shutil.disk_usage(existing).free]
        entry = self.filesystems[device]
        entry[1] += size
        r.need(entry[1] <= entry[2], 'insufficient disk space on ' + entry[0])

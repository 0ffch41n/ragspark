#!/usr/bin/env python3
"""Publisher's utilities. Work from immutable Git blobs and local dependency archives."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'installer'))
from rsinst import release as r, release_inventory as inventory, release_verify as verify

from profiles import resolve
from source import pack, snapshot


def record_file(path):
    return r.parse(verify.bounded_read(Path(path).absolute(), r.MAX_MANIFEST))


def select_profiles(directory, recipe, deps, compose):
    assets = {}
    for asset in deps:
        r.artifact(asset)
        if asset['kind'] == 'model':
            key = (asset['repo'], asset['revision'])
        else:
            r.need(asset['kind'] == 'image', 'publisher supplies only image/model dependencies')
            key = asset['image_ref']
        r.need(key not in assets, 'ambiguous dependency descriptors')
        assets[key] = asset

    def find(key):
        if key not in assets:
            raise r.InvalidRelease('missing dependency descriptor for ' + str(key))
        return assets[key]['id']

    profiles = []
    used = set()
    for p in recipe:
        r.fields(p, 'id llm mode requirements')
        roles = resolve(directory, p['llm'], p['mode'], compose)
        r.fields(p['requirements'], ' '.join(roles))
        for role, spec in roles.items():
            images = {name: find(ref) for name, ref in spec['images'].items()}
            models = {name: find((pin['repo'], pin['revision'])) for name, pin in spec['models'].items()}
            roles[role] = {'requirements': p['requirements'][role], 'images': images, 'models': models}
            used.update(images.values())
            used.update(models.values())
        profiles.append({'id': p['id'], 'mode': p['mode'], 'llm': p['llm'], 'roles': roles})
    r.need(len({a['id'] for a in deps}) == len(deps), 'duplicate dependency IDs')
    return profiles, sorted((a for a in deps if a['id'] in used), key=lambda a: a['id'])


def build(repo, commit, recipe, deps, store, output, compose):
    r.fields(recipe, 'version sequence created_at profiles transitions code_urls')
    r.array(recipe['profiles'], 1, 1000)
    r.array(deps, 1, 10000)
    output = Path(output).absolute()
    r.need(not os.path.lexists(output), 'output already exists')
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        workspace = Path(tmp)
        directory = workspace / 'source'
        files = snapshot(repo, commit, directory)
        stage = workspace / 'result'
        stage.mkdir()
        code = pack(directory, files, stage)
        code['urls'] = recipe['code_urls']
        profiles, selected = select_profiles(directory, recipe['profiles'], deps, compose)
        result = {'format': 1, 'product': 'ragspark', 'version': recipe['version'],
                  'sequence': recipe['sequence'], 'created_at': recipe['created_at'], 'source_commit': commit,
                  'updater_protocol': 1, 'pair_protocol': 1, 'installation_schema': 1,
                  'target': {'os': 'linux', 'arch': 'aarch64', 'gpu': 'GB10'}, 'profiles': profiles,
                  'transitions': recipe['transitions'], 'artifacts': [code, *selected]}
        raw = r.canonical(result)
        r.manifest(raw)
        legacy = {}
        for transition in result['transitions']:
            source = transition['source']
            old = source.get('legacy_commit')
            if old:
                if old not in legacy:
                    records = snapshot(repo, old, workspace / ('baseline-' + old))
                    legacy[old] = inventory.legacy_baseline(old, records)
                r.need(legacy[old] == source, 'legacy inventory differs from the referenced commit')
        verify.verify_artifact(stage, code)
        for asset in selected:
            verify.verify_artifact(store, asset)
        (stage / 'release.json').write_bytes(raw)
        # Concurrent builders of this output cannot replace one another.
        fd = os.open(output.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            r.need(not os.path.lexists(output), 'output already exists')
            stage.rename(output)
        finally:
            os.close(fd)
    return {'output': str(output), 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'files': len(files), 'profiles': [p['id'] for p in profiles]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('pins', 'baseline', 'compare', 'build'):
        p = sub.add_parser(action)
        if action != 'compare':
            p.add_argument('--repo', type=Path, default=ROOT)
        p.add_argument('--commit', required=True, help='full reviewed source commit')
        if action in ('build', 'pins'):
            p.add_argument('--compose-bin', help='standalone Docker Compose executable (default: docker compose)')
        if action == 'pins':
            p.add_argument('--llm', required=True)
            p.add_argument('--mode', choices=['single', 'stack'], required=True)
        if action == 'baseline':
            p.add_argument('--files', action='store_true', help='include reference file list for Git-free comparison')
        if action == 'compare':
            p.add_argument('--baseline', type=Path, required=True, help='reference inventory from baseline --files')
            p.add_argument('--inventory-sha256', required=True, help='independently approved baseline digest')
            p.add_argument('--installed', type=Path, required=True)
        if action == 'build':
            for option in ('recipe', 'dependencies', 'store', 'output'):
                p.add_argument('--' + option, type=Path, required=True)
    args = parser.parse_args(argv)
    compose = [args.compose_bin] if getattr(args, 'compose_bin', None) else ['docker', 'compose']
    try:
        if args.action == 'build':
            result = build(args.repo, args.commit, record_file(args.recipe), record_file(args.dependencies),
                           args.store.absolute(), args.output, compose)
        elif args.action == 'compare':
            ref = record_file(args.baseline)
            r.fields(ref, 'source files')
            source = {'legacy_commit': args.commit, 'inventory_sha256': args.inventory_sha256}
            r.need(ref['source'] == source, 'baseline differs from approved source')
            result = inventory.compare(args.installed, ref['files'], source)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / 'source'
                files = snapshot(args.repo, args.commit, directory)
                if args.action == 'pins':
                    result = resolve(directory, args.llm, args.mode, compose)
                else:
                    source = inventory.legacy_baseline(args.commit, files)
                    result = {'source': source, 'files': files} if args.files else source
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get('matches', True) else 3
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('release: ' + str(exc), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())

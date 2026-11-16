"""Synthetic metadata only; never a published release or production key."""
import datetime as dt
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'installer'))
from rsinst import release as r


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_record(name='installer/example.py', data=b'pass\n', mode='0644'):
    return {'path': name, 'bytes': len(data), 'sha256': sha(data), 'mode': mode}


def sample():
    code = {'id': 'code', 'kind': 'code', 'path': 'payload/code.tar.gz', 'bytes': 100,
            'sha256': 'a' * 64, 'expanded_bytes': 5, 'urls': [], 'files': [file_record()]}
    image = {'id': 'image', 'kind': 'image', 'path': 'payload/images/test.tar', 'bytes': 100,
             'sha256': 'b' * 64, 'expanded_bytes': 200, 'urls': ['https://example.invalid/image.tar'],
             'image_ref': 'example.invalid/test/image@sha256:' + 'c' * 64,
             'config_digest': 'sha256:' + 'd' * 64, 'platform': 'linux/arm64'}
    model = {'id': 'weights', 'kind': 'model', 'path': 'payload/models/test.tar', 'bytes': 100,
             'sha256': 'e' * 64, 'expanded_bytes': 5, 'urls': [], 'repo': 'fixture/model',
             'revision': 'f' * 40, 'files': [file_record('config.json')]}
    requirements = {'python_min': [3, 10, 0], 'docker_min': [24, 0, 0], 'compose_min': [2, 24, 0],
                    'driver_min': [580, 0, 0], 'min_total_ram_bytes': 110 << 30}
    profile = {'id': 'fixture-single', 'mode': 'single', 'llm': 'fixture-llm', 'roles': {
        'head': {'requirements': requirements, 'images': {'vllm-llm': 'image'}, 'models': {'llm': 'weights'}}}}
    transition = {'source': {'version': '0.1.0', 'manifest_sha256': '1' * 64}, 'profile': profile['id'],
                  'impact': 'code', 'restart': {'head': []}, 'backup': 'none', 'migration': 'none', 'rollback': 'compatible'}
    return {'format': 1, 'product': 'ragspark', 'version': '0.2.0', 'sequence': 2, 'source_commit': '2' * 40,
            'created_at': '2026-11-13T00:00:00Z', 'updater_protocol': 1, 'pair_protocol': 1, 'installation_schema': 1,
            'target': {'os': 'linux', 'arch': 'aarch64', 'gpu': 'GB10'},
            'artifacts': [code, image, model], 'profiles': [profile], 'transitions': [transition]}


def pointer(raw=None):
    if raw is None: raw = r.canonical(sample())
    return {'format': 1, 'product': 'ragspark', 'channel': 'stable', 'sequence': 8,
            'issued_at': '2026-11-13T00:00:00Z', 'expires_at': '2026-11-20T00:00:00Z',
            'version': '0.2.0', 'release_sequence': 2,
            'manifest_url': 'https://example.invalid/release.json',
            'signature_url': 'https://example.invalid/release.json.sig',
            'manifest_bytes': len(raw), 'manifest_sha256': sha(raw)}


NOW = dt.datetime(2026, 11, 13, 12, tzinfo=dt.timezone.utc).timestamp()

"""Adversarial metadata tests; no network, services or real release material."""
import copy
import json
import unittest

from release_fixtures import NOW, pointer, sample, sha
from rsinst import release as r


class Parsing(unittest.TestCase):
    def test_exact_json_acceptance(self):
        d = sample()
        self.assertEqual(r.manifest(r.canonical(d)), d)

    def test_malformed_json_and_numbers(self):
        bad = [b'', b'\xef\xbb\xbf{}', b'\xff', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
               b'{"x":1.0}', b'{"x":1e2}', b'{"x":-1}', b'{"x":9223372036854775808}',
               b'{"x":' + b'9' * 5000 + b'}', b'{} trailing', b'[' * 33 + b'0' + b']' * 33]
        for raw in bad:
            with self.subTest(raw=raw[:30]), self.assertRaises(r.InvalidRelease): r.parse(raw)

    def test_malformed_tree_mutations_never_leak_parser_exceptions(self):
        def nodes(value, path=()):
            yield path
            if isinstance(value, dict):
                for k, v in value.items(): yield from nodes(v, path + (k,))
            elif isinstance(value, list):
                for k, v in enumerate(value): yield from nodes(v, path + (k,))
        for base, check in ((sample(), r.manifest), (pointer(), lambda b: r.channel(b, NOW))):
            for path in nodes(base):
                for value in (None, True, False, 0, -1, 1.5, '', [], {}, '\ud800'):
                    data = copy.deepcopy(base)
                    if not path:
                        data = value
                    else:
                        parent = data
                        for key in path[:-1]: parent = parent[key]
                        parent[path[-1]] = value
                    raw = json.dumps(data, ensure_ascii=True).encode()
                    try:
                        check(raw)
                    except r.InvalidRelease:
                        pass  # Some mutations remain valid; every rejection must be controlled.

    def test_metadata_limits(self):
        self.assertEqual(r.parse(b'{}', 2), {})
        with self.assertRaises(r.InvalidRelease): r.parse(b'{} ', 2)
        with self.assertRaises(r.InvalidRelease): r.parse('{}')

    def test_paths(self):
        for name in ('/etc/passwd', '../x', 'a/../x', './x', 'a//b', 'x/', 'a\\b', 'a\x00b', 'a\nb', 'a\x85b', '\ud800'):
            with self.subTest(name=repr(name)), self.assertRaises(r.InvalidRelease): r.path(name)
        self.assertEqual(r.path('dir/имя.txt'), 'dir/имя.txt')

    def test_version_and_url(self):
        for v in ('1.02.3', 'v1.2.3', '1.2.3-rc1', '1.2.3+build', '1.2', True):
            with self.subTest(v=v), self.assertRaises(r.InvalidRelease): r.version(v)
        for url in ('http://example.invalid/x', 'https://user:pass@example.invalid/x',
                    'https://example.invalid:bad/x', 'https://example.invalid/#x', 'https:///x',
                    'https://example.invalid/\n', 'https://exa mple.invalid/x', 'https://@example.invalid/x', 'https://:@example.invalid/x'):
            with self.subTest(url=url), self.assertRaises(r.InvalidRelease): r.https(url)
        self.assertGreater(r.version('1.10.0'), r.version('1.9.0'))


class Manifest(unittest.TestCase):
    def reject(self, change):
        d = sample(); change(d)
        with self.assertRaises(r.InvalidRelease): r.manifest(r.canonical(d))

    def test_unknown_missing_and_wrong_types(self):
        for key in sample():
            with self.subTest(key=key): self.reject(lambda d: d.pop(key))
        for key, value in [('surprise', 1), ('format', True), ('sequence', False), ('sequence', 0),
                           ('product', 'other'), ('pair_protocol', 2), ('installation_schema', 2),
                           ('created_at', '2026-02-30T00:00:00Z'), ('source_commit', 'ab')]:
            with self.subTest(key=key, value=value): self.reject(lambda d: d.update({key: value}))
        self.reject(lambda d: d['target'].update(arch='x86_64'))

    def test_inventory_rejections(self):
        changes = [lambda a: a['files'].append(copy.deepcopy(a['files'][0])),
                   lambda a: a['files'][0].update(path='compose/.env'),
                   lambda a: a['files'][0].update(path='installer/__pycache__/x.pyc'),
                   lambda a: a['files'][0].update(path='manifest.json'),
                   lambda a: a['files'][0].update(path='etc/passwd'),
                   lambda a: a['files'][0].update(mode='4755'),
                   lambda a: a['files'][0].update(bytes=True),
                   lambda a: a.update(expanded_bytes=6),
                   lambda a: a.update(path='outside.tar'),
                   lambda a: a['files'].append(dict(a['files'][0], path='installer/example.py/child', bytes=0))]
        for change in changes:
            with self.subTest(change=change): self.reject(lambda d: change(d['artifacts'][0]))

    def test_inventory_path_limits(self):
        for name in ('installer/' + 'x' * 101, 'installer/' + 'a/' * 32 + 'x'):
            with self.subTest(name=name):
                self.reject(lambda d: d['artifacts'][0]['files'][0].update(path=name))

    def test_artifact_rejections(self):
        self.reject(lambda d: d['artifacts'].append(copy.deepcopy(d['artifacts'][0])))
        self.reject(lambda d: d['artifacts'][1].update(path=d['artifacts'][0]['path']))
        self.reject(lambda d: d['artifacts'][1].update(path=d['artifacts'][0]['path'] + '/x'))
        self.reject(lambda d: d['artifacts'].pop(0))
        self.reject(lambda d: d['artifacts'][1].update(platform='linux/amd64'))
        self.reject(lambda d: d['artifacts'][1].update(image_ref='example.invalid/test:latest'))
        for ref in ('vllm/name', 'registry.invalid//repo', 'registry.invalid:/repo', 'registry.invalid:99999/repo',
                    'registry.invalid/../repo', 'registry.invalid/repo::tag'):
            with self.subTest(ref=ref):
                self.reject(lambda d: d['artifacts'][1].update(image_ref=ref + '@sha256:' + '0' * 64))
        self.reject(lambda d: d['artifacts'][2].update(revision='main'))
        self.reject(lambda d: d['artifacts'][2].update(repo='../model'))
        self.reject(lambda d: d['artifacts'][0].update(urls=['https://example.invalid/x'] * 2))
        self.reject(lambda d: d['artifacts'][0].update(bytes=r.MAX_CODE_BYTES + (64 << 20) + 1))

    def test_local_limits_override_signed_totals(self):
        def huge(d):
            a = d['artifacts'][0]
            a['files'][0]['bytes'] = a['expanded_bytes'] = r.MAX_CODE_BYTES + 1
        self.reject(huge)

    def test_role_maps_and_requirements(self):
        self.reject(lambda d: d['profiles'][0]['roles'].update(node=copy.deepcopy(d['profiles'][0]['roles']['head'])))
        self.reject(lambda d: d['profiles'][0]['roles']['head']['images'].update(bad='weights'))
        self.reject(lambda d: d['profiles'][0]['roles']['head']['models'].update(llm='missing'))
        self.reject(lambda d: d['profiles'][0]['roles']['head']['models'].update(other='weights'))
        self.reject(lambda d: d['profiles'][0]['roles']['head']['requirements'].update(python_min=[3, True, 0]))
        self.reject(lambda d: d['profiles'][0]['roles']['head']['requirements'].update(docker_min=[24, 0]))
        self.reject(lambda d: d['profiles'].append(copy.deepcopy(d['profiles'][0])))

    def test_valid_stack_and_service_transition(self):
        d = sample(); p = d['profiles'][0]
        p['mode'] = 'stack'; p['roles']['node'] = copy.deepcopy(p['roles']['head'])
        d['transitions'][0].update(impact='services', backup='required', restart={'head': ['vllm-llm'], 'node': ['vllm-llm']})
        self.assertEqual(r.manifest(r.canonical(d)), d)

    def test_transition_rejections(self):
        for values in ({'migration': 'upgrade'}, {'rollback': 'unsafe'}, {'profile': 'absent'},
                       {'impact': 'services'}, {'restart': {'head': ['vllm-llm']}},
                       {'restart': {'head': ['unknown']}}, {'restart': {}},
                       {'impact': 'systemd', 'backup': 'none'}):
            with self.subTest(values=values): self.reject(lambda d: d['transitions'][0].update(values))
        self.reject(lambda d: d['transitions'].append(copy.deepcopy(d['transitions'][0])))
        self.reject(lambda d: d['transitions'][0]['source'].update(version='0.2.0'))
        self.reject(lambda d: d['transitions'][0]['source'].update(legacy_commit='f' * 40))

    def test_legacy_source_and_matching(self):
        d = sample(); t = d['transitions'][0]
        t['source'] = {'legacy_commit': 'a' * 40, 'inventory_sha256': 'b' * 64}
        checked = r.manifest(r.canonical(d))
        args = [checked, t['source'], 'fixture-single', 'single', 'fixture-llm', d['target'], 2]
        self.assertEqual(r.select_transition(*args), t)
        for i, value in [(1, {'legacy_commit': 'c' * 40, 'inventory_sha256': 'b' * 64}),
                         (2, 'other'), (3, 'stack'), (4, 'different-llm'),
                         (5, dict(d['target'], arch='x86_64')), (6, 3)]:
            changed = list(args); changed[i] = value
            with self.subTest(index=i), self.assertRaises(r.InvalidRelease): r.select_transition(*changed)


class Channel(unittest.TestCase):
    def test_success_and_same_pointer(self):
        raw = r.canonical(pointer())
        d, state = r.channel(raw, NOW)
        self.assertEqual(r.channel(raw, NOW, state), (d, state))

    def test_dates_and_limits(self):
        for change in ({'issued_at': '2026-11-14T00:00:00Z'}, {'expires_at': '2026-11-13T00:00:00Z'},
                       {'expires_at': '2026-12-01T00:00:00Z'}, {'issued_at': '2026-13-13T00:00:00Z'},
                       {'manifest_bytes': r.MAX_MANIFEST + 1}, {'sequence': True}, {'channel': 'nightly'}):
            d = pointer(); d.update(change)
            with self.subTest(change=change), self.assertRaises(r.InvalidRelease): r.channel(r.canonical(d), NOW)

    def test_clock_tolerance_boundaries(self):
        p = pointer(); issued = r.timestamp(p['issued_at']); expires = r.timestamp(p['expires_at'])
        r.channel(r.canonical(p), issued - 300); r.channel(r.canonical(p), expires + 300)
        for now in (issued - 301, expires + 301, float('nan'), float('inf'), True):
            with self.subTest(now=now), self.assertRaises(r.InvalidRelease): r.channel(r.canonical(p), now)

    def test_replay_and_identity_reuse(self):
        d = pointer(); _, state = r.channel(r.canonical(d), NOW)
        changes = ({'sequence': 7}, {'manifest_sha256': '9' * 64},
                   {'sequence': 9, 'release_sequence': 1},
                   {'sequence': 9, 'manifest_sha256': '9' * 64},
                   {'sequence': 9, 'release_sequence': 3, 'version': '0.1.0'},
                   {'sequence': 9, 'release_sequence': 3, 'version': '0.2.0'})
        for change in changes:
            p = dict(d, **change)
            with self.subTest(change=change), self.assertRaises(r.InvalidRelease): r.channel(r.canonical(p), NOW, state)
        p = dict(d, sequence=9, expires_at='2026-11-21T00:00:00Z')
        r.channel(r.canonical(p), NOW, state)
        p.update(release_sequence=3, version='0.3.0', manifest_sha256='9' * 64)
        r.channel(r.canonical(p), NOW, state)

    def test_channel_binds_exact_release(self):
        raw = r.canonical(sample()); d = r.manifest(raw); p = pointer(raw)
        r.bind_channel(p, raw, d)
        for change in ({'manifest_bytes': len(raw) + 1}, {'manifest_sha256': '9' * 64},
                       {'version': '0.3.0'}, {'release_sequence': 4}):
            with self.subTest(change=change), self.assertRaises(r.InvalidRelease): r.bind_channel(dict(p, **change), raw, d)


if __name__ == '__main__': unittest.main()

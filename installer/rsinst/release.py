"""Format-1 release metadata. Pure validation, not an updater or trust decision.

Call release_verify.load_* for signed input. Parsing alone never authorizes code,
images, model downloads or installation. Effective Compose/catalog comparison and
persisting replay state belong to the later planner.
"""
import datetime as dt
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

MAX_INT = (1 << 63) - 1
MAX_CHANNEL = 64 << 10
MAX_MANIFEST = 16 << 20
MAX_CODE_BYTES = 1 << 30
MAX_CODE_FILES = 20000
MAX_MODEL_FILES = 100000
MAX_DEPTH = 32
CODE_DIRS = {'installer', 'tools', 'catalog', 'acceptance', 'compose'}
CODE_FILES = {'LICENSE', 'README.md', 'README.ru.md'}
RESERVED = {'VERSION', 'manifest.json', 'watch.json', 'install.log', 'backup.lock', '__pycache__', '.git', '.ssh', '.aws'}


class InvalidRelease(ValueError):
    """Rejected metadata/content; messages never contain raw untrusted values."""


def need(ok, message):
    if not ok:
        raise InvalidRelease(message)


def fields(value, names):
    need(type(value) is dict and set(value) == set(names.split()), 'unexpected object fields')


def integer(value, minimum=0, maximum=MAX_INT):
    need(type(value) is int and minimum <= value <= maximum, 'invalid integer')
    return value


def string(value, pattern=None, maximum=4096):
    need(type(value) is str and 0 < len(value) <= maximum, 'invalid string')
    need(all(unicodedata.category(c) not in ('Cc', 'Cs') for c in value),
         'invalid string characters')
    if pattern:
        need(re.fullmatch(pattern, value) is not None, 'invalid string format')
    return value


def choice(value, values):
    string(value)
    need(value in values, 'unsupported value')
    return value


def digest(value):
    return string(value, r'[0-9a-f]{64}')


def commit(value):
    return string(value, r'[0-9a-f]{40}')


def ident(value):
    return string(value, r'[a-z0-9-]{1,80}')


def version(value):
    string(value, r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', 60)
    return tuple(integer(int(part)) for part in value.split('.'))


def timestamp(value):
    string(value, r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
    try:
        return dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
    except (ValueError, OverflowError):
        raise InvalidRelease('invalid UTC timestamp') from None


def https(value):
    string(value, maximum=8192)
    try:
        u = urlsplit(value)
        need(u.scheme == 'https' and bool(u.hostname) and u.username is None and u.password is None
             and not u.fragment and '\\' not in value and not any(c.isspace() for c in value), 'invalid HTTPS URL')
        if u.port is not None:
            integer(u.port, 1, 65535)
    except ValueError:
        raise InvalidRelease('invalid HTTPS URL') from None
    return value


def path(value):
    string(value)
    need('\\' not in value and all(p not in ('', '.', '..') for p in value.split('/')), 'unsafe relative path')
    return value


def code_path(value):
    path(value)
    parts = value.split('/')
    need(value in CODE_FILES or (len(parts) > 1 and parts[0] in CODE_DIRS), 'path outside code inventory')
    need(not any(p in RESERVED or p.startswith('.env') or p.endswith(('.pyc', '.lock', '.log'))
                 for p in parts), 'mutable path in code inventory')
    return value


def array(value, minimum=0, maximum=MAX_MODEL_FILES):
    need(type(value) is list and minimum <= len(value) <= maximum, 'invalid array')
    return value


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        need(k not in out, 'duplicate JSON key')
        out[k] = v
    return out


def _number(text):
    need(len(text) <= 19 and text.isascii() and text.isdigit(), 'invalid JSON integer')
    return integer(int(text))


def _non_integer(_):
    raise InvalidRelease('non-integer JSON number')


def parse(raw, limit=MAX_MANIFEST):
    need(type(raw) is bytes and 0 < len(raw) <= limit and not raw.startswith(b'\xef\xbb\xbf'), 'invalid metadata size/encoding')
    try:
        text = raw.decode('utf-8', errors='strict')
        # Bound nesting before json.loads allocates a deeply nested structure.
        depth, quoted, escaped = 0, False, False
        for c in text:
            if quoted:
                if escaped:
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    quoted = False
            elif c == '"':
                quoted = True
            elif c in '[{':
                depth += 1
                need(depth <= MAX_DEPTH, 'JSON nesting limit')
            elif c in ']}':
                depth -= 1
        return json.loads(text, object_pairs_hook=_pairs, parse_int=_number,
                          parse_float=_non_integer, parse_constant=_non_integer)
    except (UnicodeError, RecursionError, ValueError) as e:
        if isinstance(e, InvalidRelease):
            raise
        raise InvalidRelease('invalid JSON') from None


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def channel(raw, now, previous=None):
    """Return (validated pointer, proposed high-water record); caller persists it."""
    d = parse(raw, MAX_CHANNEL)
    fields(d, 'format product channel sequence issued_at expires_at version release_sequence manifest_url signature_url manifest_bytes manifest_sha256')
    need(integer(d['format']) == 1 and d['product'] == 'ragspark' and d['channel'] == 'stable', 'unsupported channel')
    integer(d['sequence'], 1); integer(d['release_sequence'], 1)
    issued, expires = timestamp(d['issued_at']), timestamp(d['expires_at'])
    need(0 < expires - issued <= 14 * 86400, 'invalid channel lifetime')
    need(type(now) in (int, float) and issued <= now + 300 and expires >= now - 300, 'channel expired or clock incorrect')
    version(d['version']); https(d['manifest_url']); https(d['signature_url'])
    integer(d['manifest_bytes'], 1, MAX_MANIFEST); digest(d['manifest_sha256'])
    sha = hashlib.sha256(raw).hexdigest()
    state = {'sequence': d['sequence'], 'sha256': sha, 'release_sequence': d['release_sequence'],
             'version': d['version'], 'manifest_sha256': d['manifest_sha256']}
    if previous is not None:
        fields(previous, 'sequence sha256 release_sequence version manifest_sha256')
        integer(previous['sequence'], 1); integer(previous['release_sequence'], 1)
        digest(previous['sha256']); digest(previous['manifest_sha256']); version(previous['version'])
        need(d['sequence'] >= previous['sequence'], 'channel sequence rollback')
        need(d['sequence'] != previous['sequence'] or sha == previous['sha256'], 'channel sequence reused')
        need(d['release_sequence'] >= previous['release_sequence'], 'release sequence rollback')
        if d['release_sequence'] == previous['release_sequence']:
            need(d['version'] == previous['version'] and d['manifest_sha256'] == previous['manifest_sha256'], 'release sequence reused')
        else:
            need(version(d['version']) > version(previous['version']), 'release version rollback/reuse')
    return d, state


def inventory(entries, kind):
    array(entries, 1, MAX_CODE_FILES if kind == 'code' else MAX_MODEL_FILES)
    seen, total = set(), 0
    for f in entries:
        fields(f, 'path bytes sha256 mode')
        (code_path if kind == 'code' else path)(f['path'])
        parts = f['path'].split('/')
        name_bytes = f['path'].encode('utf-8')
        need(len(parts) <= 32 and (len(name_bytes) <= 100 or
             (len(parts[-1].encode('utf-8')) <= 100 and len('/'.join(parts[:-1]).encode('utf-8')) <= 155)),
             'inventory path exceeds USTAR/depth limits')
        need(f['path'] not in seen, 'duplicate inventory path')
        seen.add(f['path'])
        total += integer(f['bytes']); integer(total)
        digest(f['sha256']); choice(f['mode'], ('0644', '0755'))
    for p in seen:
        parts = p.split('/')
        need(not any('/'.join(parts[:n]) in seen for n in range(1, len(parts))), 'inventory file/directory collision')
    if kind == 'code':
        integer(total, 0, MAX_CODE_BYTES)
    return total


def artifact(a):
    need(type(a) is dict, 'invalid artifact')
    kind = choice(a.get('kind'), ('code', 'image', 'model'))
    extra = {'code': 'files', 'model': 'repo revision files', 'image': 'image_ref config_digest platform'}[kind]
    fields(a, 'id kind path bytes sha256 expanded_bytes urls ' + extra)
    ident(a['id']); path(a['path'])
    need(a['path'].startswith('payload/') and len(a['path'].split('/')) > 1, 'artifact outside payload')
    integer(a['bytes'], 1); digest(a['sha256']); integer(a['expanded_bytes'])
    for url in array(a['urls'], maximum=32):
        https(url)
    need(len(set(a['urls'])) == len(a['urls']), 'duplicate artifact URL')
    if kind != 'image':
        need(inventory(a['files'], kind) == a['expanded_bytes'], 'inventory byte total differs')
        if kind == 'model':
            string(a['repo'], r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*')
            commit(a['revision'])
        else:
            # Bound compressed input as well as expanded data and tar headers.
            integer(a['bytes'], 1, MAX_CODE_BYTES + (64 << 20))
    else:
        string(a['image_ref'], r'[^@]+@sha256:[0-9a-f]{64}')
        location = a['image_ref'].split('@')[0]
        host, slash, repository = location.partition('/')
        need(bool(slash), 'image registry/repository missing')
        hostname, colon, port = host.partition(':')
        string(hostname, r'[a-z0-9]+(?:[.-][a-z0-9]+)*')
        need('.' in hostname or hostname == 'localhost' or bool(colon), 'explicit image registry required')
        if colon:
            string(port, r'[0-9]{1,5}'); integer(int(port), 1, 65535)
        repository, colon, tag = repository.partition(':')
        if colon: string(tag, r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')
        for component in repository.split('/'):
            string(component, r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*')
        string(a['config_digest'], r'sha256:[0-9a-f]{64}')
        need(a['platform'] == 'linux/arm64', 'unsupported image platform')
    return a


def source(s):
    need(type(s) is dict, 'invalid transition source')
    if 'version' in s:
        fields(s, 'version manifest_sha256'); version(s['version']); digest(s['manifest_sha256'])
    else:
        fields(s, 'legacy_commit inventory_sha256'); commit(s['legacy_commit']); digest(s['inventory_sha256'])
    return s


def manifest(raw):
    d = parse(raw)
    fields(d, 'format product version sequence source_commit created_at updater_protocol pair_protocol installation_schema target artifacts profiles transitions')
    for key in ('format', 'updater_protocol', 'pair_protocol', 'installation_schema'):
        need(integer(d[key]) == 1, 'unsupported release protocol/schema')
    need(d['product'] == 'ragspark', 'wrong product')
    version(d['version']); integer(d['sequence'], 1); commit(d['source_commit']); timestamp(d['created_at'])
    fields(d['target'], 'os arch gpu')
    need(d['target'] == {'os': 'linux', 'arch': 'aarch64', 'gpu': 'GB10'}, 'unsupported target')
    artifacts, names = {}, set()
    for a in array(d['artifacts'], 1, 10000):
        artifact(a)
        need(a['id'] not in artifacts and a['path'] not in names, 'duplicate artifact ID/path')
        artifacts[a['id']] = a; names.add(a['path'])
    need(sum(a['kind'] == 'code' for a in artifacts.values()) == 1, 'expected exactly one code artifact')
    for p in names:
        parts = p.split('/')
        need(not any('/'.join(parts[:n]) in names for n in range(1, len(parts))), 'artifact path collision')
    profiles = {}
    for p in array(d['profiles'], 1, 1000):
        fields(p, 'id mode llm roles'); ident(p['id']); string(p['llm'], r'[a-z0-9][a-z0-9.-]{0,79}')
        choice(p['mode'], ('single', 'stack'))
        need(p['id'] not in profiles, 'duplicate profile')
        fields(p['roles'], 'head' if p['mode'] == 'single' else 'head node')
        for role in p['roles'].values():
            fields(role, 'requirements images models')
            req = role['requirements']
            fields(req, 'python_min docker_min compose_min driver_min min_total_ram_bytes')
            for k in ('python_min', 'docker_min', 'compose_min', 'driver_min'):
                for n in array(req[k], 3, 3): integer(n)
            integer(req['min_total_ram_bytes'], 1)
            for field, kind in (('images', 'image'), ('models', 'model')):
                need(type(role[field]) is dict and len(role[field]) <= 1000, 'invalid role map')
                for name, ref in role[field].items():
                    if field == 'images': string(name, r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}')
                    else: choice(name, ('llm', 'embedding', 'rerank'))
                    ident(ref)
                    need(ref in artifacts and artifacts[ref]['kind'] == kind, 'invalid artifact reference')
            need(bool(role['images']), 'role has no images')
        profiles[p['id']] = p
    matches = set()
    for t in array(d['transitions'], 1, 10000):
        fields(t, 'source profile impact restart backup migration rollback')
        source(t['source']); ident(t['profile'])
        need(t['profile'] in profiles, 'unknown transition profile')
        if 'version' in t['source']:
            need(version(t['source']['version']) < version(d['version']), 'non-upgrade transition')
        key = (canonical(t['source']), t['profile'])
        need(key not in matches, 'ambiguous transition'); matches.add(key)
        choice(t['impact'], ('code', 'services', 'systemd'))
        choice(t['backup'], ('none', 'required'))
        need(t['migration'] == 'none' and t['rollback'] == 'compatible', 'unsupported data migration/rollback')
        roles = profiles[t['profile']]['roles']
        fields(t['restart'], ' '.join(roles))
        for name, services in t['restart'].items():
            array(services, maximum=1000)
            for svc in services:
                string(svc)
                need(svc in roles[name]['images'], 'restart references unknown service')
            need(len(set(services)) == len(services), 'duplicate restart service')
            if t['impact'] == 'code': need(not services, 'code-only restart forbidden')
        if t['impact'] != 'code': need(t['backup'] == 'required', 'backup required')
    return d


def bind_channel(pointer, raw, release):
    """Bind independently verified channel/release, including sequence and version."""
    need(len(raw) == pointer['manifest_bytes'] and hashlib.sha256(raw).hexdigest() == pointer['manifest_sha256'], 'channel release digest/length mismatch')
    need(release['version'] == pointer['version'] and release['sequence'] == pointer['release_sequence'], 'channel release identity mismatch')


def select_transition(release, installed_source, profile_id, mode, llm, target, sequence_floor=0):
    """Pure compatibility selector for validated metadata; not an apply decision."""
    source(installed_source); ident(profile_id); integer(sequence_floor)
    need(target == release['target'], 'host target mismatch')
    need(release['sequence'] >= sequence_floor, 'release below accepted sequence')
    profiles = [p for p in release['profiles'] if p['id'] == profile_id]
    need(len(profiles) == 1 and profiles[0]['mode'] == mode and profiles[0]['llm'] == llm, 'model/mode/profile mismatch')
    matches = [t for t in release['transitions'] if t['source'] == installed_source and t['profile'] == profile_id]
    need(len(matches) == 1, 'no unique tested transition')
    return matches[0]

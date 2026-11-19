"""Restricted SSH transport for the separately authorized update account."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import pair_transport as pair, paths, release as r, release_verify as v


USER = 'ragspark-update'
COMMAND = 'update-v1'
ENTRYPOINT = pair.PAIR_DIR / 'receiver' / 'pair_update.py'
RUNNER = ('/usr/bin/python3', '-I', '-B', paths.UPDATE_RUNNER, 'update-peer')
SUDOERS = '/etc/sudoers.d/ragspark-pair-update'
ACTIONS = ('receive', 'status', 'apply', 'commit', 'cancel')
GRANT_PURPOSE = 'signed-code-update'
REQUEST_TIMEOUT = 1800


def authorized_key(head_ip, key):
    return (f'restrict,from="{pair.cable_address(head_ip)}",'
            f'command="/usr/bin/python3 -I -B {ENTRYPOINT}" {pair.public_key(key)}\n')


def sudo_rule():
    return (f'{USER} ALL=(root) NOPASSWD: NOSETENV: ' + ' '.join(RUNNER) + '\n').encode('ascii')


def grant(base, read):
    offer = r.parse(read(base / 'update.json'), pair.MAX_REPLY)
    r.fields(offer, 'protocol pair key purpose')
    r.need(offer['purpose'] == GRANT_PURPOSE, 'update grant requires administrator confirmation')
    r.need(r.integer(offer['protocol']) == pair.PROTOCOL, 'unsupported update grant protocol')
    data = pair.binding(offer['pair'])
    pair.public_key(offer['key'])
    r.need(pair.binding(r.parse(read(base / 'node.json'), pair.MAX_REPLY)) == data,
           'update grant differs from active pair')
    return data


def verify_connection(command, connection):
    r.need(command == COMMAND, 'unsupported update SSH command')
    pair.require_cable(connection, grant(pair.PAIR_DIR, pair._read_public_node_file))


def request_fields(frame, data):
    r.fields(frame, 'protocol pair action operation manifest_sha256')
    r.need(r.integer(frame['protocol']) == pair.PROTOCOL, 'unsupported pair update protocol')
    r.need(frame['pair'] == data['id'], 'request differs from active pair')
    r.choice(frame['action'], ACTIONS)
    r.string(frame['operation'], r'[0-9a-f]{32}')
    r.digest(frame['manifest_sha256'])
    return frame


def read_request(stream, data):
    raw = stream.readline(pair.MAX_REPLY + 1)
    r.need(0 < len(raw) <= pair.MAX_REPLY and raw.endswith(b'\n'), 'invalid pair update frame')
    frame = request_fields(r.parse(raw, pair.MAX_REPLY), data)
    if frame['action'] != 'receive':
        r.need(not stream.read(1), 'unexpected data after pair request')
    return frame


def reply_fields(reply, frame):
    r.fields(reply, 'operation phase target')
    r.need(reply['operation'] == frame['operation'], 'reply differs from requested operation')
    r.choice(reply['phase'], ('absent', 'prepared', 'publishing', 'checking', 'verified', 'committed',
                             'reverting', 'reverted'))
    if isinstance(reply['target'], dict) and 'version' in reply['target'] and reply['phase'] != 'absent':
        r.fields(reply['target'], 'version manifest_sha256')
        r.version(reply['target']['version'])
    else:
        r.need(reply['phase'] in ('absent', 'reverted'), 'reply requires a release version')
        r.fields(reply['target'], 'manifest_sha256')
    r.need(reply['target']['manifest_sha256'] == frame['manifest_sha256'], 'reply differs from requested release')
    return reply


def query(action, operation, digest, *, bundle=None, receipt=None, directory=pair.HEAD_DIR,
          expected_pair=None):
    """Send a request to the enrolled node using the pinned U3 host key."""
    identity = directory / 'update' / 'id_ed25519'
    data = pair.head_binding(directory, identity)
    if expected_pair is not None:
        r.need(data == expected_pair, 'current enrollment differs from the saved operation')
    frame = request_fields(dict(protocol=pair.PROTOCOL, pair=data['id'], action=action,
                                operation=operation, manifest_sha256=digest), data)
    r.need((bundle is not None and receipt is not None) if action == 'receive' else
           (bundle is None and receipt is None), 'bundle is only for the receive action')
    argv = [*pair.connection_args(data, identity, directory / 'known_hosts'),
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
            USER + '@' + data['node_ip'], COMMAND]
    with tempfile.TemporaryFile() as framed:
        framed.write(json.dumps(frame, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n')
        if action == 'receive':
            from . import update_bundle
            r.need(hashlib.sha256(receipt[0]).hexdigest() == digest, 'pair release digest differs')
            update_bundle.send(framed, bundle, receipt)
        framed.seek(0)
        try:
            raw = pair._read_ssh(argv, stdin=framed, timeout=REQUEST_TIMEOUT)
        except OSError:
            raise pair.PairUnavailable('cannot run SSH for pair update') from None
    return reply_fields(r.parse(raw, pair.MAX_REPLY), frame)


def accept(root, tree, stream):
    """Dispatch the fixed sudo command using administrator-owned grant data."""
    from . import update_node
    r.need(os.environ.get('SUDO_USER') == USER, 'pair update requires its service account')
    base = Path(paths.host(str(pair.PAIR_DIR)))
    data = grant(base, lambda path: v.bounded_read(path, pair.MAX_REPLY, trusted=True))
    frame = read_request(stream, data)
    return update_node.command(root, tree, [frame['action'], frame['operation'], frame['manifest_sha256']],
                               stream=stream)

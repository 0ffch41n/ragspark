"""Authenticated, read-only status transport for an enrolled Spark pair."""

import base64
import ipaddress
import os
from pathlib import Path
import selectors
import stat
import subprocess
import time

from . import release as r, release_verify as v


PAIR_DIR = Path('/var/lib/ragspark-pair')
HEAD_DIR = PAIR_DIR / 'head'
NODE_BINDING = PAIR_DIR / 'node.json'
NODE_VERSION = Path('/opt/ragspark/VERSION')
RECEIVER = PAIR_DIR / 'receiver' / 'pair_status.py'
USER = 'ragspark-pair'
PROTOCOL = 1
COMMAND = 'status'
MAX_REPLY = 1024
SSH_TIMEOUT = 12
UNIT_STATES = ('activating', 'active', 'deactivating', 'failed', 'inactive',
               'maintenance', 'reloading', 'refreshing', 'unknown')
COMMAND_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
ED25519_HEADER = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'


class PairUnavailable(RuntimeError):
    """A fixed diagnostic with no remote stderr or state file contents."""


def cable_address(value):
    r.string(value, r'[0-9.]{7,15}')
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise r.InvalidRelease('invalid pair IPv4 address') from None
    r.need(not (address.is_loopback or address.is_multicast or address.is_unspecified
                or int(address) == 0xffffffff), 'invalid cable address')
    return str(address)


def binding(data):
    r.fields(data, 'id head_ip node_ip')
    r.string(data['id'], r'[0-9a-f]{32}')
    head = cable_address(data['head_ip'])
    node = cable_address(data['node_ip'])
    r.need(head != node, 'the two cable addresses must differ')
    return data


def public_key(value):
    r.string(value, r'ssh-ed25519 [A-Za-z0-9+/]{68}', maximum=80)
    raw = base64.b64decode(value.split()[1], validate=True)
    r.need(raw.startswith(ED25519_HEADER), 'invalid Ed25519 public key')
    return value


def authorized_key(source, key):
    command = f'/usr/bin/python3 -I -B {RECEIVER}'
    return f'restrict,from="{cable_address(source)}",command="{command}" {public_key(key)}\n'


def known_host(node, key):
    return f'{cable_address(node)} {public_key(key)}\n'


def ssh_args(data, directory):
    state = binding(data)
    return ['/usr/bin/ssh', '-F', '/dev/null', '-4', '-n', '-T',
            '-o', 'IdentityFile=' + str(directory / 'id_ed25519'), '-b', state['head_ip'],
            '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
            '-o', 'PreferredAuthentications=publickey', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'UserKnownHostsFile=' + str(directory / 'known_hosts'),
            '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'UpdateHostKeys=no',
            '-o', 'VerifyHostKeyDNS=no', '-o', 'ClearAllForwardings=yes',
            '-o', 'ForwardAgent=no', '-o', 'ConnectTimeout=5',
            USER + '@' + state['node_ip'], COMMAND]


def _read_ssh(argv):
    deadline = time.monotonic() + SSH_TIMEOUT
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=COMMAND_ENV) as proc:
        try:
            reply = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        raise PairUnavailable('pair status timed out')
                    chunk = os.read(proc.stdout.fileno(), MAX_REPLY + 1 - len(reply))
                    if not chunk:
                        break
                    reply.extend(chunk)
                    if len(reply) > MAX_REPLY:
                        raise PairUnavailable('pair status reply exceeds the size limit')
            try:
                code = proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise PairUnavailable('pair status timed out') from None
        except BaseException:
            proc.kill()
            raise
        if code != 0:
            raise PairUnavailable('pair SSH failed; check cable, host key and enrollment')
    return bytes(reply)


def _status(reply, expected):
    r.fields(reply, 'protocol role pair version unit_state')
    r.need(r.integer(reply['protocol']) == PROTOCOL, 'incompatible pair protocol')
    r.need(reply['role'] == 'node', 'pair status is not from a node')
    r.need(binding(reply['pair']) == expected, 'pair status disagrees with local enrollment')
    r.string(reply['version'], r'[0-9a-f]{7,40}|[0-9]+\.[0-9]+\.[0-9]+', maximum=60)
    if '.' in reply['version']:
        r.version(reply['version'])
    r.choice(reply['unit_state'], UNIT_STATES)
    return reply


def query_head(directory=HEAD_DIR):
    """Query the one enrolled node using administrator-owned files."""
    data = binding(r.parse(v.bounded_read(directory / 'peer.json', MAX_REPLY, trusted=True)))
    hosts = v.bounded_read(directory / 'known_hosts', 256, trusted=True).decode('ascii')
    host, separator, key = hosts.rstrip('\n').partition(' ')
    r.need(separator and host == data['node_ip'] and hosts == known_host(host, key),
           'expected exactly one pinned key for the enrolled node')
    with v.regular_file(directory / 'id_ed25519', trusted=True) as private:
        r.need(stat.S_IMODE(os.fstat(private.fileno()).st_mode) == 0o600,
               'pair identity must be mode 0600')
    try:
        raw = _read_ssh(ssh_args(data, directory))
    except OSError:
        raise PairUnavailable('cannot run SSH for pair status') from None
    return _status(r.parse(raw, MAX_REPLY), data)


def _read_public_node_file(path):
    # The receiver runs without sudo, using only small root-owned public files.
    with v.regular_file(path) as stream:
        info = os.fstat(stream.fileno())
        r.need(info.st_uid == 0 and not info.st_mode & 0o022, 'unsafe node status file')
        data = stream.read(MAX_REPLY + 1)
        r.need(0 < len(data) <= MAX_REPLY, 'node status file size limit')
    return data


def _unit_state():
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'show', '--property=ActiveState',
                                 '--value', 'ragspark.service'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=COMMAND_ENV,
                                timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    state = result.stdout.decode('ascii', errors='replace').strip()
    return state if result.returncode == 0 and state in UNIT_STATES else 'unknown'


def node_status(command, connection):
    """Return only the enrolled identity, release marker and systemd state."""
    r.need(command == COMMAND, 'unsupported pair command')
    state = binding(r.parse(_read_public_node_file(NODE_BINDING), MAX_REPLY))
    endpoints = connection.split()
    r.need(len(endpoints) == 4 and endpoints[0] == state['head_ip']
           and endpoints[2] == state['node_ip'], 'pair status requires the enrolled cable endpoints')
    reply = {'protocol': PROTOCOL, 'role': 'node', 'pair': state,
             'version': _read_public_node_file(NODE_VERSION).decode('ascii').strip(),
             'unit_state': _unit_state()}
    return r.canonical(_status(reply, state))

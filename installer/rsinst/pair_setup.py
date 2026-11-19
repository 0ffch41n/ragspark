"""Explicit administrator enrollment for the read-only pair transport."""

import base64
import hashlib
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys
import tempfile

from . import pair_transport as transport, pair_update_transport as updates, paths, preflight, release as r
from . import release_verify as v, stackmode, ui, update_transaction as txn
from .update_source import Busy


ACCOUNT_HOME = transport.PAIR_DIR / 'account'
UPDATE_HOME = transport.PAIR_DIR / 'update-account'
RECEIVER_FILES = ('pair_status.py', 'rsinst/__init__.py', 'rsinst/pair_transport.py',
                  'rsinst/release.py', 'rsinst/release_verify.py')
UPDATE_RECEIVER_FILES = (*RECEIVER_FILES, 'pair_update.py', 'rsinst/pair_update_transport.py',
                         'rsinst/paths.py')
MAX_TOKEN = 1024
MAX_RECORD = 512
HOST_KEY = '/etc/ssh/ssh_host_ed25519_key.pub'


def token(prefix, record):
    return prefix + base64.b32encode(r.canonical(record)).decode('ascii').rstrip('=')


def read_token(prefix, prompt):
    print(prompt, flush=True)
    value = sys.stdin.readline(MAX_TOKEN + 1).strip()
    r.string(value, r'[A-Z2-7]+', MAX_TOKEN)
    r.need(value.startswith(prefix), 'wrong enrollment code type')
    encoded = value[len(prefix):]
    try:
        raw = base64.b32decode(encoded + '=' * (-len(encoded) % 8))
    except ValueError:
        raise r.InvalidRelease('invalid enrollment code') from None
    data = r.parse(raw, MAX_RECORD)
    r.need(token(prefix, data) == value, 'invalid enrollment code encoding')
    r.fields(data, 'protocol pair key')
    r.need(r.integer(data['protocol']) == transport.PROTOCOL, 'incompatible pair protocol')
    transport.binding(data['pair'])
    transport.public_key(data['key'])
    return data


def record(binding, public_key):
    return {'protocol': transport.PROTOCOL, 'pair': binding, 'key': public_key}


def read_key(path):
    raw = v.bounded_read(path, 2048, trusted=True).decode('ascii').strip().split()
    r.need(len(raw) >= 2, 'expected an Ed25519 public key')
    return transport.public_key(' '.join(raw[:2]))


def fingerprint(key):
    raw = base64.b64decode(transport.public_key(key).split()[1])
    return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode('ascii').rstrip('=')


def read_json(path):
    return r.parse(v.bounded_read(path, MAX_RECORD, trusted=True), MAX_RECORD)


def ensure_directory(path, mode):
    path.mkdir(mode=mode, exist_ok=True)
    txn.secure_directory(path)
    # The command may run with umask 077. sshd and the unprivileged receiver
    # need traversal to their public files.
    os.chmod(path, mode)


def checked_command(argv, message):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=transport.COMMAND_ENV, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise r.InvalidRelease(message) from None
    r.need(result.returncode == 0, message)


def endpoints(manifest, role):
    llm = manifest.config.get('llm')
    r.need(manifest.status == 'installed' and stackmode.is_stack(llm),
           'pairing requires an installed stack-mode profile')
    head, node = map(transport.cable_address, stackmode.addresses(manifest.config))
    r.need(head != node, 'the two cable addresses must differ')
    local = head if role == 'head' else node
    r.need(preflight.cable_interface(local) is not None, 'the configured local cable address is absent')
    return {'head_ip': head, 'node_ip': node}


def require_endpoints(binding, local):
    r.need(all(binding[key] == addr for key, addr in local.items()),
           'the enrollment addresses differ from this installation')


def create_invitation(base, local):
    pending = base / 'pending'
    if not os.path.lexists(pending):
        with tempfile.TemporaryDirectory(prefix='enroll-', dir=base) as tmp:
            stage = Path(tmp) / 'pending'
            stage.mkdir(mode=0o700)
            checked_command(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                             'ragspark-pair', '-f', str(stage / 'id_ed25519')],
                            'cannot create the pair identity')
            data = transport.binding(dict(local, id=secrets.token_hex(16)))
            txn.replace(stage / 'peer.json', r.canonical(data), 0o600)
            stage.rename(pending)
            txn.sync_dir(base)
    txn.secure_directory(pending)
    data = transport.binding(read_json(pending / 'peer.json'))
    require_endpoints(data, local)
    return record(data, read_key(pending / 'id_ed25519.pub'))


def managed_account(base, user=transport.USER, home=ACCOUNT_HOME):
    marker = base / 'account.json'
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        account = None
    if not os.path.lexists(marker):
        r.need(account is None, 'the pairing account already exists outside RAGSpark')
        txn.replace(marker, r.canonical({'uid': None}), 0o600)
    identity = read_json(marker)
    r.fields(identity, 'uid')
    uid = identity['uid']
    if uid is not None:
        r.integer(uid, 1)
        r.need(account is not None and account.pw_uid == uid, 'the managed pairing account changed')
    if account is None:
        checked_command(['/usr/sbin/useradd', '--system', '--user-group', '--no-create-home',
                         '--home-dir', str(home), '--shell', '/bin/sh', user],
                        'cannot create the pairing account')
        account = pwd.getpwnam(user)
    r.need(account.pw_uid != 0 and account.pw_gid != 0 and account.pw_dir == str(home)
           and account.pw_shell == '/bin/sh'
           and set(os.getgrouplist(user, account.pw_gid)) == {account.pw_gid},
           'the pairing account must have a dedicated home and no supplementary groups')
    if uid is None:
        txn.replace(marker, r.canonical({'uid': account.pw_uid}), 0o600)


def install_receiver(base, files=RECEIVER_FILES):
    target = base / 'receiver'
    ensure_directory(target, 0o755)
    ensure_directory(target / 'rsinst', 0o755)
    installer = Path(paths.install_dir()) / 'installer'
    for name in files:
        source = installer / name
        with v.regular_file(source, trusted=True) as stream:
            code = stream.read((128 << 10) + 1)
        r.need(len(code) <= 128 << 10, 'pair receiver source exceeds its size limit')
        txn.replace(target / name, code, 0o644)


def authorize_node(base, offer):
    accepted = base / 'accepted.json'
    if os.path.lexists(accepted):
        r.need(read_json(accepted) == offer, 'this node is paired; revoke it before enrolling another head')
    managed_account(base)
    home = base / 'account'
    ensure_directory(home, 0o755)
    ssh_dir = home / '.ssh'
    ensure_directory(ssh_dir, 0o755)
    authorization = ssh_dir / 'authorized_keys'
    line = transport.authorized_key(offer['pair']['head_ip'], offer['key']).encode('ascii')
    if os.path.lexists(authorization):
        r.need(v.bounded_read(authorization, MAX_RECORD, trusted=True) == line
               and os.path.lexists(accepted)
               and read_json(base / 'node.json') == offer['pair'],
               'existing SSH authorization differs; revoke it before enrolling')
        return
    # The key is published last. An interruption before then is retried with
    # the same invitation, and cannot grant access to a different key.
    txn.replace(accepted, r.canonical(offer), 0o600)
    install_receiver(base)
    txn.replace(base / 'node.json', r.canonical(offer['pair']), 0o644)
    txn.replace(authorization, line, 0o644)


def finish_head(base, receipt):
    pending = base / 'pending'
    txn.secure_directory(pending)
    data = transport.binding(read_json(pending / 'peer.json'))
    r.need(receipt['pair'] == data, 'the confirmation does not match the pending invitation')
    line = transport.known_host(data['node_ip'], receipt['key'])
    txn.replace(pending / 'known_hosts', line.encode('ascii'), 0o600)
    result = transport.query_head(pending)
    r.need(not os.path.lexists(base / 'head'), 'a head enrollment already exists')
    pending.rename(base / 'head')
    txn.sync_dir(base)
    return result


def create_update_invitation(base, local):
    head = base / 'head'
    data = transport.head_binding(head, head / 'id_ed25519')
    require_endpoints(data, local)
    target = head / 'update'
    if not os.path.lexists(target):
        with tempfile.TemporaryDirectory(prefix='update-key-', dir=head) as tmp:
            stage = Path(tmp) / 'identity'
            stage.mkdir(mode=0o700)
            checked_command(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                             updates.USER, '-f', str(stage / 'id_ed25519')],
                            'cannot create the update identity')
            stage.rename(target)
            txn.sync_dir(head)
    txn.secure_directory(target)
    return record(data, read_key(target / 'id_ed25519.pub'))


def install_update_sudoers():
    target = Path(paths.host(updates.SUDOERS))
    txn.secure_directory(target.parent)
    data = updates.sudo_rule()
    if os.path.lexists(target):
        with v.regular_file(target, trusted=True) as current:
            r.need(os.fstat(current.fileno()).st_mode & 0o777 == 0o440
                   and current.read(MAX_RECORD + 1) == data, 'existing update sudo rule differs')
        return
    with tempfile.TemporaryDirectory(prefix='ragspark-sudo-', dir=target.parent) as tmp:
        validate = Path(tmp) / 'rule'
        txn.replace(validate, data, 0o440)
        checked_command(['/usr/sbin/visudo', '-c', '-f', str(validate)], 'invalid update sudo rule')
    txn.replace(target, data, 0o440)


def authorize_updates(base, offer):
    enrolled = read_json(base / 'accepted.json')
    r.need(offer['pair'] == enrolled['pair'] == read_json(base / 'node.json'),
           'update invitation differs from the active pair')
    r.need(offer['key'] != enrolled['key'], 'update and status keys must differ')
    status_auth = base / 'account' / '.ssh' / 'authorized_keys'
    r.need(v.bounded_read(status_auth, MAX_RECORD, trusted=True) ==
           transport.authorized_key(offer['pair']['head_ip'], enrolled['key']).encode('ascii'),
           'status enrollment is incomplete')
    grant = base / 'update.json'
    if os.path.lexists(grant):
        r.need(read_json(grant) == offer, 'revoke the existing update key before enrolling another')
    with v.regular_file(Path(paths.host(paths.UPDATE_RUNNER)), trusted=True):
        pass
    home = base / UPDATE_HOME.name
    ensure_directory(home, 0o755)
    managed_account(home, updates.USER, UPDATE_HOME)
    ssh_dir = home / '.ssh'
    ensure_directory(ssh_dir, 0o755)
    authorization = ssh_dir / 'authorized_keys'
    line = updates.authorized_key(offer['pair']['head_ip'], offer['key']).encode('ascii')
    if os.path.lexists(authorization):
        r.need(v.bounded_read(authorization, MAX_RECORD, trusted=True) == line
               and os.path.lexists(grant), 'existing update SSH authorization differs')
    install_receiver(base, UPDATE_RECEIVER_FILES)
    install_update_sudoers()
    txn.replace(grant, r.canonical(offer), 0o644)
    txn.replace(authorization, line, 0o644)


def remove_update_grant(base):
    ssh_dir = base / UPDATE_HOME.name / '.ssh'
    if os.path.lexists(ssh_dir):
        txn.secure_directory(ssh_dir.parent)
        txn.secure_directory(ssh_dir)
        (ssh_dir / 'authorized_keys').unlink(missing_ok=True)
        txn.sync_dir(ssh_dir)
    (base / 'update.json').unlink(missing_ok=True)
    sudoers = Path(paths.host(updates.SUDOERS))
    if os.path.lexists(sudoers):
        r.need(v.bounded_read(sudoers, MAX_RECORD, trusted=True) == updates.sudo_rule(),
               'existing update sudo rule differs')
        sudoers.unlink()
        txn.sync_dir(sudoers.parent)
    txn.sync_dir(base)


def remove_node_grant(base):
    remove_update_grant(base)
    ssh_dir = base / 'account' / '.ssh'
    if os.path.lexists(ssh_dir):
        txn.secure_directory(ssh_dir.parent)
        txn.secure_directory(ssh_dir)
        (ssh_dir / 'authorized_keys').unlink(missing_ok=True)
        txn.sync_dir(ssh_dir)
    (base / 'node.json').unlink(missing_ok=True)
    (base / 'accepted.json').unlink(missing_ok=True)
    txn.sync_dir(base)


def remove_head_identity(base):
    # Retire the active paths first. This prevents a retry from using a
    # partially deleted identity directory.
    for name in ('head', 'pending'):
        directory = base / name
        if os.path.lexists(directory):
            txn.secure_directory(directory)
            with tempfile.TemporaryDirectory(prefix='retired-', dir=base) as tmp:
                directory.rename(Path(tmp) / name)
                txn.sync_dir(base)


def show_status(base, role):
    if role == 'node' and os.path.lexists(base / 'accepted.json'):
        offer = read_json(base / 'accepted.json')
        data = transport.binding(offer['pair'])
        authorization = base / 'account' / '.ssh' / 'authorized_keys'
        code = 'pair_node_pending'
        if os.path.lexists(authorization):
            line = transport.authorized_key(data['head_ip'], offer['key']).encode('ascii')
            r.need(v.bounded_read(authorization, MAX_RECORD, trusted=True) == line
                   and read_json(base / 'node.json') == data, 'incomplete or inconsistent node authorization')
            code = 'pair_node_status'
        ui.say(ui.t(code, head=data['head_ip']))
    elif role == 'head' and os.path.lexists(base / 'head'):
        show_head_status(transport.query_head(base / 'head'))
    elif role == 'head' and os.path.lexists(base / 'pending'):
        ui.say(ui.t('pair_pending'))
    else:
        ui.say(ui.t('pair_none'))


def show_head_status(result):
    ui.status('OK', ui.t('pair_connected', node=result['pair']['node_ip'],
                         version=result['version'], state=result['unit_state']))


def command(args, manifest):
    if manifest is None or manifest.status != 'installed':
        ui.status('FAIL', ui.t('pair_requires_install'))
        return 1
    base = Path(paths.host(str(transport.PAIR_DIR)))
    role = manifest.config.get('role', 'head')
    action = args.pair_action
    try:
        r.choice(role, paths.ROLES)
        r.need(action in ('accept', 'update-accept', 'status', 'revoke') or role == 'head',
               'run this command on the head')
        r.need(action != 'accept' or role == 'node', 'run pair accept on the second Spark')
        r.need(action != 'update-accept' or role == 'node', 'run pair update-accept on the second Spark')
        if action in ('status', 'revoke') and not os.path.lexists(base):
            ui.say(ui.t('pair_none'))
            return 0
        if action == 'status':
            txn.secure_directory(base)
        else:
            ensure_directory(base, 0o755)
        with txn.lock(base / 'pair.lock', exclusive=action != 'status'):
            if action == 'status':
                show_status(base, role)
            elif action == 'revoke':
                prompt = 'pair_revoke_confirm' if role == 'node' else 'pair_remove_identity_confirm'
                if not ui.confirm(ui.t(prompt)):
                    return 1
                if role == 'node':
                    remove_node_grant(base)
                    ui.status('OK', ui.t('pair_revoked'))
                else:
                    remove_head_identity(base)
                    ui.status('OK', ui.t('pair_identity_removed'))
                    ui.say(ui.t('pair_revoke_node_hint'))
            else:
                return enroll(base, role, action, manifest)
    except Busy:
        ui.status('FAIL', ui.t('pair_busy'))
        return 4
    except (OSError, ValueError, EOFError, KeyError, transport.PairUnavailable) as exc:
        detail = str(exc) if isinstance(exc, (r.InvalidRelease, transport.PairUnavailable)) else ui.t('pair_input')
        ui.status('FAIL', ui.t('pair_error', detail=detail))
        return 1
    return 0


def enroll(base, role, action, manifest):
    local = endpoints(manifest, role)
    if action == 'update-invite':
        offer = create_update_invitation(base, local)
        ui.say(ui.t('pair_update_invitation', fingerprint=fingerprint(offer['key'])))
        print(token('RSU', offer), flush=True)
    elif action == 'update-accept':
        offer = read_token('RSU', ui.t('pair_update_prompt'))
        require_endpoints(offer['pair'], local)
        if not ui.confirm(ui.t('pair_update_confirm', head=local['head_ip'],
                              fingerprint=fingerprint(offer['key']))):
            return 1
        authorize_updates(base, offer)
        ui.status('OK', ui.t('pair_update_allowed'))
    elif action == 'begin':
        if os.path.lexists(base / 'head'):
            show_status(base, role)
        else:
            offer = create_invitation(base, local)
            ui.say(ui.t('pair_invitation_hint', node=local['node_ip']))
            print(token('RSI', offer), flush=True)
    elif action == 'accept':
        offer = read_token('RSI', ui.t('pair_invitation_prompt'))
        require_endpoints(offer['pair'], local)
        host_key = read_key(Path(paths.host(HOST_KEY)))
        if not ui.confirm(ui.t('pair_accept_confirm', head=local['head_ip'],
                              fingerprint=fingerprint(offer['key']))):
            return 1
        authorize_node(base, offer)
        ui.say(ui.t('pair_confirmation_hint', fingerprint=fingerprint(host_key)))
        print(token('RSR', record(offer['pair'], host_key)), flush=True)
    else:
        if os.path.lexists(base / 'head'):
            show_status(base, role)
            return 0
        r.need(os.path.lexists(base / 'pending'), 'start pairing with sudo ragspark pair')
        receipt = read_token('RSR', ui.t('pair_confirmation_prompt'))
        require_endpoints(receipt['pair'], local)
        if not ui.confirm(ui.t('pair_finish_confirm', fingerprint=fingerprint(receipt['key']))):
            return 1
        show_head_status(finish_head(base, receipt))
    return 0

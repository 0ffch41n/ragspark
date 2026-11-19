"""Participant in a signed code-only update, run by the stable node runner."""
from contextlib import nullcontext
import os
import shutil
import sys

from . import release as r, release_verify as v, update_apply, update_bootstrap, update_bundle
from . import update_check as check, update_transaction as txn


INBOX = 'pair-inbox'
PREPARATION = 'pair-preparation'
CANCELLATION = 'pair-cancelled.json'


def prepare(root, tree, config, area, operation_id, digest, bundle):
    r.need(not txn.pending(root), 'a local update requires recovery')
    txn.secure_directory(bundle)

    def save(plan, candidate, before, after, current, target, receipt):
        if not os.path.lexists(area):
            area.mkdir(mode=0o700)
            txn.sync_dir(root)
        return txn.prepare(area, tree, candidate, before, after, current, target,
                           receipts={'previous': update_apply.saved_receipt(root), 'target': receipt},
                           containers=plan['containers'], pair_operation=operation_id)

    return check.check(root, tree, bundle, config, save, pair_release=digest)


def node_config(tree):
    config = check.installed_config(tree)
    r.need(config.get('role') == 'node', 'pair preparation runs on the node')
    return config


def saved_operation(root, area, operation_id, digest):
    for directory in (root, area):
        operation = txn.operation_path(directory)
        if not os.path.lexists(operation):
            continue
        plan = txn.load_plan(operation)
        if 'pair_operation' not in plan:
            continue
        r.need('pair' not in plan, 'invalid node update journal')
        if plan['pair_operation'] == operation_id:
            r.need(plan['target_source'].get('manifest_sha256') == digest, 'saved pair release differs')
            return directory, operation, plan
        r.need(plan['phase'] in txn.TERMINAL, 'saved pair operation differs')
    return None, None, None


def saved_request(root, tree, area, directory, operation, plan, action):
    phase = plan['phase']
    if action in ('prepare', 'status'):
        return plan
    if action == 'commit':
        r.need(directory == root, 'pair code has not been published')
        txn.commit_pair(operation, plan)
    elif action == 'cancel':
        r.need(phase != 'committed', 'pair is already committed')
        if phase == 'prepared':
            txn.mark(operation, plan, 'reverted')
        elif phase != 'reverted':
            r.need(directory == root, 'pair code journal is not active')
            update_apply.verify_receipt(root, 'previous', plan)
            plan['phase'] = txn.run(root, tree, update_apply.verifier(root, plan['containers'], True),
                                    rollback=True)
    elif action == 'apply':
        r.need(phase not in ('reverting', 'reverted'), 'pair is being rolled back')
        if phase == 'prepared':
            update_bootstrap.ensure_ready('node')
            r.need(update_apply.observe(tree) == plan['containers'], 'node containers changed after preparation')
            if directory == area:
                txn.activate(root, operation)
        if phase not in ('verified', 'committed'):
            update_apply.verify_receipt(root, 'target', plan)
            plan['phase'] = txn.run(root, tree, update_apply.verifier(root, plan['containers'], True),
                                    defer_commit=True)
    return plan


def request(root, tree, action, operation_id, digest, bundle=None):
    config = node_config(tree)
    area = root / PREPARATION
    if os.path.lexists(area):
        txn.secure_directory(area)
    directory, operation, plan = saved_operation(root, area, operation_id, digest)
    if plan:
        return saved_request(root, tree, area, directory, operation, plan, action)
    tombstone = root / CANCELLATION
    if os.path.lexists(tombstone):
        saved = r.parse(v.bounded_read(tombstone, 1024, trusted=True), 1024)
        r.fields(saved, 'operation manifest_sha256')
        if saved['operation'] == operation_id:
            r.need(saved['manifest_sha256'] == digest, 'saved pair release differs')
            r.need(action in ('prepare', 'status', 'cancel'), 'pair was cancelled')
            return {'pair_operation': operation_id, 'phase': 'reverted',
                    'target_source': {'manifest_sha256': digest}}
    if action == 'prepare':
        return prepare(root, tree, config, area, operation_id, digest, bundle or root / INBOX)
    r.need(action in ('status', 'cancel'), 'no saved pair preparation')
    if action == 'cancel':
        txn.replace(tombstone, r.canonical({'operation': operation_id, 'manifest_sha256': digest}), 0o600)
    return {'pair_operation': operation_id, 'phase': 'reverted' if action == 'cancel' else 'absent',
            'target_source': {'manifest_sha256': digest}}


def receive(root, tree, operation_id, digest, stream):
    node_config(tree)
    with txn.lock(root / 'pair-transfer.lock'):
        directory = root / 'pair-transfer'
        if os.path.lexists(directory):
            txn.secure_directory(directory)
            shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
        try:
            update_bundle.receive(stream, directory, root / 'trust', digest)
            with txn.lock(root / 'maintenance.lock'):
                return request(root, tree, 'prepare', operation_id, digest, bundle=directory)
        finally:
            shutil.rmtree(directory)


def command(root, tree, args, *, stream=None):
    r.need(len(args) == 3, 'expected update-node action, operation ID and release digest')
    action = r.choice(args[0], ('prepare', 'receive', 'status', 'apply', 'commit', 'cancel'))
    operation_id = r.string(args[1], r'[0-9a-f]{32}')
    digest = r.digest(args[2])
    if action == 'receive':
        plan = receive(root, tree, operation_id, digest, stream if stream is not None else sys.stdin.buffer)
    else:
        transfer = txn.lock(root / 'pair-transfer.lock') if action == 'cancel' else nullcontext()
        with transfer, txn.lock(root / 'maintenance.lock', exclusive=action != 'status'):
            plan = request(root, tree, action, operation_id, digest)
    reply = {'operation': plan['pair_operation'], 'phase': plan['phase'], 'target': plan['target_source']}
    print(r.canonical(reply).decode('utf-8'), end='')
    return 0

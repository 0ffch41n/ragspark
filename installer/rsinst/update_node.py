"""Prepare a signed code-only release on the node through the stable runner."""
import os

from . import release as r, update_apply, update_check as check, update_transaction as txn


INBOX = 'pair-inbox'
PREPARATION = 'pair-preparation'


def prepare(root, tree, config, area, operation_id, digest):
    r.need(not txn.pending(root), 'a local update requires recovery')
    bundle = root / INBOX
    txn.secure_directory(bundle)

    def save(plan, candidate, before, after, current, target, receipt):
        if not os.path.lexists(area):
            area.mkdir(mode=0o700)
            txn.sync_dir(root)
        return txn.prepare(area, tree, candidate, before, after, current, target,
                           receipts={'previous': update_apply.saved_receipt(root), 'target': receipt},
                           containers=plan['containers'], pair_operation=operation_id)

    return check.check(root, tree, bundle, config, save, pair_release=digest)


def request(root, tree, action, operation_id, digest):
    config = check.installed_config(tree)
    r.need(config.get('role') == 'node', 'pair preparation runs on the node')
    area = root / PREPARATION
    if os.path.lexists(area):
        txn.secure_directory(area)
    operation = txn.operation_path(area)
    if os.path.lexists(operation):
        plan = txn.load_plan(operation)
        r.need('pair_operation' in plan and plan['phase'] in ('prepared', 'reverted'),
               'invalid saved pair preparation')
        if plan['pair_operation'] == operation_id:
            r.need(plan['target_source']['manifest_sha256'] == digest, 'saved pair release differs')
            if action == 'cancel' and plan['phase'] == 'prepared':
                txn.mark(operation, plan, 'reverted')
                plan['phase'] = 'reverted'
            return plan
        r.need(action == 'prepare' and plan['phase'] == 'reverted', 'saved pair operation differs')
    r.need(action == 'prepare', 'no saved pair preparation')
    return prepare(root, tree, config, area, operation_id, digest)


def command(root, tree, args):
    r.need(len(args) == 3, 'expected update-node action, operation ID and release digest')
    action = r.choice(args[0], ('prepare', 'status', 'cancel'))
    operation_id = r.string(args[1], r'[0-9a-f]{32}')
    digest = r.digest(args[2])
    with txn.lock(root / 'maintenance.lock', exclusive=action != 'status'):
        plan = request(root, tree, action, operation_id, digest)
    reply = {'operation': plan['pair_operation'], 'phase': plan['phase'], 'target': plan['target_source']}
    print(r.canonical(reply).decode('utf-8'), end='')
    return 0

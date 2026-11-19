"""Durable coordinator for code-only updates of the paired Sparks."""
from pathlib import Path
import secrets

from . import pair_transport, pair_update_transport as transport, paths, release as r, stackmode, ui
from . import update_apply as local, update_bootstrap, update_check as check, update_transaction as txn
from .update_source import PreparationError


def head_directory():
    return Path(paths.host(str(pair_transport.HEAD_DIR)))


def peer_request(plan, action, **payload):
    reply = transport.query(action, plan['pair_operation'], plan['target_source']['manifest_sha256'],
                            directory=head_directory(), expected_pair=plan['pair']['peer'], **payload)
    if 'version' in reply['target']:
        r.need(reply['target'] == plan['target_source'], 'node reported another target release')
    return reply['phase']


def decide(operation, plan, decision):
    r.need(plan['pair']['decision'] is None, 'pair decision is already recorded')
    plan['pair']['decision'] = decision
    txn.mark(operation, plan, plan['phase'])


def complete(root, tree, operation, plan):
    decision = plan['pair']['decision']
    if decision == 'rollback':
        r.need(peer_request(plan, 'cancel') == 'reverted', 'node did not confirm rollback')
        if plan['phase'] == 'prepared':
            txn.mark(operation, plan, 'reverted')
            return 'reverted'
        local.verify_receipt(root, 'previous', plan)
        return txn.run(root, tree, local.verifier(root, plan['containers'], True), rollback=True)

    if decision is None:
        local.verify_receipt(root, 'target', plan)
        phase = peer_request(plan, 'status')
        if phase == 'absent':
            r.need(plan['phase'] == 'prepared', 'node lost its pair preparation')
            phase = peer_request(plan, 'receive', bundle=operation / 'bundle',
                                 receipt=local.saved_receipt(operation / 'target'))
        if phase in ('prepared', 'publishing', 'checking'):
            phase = peer_request(plan, 'apply')
        r.need(phase == 'verified', 'node did not verify the pair release')
        if plan['phase'] == 'prepared':
            r.need(local.observe(tree) == plan['containers'], 'head containers changed after preparation')
        plan['phase'] = txn.run(root, tree, local.verifier(root, plan['containers'], True), defer_commit=True)
        r.need(plan['phase'] == 'verified', 'head did not verify the pair release')
        decide(operation, plan, 'commit')

    r.need(peer_request(plan, 'commit') == 'committed', 'node did not confirm the pair decision')
    return txn.commit_pair(operation, plan)


def recover(root, tree, rollback):
    operation = txn.operation_path(root)
    plan = txn.load_plan(operation)
    decision = plan['pair']['decision']
    if rollback and decision == 'commit':
        ui.status('FAIL', ui.t('up_pair_commit_decided'))
        return 3
    if plan['phase'] in txn.TERMINAL:
        ui.status('OK', ui.t('up_done_phase', phase=plan['phase']))
        return 0
    try:
        if rollback and decision is None:
            if not ui.confirm(ui.t('up_confirm_rollback')):
                return 8
            decide(operation, plan, 'rollback')
        phase = complete(root, tree, operation, plan)
    except (OSError, ValueError, PreparationError, pair_transport.PairUnavailable) as exc:
        ui.status('FAIL', ui.t('up_prepare', detail=str(exc)))
        ui.status('FAIL', ui.t('up_recover'))
        return 7
    ui.status('OK', ui.t('up_pair_done', phase=phase))
    return 0


def publish(root, tree, plan, candidate, before, after, previous, target, receipt):
    directory = head_directory()
    peer = pair_transport.head_binding(directory, directory / 'update/id_ed25519')
    r.need((peer['head_ip'], peer['node_ip']) == stackmode.addresses(check.installed_config(tree)),
           'pair enrollment differs from the installation addresses')
    update_bootstrap.ensure_ready('head')
    check.show(plan)
    ui.say(ui.t('up_pair_peer', node=peer['node_ip']))
    if not ui.confirm(ui.t('up_pair_confirm')):
        return 8
    txn.prepare(root, tree, candidate, before, after, previous, target,
                receipts={'previous': local.saved_receipt(root), 'target': receipt},
                containers=plan['containers'], pair_operation=secrets.token_hex(16),
                pair={'peer': peer, 'decision': None}, bundle=candidate.parent)
    return recover(root, tree, False)

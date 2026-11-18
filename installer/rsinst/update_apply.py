"""Connect an authenticated update plan to the independent recovery journal."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from . import paths, release as r, release_inventory as inventory, release_verify as v, stackmode, ui, update_bootstrap
from . import update_check as check, update_host as host, update_transaction as txn
from .update_source import Busy, PreparationError


def saved_receipt(directory):
    manifest, signature = (directory / name for name in txn.RECEIPT)
    if not os.path.lexists(manifest) and not os.path.lexists(signature):
        return None
    return (v.bounded_read(manifest, r.MAX_MANIFEST, trusted=True),
            v.bounded_read(signature, v.MAX_SIGNATURE, trusted=True))


def verify_receipt(root, side, plan):
    """A resumed publication must still have trusted signature and inventory."""
    operation = txn.operation_path(root)
    identity = plan[side + '_source']
    expected = plan[side]['inventory']
    receipt = saved_receipt(operation / side)
    trust = root / 'trust'
    if receipt is None:
        baseline = r.parse(v.bounded_read(trust / 'baseline.json', r.MAX_MANIFEST, trusted=True))
        r.need('legacy_commit' in identity and identity == baseline['source']
               and inventory.legacy_baseline(identity['legacy_commit'], expected) == identity,
               'saved legacy revision differs from the trusted baseline')
        return
    raw, signature = receipt
    revoked = trust / 'revoked'
    v.verify_signature(raw, signature, trust / 'allowed_signers', 'ragspark-release-v1',
                       revoked if os.path.lexists(revoked) else None)
    release = r.manifest(raw)
    r.need(identity == {'version': release['version'], 'manifest_sha256': hashlib.sha256(raw).hexdigest()}
           and check.code_artifact(release)['files'] == expected, 'saved release proof differs from journal')


def observe(tree):
    """Record identities and readiness without creating containers or inspecting secrets."""
    raw = host.output(['docker', 'compose', 'ps', '--all', '--format', 'json'], cwd=tree / 'compose')
    try:
        rows = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if isinstance(rows, dict):
        rows = [rows]
    if not any(row['State'] == 'running' for row in rows):
        r.need(all(row['State'] in ('exited', 'created') for row in rows), 'stack is changing state')
        return {}
    declared = set(host.output(['docker', 'compose', 'config', '--services'], cwd=tree / 'compose').decode().split())
    r.need(bool(declared) and {row['Service'] for row in rows} == declared
           and all(row['State'] == 'running' and row.get('Health', '') in ('healthy', '') and row['ID']
                   for row in rows), 'stack is not ready')
    data = r.parse(v.bounded_read(tree / 'manifest.json', 4 << 20))
    from .control import _health_url
    r.need(stackmode.http_ok(_health_url(data['config']), timeout=10), 'RAGFlow health check failed')
    return {row['Service']: row['ID'] for row in rows}


def verify_cli(root, tree):
    # Use an empty bytecode root, including when the legacy tree has caches.
    with tempfile.TemporaryDirectory(prefix='verify-', dir=root) as cache:
        args = [sys.executable, '-I', '-B', '-X', 'pycache_prefix=' + cache,
                str(tree / 'installer/ragspark.py'), '--help']
        try:
            subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError):
            raise PreparationError('published CLI failed its startup check') from None


def verifier(root, expected, after_interruption=False):
    def verify(tree, _side):
        verify_cli(root, tree)
        observed = observe(tree)
        # Boot is gated while a journal is open, so a reboot can leave every
        # service down until recovery completes and the administrator starts it.
        r.need(observed == expected or (after_interruption and not observed),
               'container identities changed during code publication')
    return verify


def publish(root, tree, plan, candidate, before, after, previous, target, receipt):
    check.show(plan)
    service_change = plan['transition']['impact'] == 'services'
    if service_change:
        ui.say(ui.t('up_service_window'))
    question = 'up_confirm_service_apply' if service_change else 'up_confirm_apply'
    if not ui.confirm(ui.t(question)):
        return 8
    update_bootstrap.ensure_ready()
    expected = observe(tree)
    service = None
    if service_change:
        from . import update_services
        service = update_services.plan(root, tree, plan['llm'], expected, plan['images'])
    txn.prepare(root, tree, candidate, before, after, previous, target,
                receipts={'previous': saved_receipt(root), 'target': receipt}, containers=expected,
                service_update=service)
    if service_change:
        return complete_service_update(root, tree, rollback=False, publishing=True)
    verify = verifier(root, expected)
    try:
        txn.run(root, tree, verify)
    except (OSError, ValueError, PreparationError, KeyboardInterrupt):
        try:
            journal = txn.load_plan(txn.operation_path(root))
            verify_receipt(root, 'previous', journal)
            txn.run(root, tree, verify, rollback=True)
        except (OSError, ValueError, PreparationError, KeyboardInterrupt):
            ui.status('FAIL', ui.t('up_recover'))
            return 7
        ui.status('FAIL', ui.t('up_restored'))
        return 6
    ui.status('OK', ui.t('up_applied'))
    return 0


def perform_service_update(root, tree, rollback):
    from . import update_services as service

    operation = txn.operation_path(root)
    journal = txn.load_plan(operation)
    config = journal['service_update']
    paths.set_role('head')
    state = service.status(operation)
    phase = journal['phase']
    if rollback:
        r.need(state != 'opening-target' and phase != 'committed',
               'RAGFlow has reopened; rollback requires a new signed maintenance release')
    if phase in txn.TERMINAL:
        service.finish(operation, config)
        return phase
    reverting = rollback or phase == 'reverting' or state in ('cancelling', 'reopening-source')
    side = 'previous' if reverting else 'target'
    verify_receipt(root, side, journal)
    if reverting and (phase == 'prepared' or state in ('cancelling', 'reopening-source')):
        service.cancel_prepublication(tree, operation, journal, config)
        phase = 'reverted'
    else:
        if not reverting:
            service.quiesce(tree, operation, config)

        def verify(current_tree, selected):
            verify_cli(root, current_tree)
            service.verify_start(current_tree, operation, config, selected)
            after = observe(current_tree)
            before = journal['containers']
            untouched = set(before) - {service.WEB, *config['restart']}
            r.need(all(after.get(name) == before[name] for name in untouched),
                   'an unaffected container was recreated during maintenance')

        phase = txn.run(root, tree, verify, rollback=reverting)
    service.finish(operation, config)
    return phase


def complete_service_update(root, tree, rollback, publishing=False):
    from . import update_services as service

    try:
        phase = perform_service_update(root, tree, rollback)
    except (OSError, ValueError, PreparationError, KeyboardInterrupt):
        state = service.status(txn.operation_path(root))
        if not publishing or state in ('opening-target', 'finished'):
            ui.status('FAIL', ui.t('up_recover'))
            return 7
        try:
            perform_service_update(root, tree, rollback=True)
        except (OSError, ValueError, PreparationError, KeyboardInterrupt):
            ui.status('FAIL', ui.t('up_recover'))
            return 7
        ui.status('FAIL', ui.t('up_restored'))
        return 6
    ui.status('OK', ui.t('up_done_phase', phase=phase))
    return 0


def recover(root, tree, rollback):
    operation = txn.operation_path(root)
    r.need(os.path.lexists(operation), 'no saved update operation')
    plan = txn.load_plan(operation)
    phase = plan['phase']
    if 'service_update' in plan:
        from . import update_services
        state = update_services.status(operation)
        if rollback and (state == 'opening-target' or phase == 'committed'):
            ui.status('FAIL', ui.t('up_rollback_after_open'))
            return 3
        if rollback and phase != 'reverted' and not ui.confirm(ui.t('up_confirm_rollback')):
            return 8
        update_bootstrap.ensure_ready()
        return complete_service_update(root, tree, rollback)
    if phase == 'reverted' or phase == 'committed' and not rollback:
        ui.status('OK', ui.t('up_done_phase', phase=phase))
        return 0
    if rollback and phase != 'reverting' and not ui.confirm(ui.t('up_confirm_rollback')):
        return 8
    # The active tree may be incomplete. Query Compose before projection
    # only when deliberately reverting a previously committed installation.
    try:
        update_bootstrap.ensure_ready()
        expected = observe(tree) if phase == 'committed' else plan['containers']
        verify_receipt(root, 'previous' if rollback or phase == 'reverting' else 'target', plan)
        phase = txn.run(root, tree, verifier(root, expected, after_interruption=phase != 'committed'),
                        rollback=rollback)
    except (OSError, ValueError, PreparationError, KeyboardInterrupt):
        ui.status('FAIL', ui.t('up_recover'))
        return 7
    ui.status('OK', ui.t('up_done_phase', phase=phase))
    return 0


def command(args, gated=False):
    ui.set_lang(args.lang or ui.detect_lang())
    ui.set_yes(args.yes)
    if os.geteuid() != 0 and os.environ.get('RAGSPARK_ALLOW_NONROOT') != '1':
        ui.status('FAIL', ui.t('need_root', cmd='sudo ragspark update --check'))
        return 5
    if (args.resume or args.rollback) and args.bundle is not None:
        ui.status('FAIL', ui.t('up_recovery_source'))
        return 2
    root = Path(paths.host(paths.UPDATE_DIR))
    tree = Path(paths.install_dir())
    if not gated and not args.check:
        ui.status('FAIL', ui.t('up_gate_required'))
        return 5
    try:
        # An installed gate has already locked the inode before importing here.
        context = (txn.lock(root / 'maintenance.lock', exclusive=False)
                   if not gated and (root / 'maintenance.lock').exists() else nullcontext())
        with context:
            if args.resume or args.rollback:
                return recover(root, tree, args.rollback)
            if txn.pending(root):
                ui.status('FAIL', ui.t('up_recover'))
                return 7
            if args.check:
                return check.update(args)
            return check.update(args, lambda *values: publish(root, tree, *values))
    except Busy:
        ui.status('FAIL', ui.t('up_busy'))
        return 4
    except (OSError, ValueError, PreparationError) as exc:
        ui.status('FAIL', ui.t('up_prepare', detail=str(exc)))
        return 5
    except KeyboardInterrupt:
        ui.status('FAIL', ui.t('up_recover') if txn.pending(root) else ui.t('cancelled'))
        return 7 if txn.pending(root) else 8

"""Journalled maintenance for compatible single-node vLLM changes.

No data restore is performed by this runner. The RAGFlow port is removed
during validation, and opening it is a durable forward-only boundary.
"""
import json
import os
from pathlib import Path
import tempfile
import re
import shutil
import time

from . import backup, backup_remote, paths, release as r, release_verify as v, ui
from . import update_bootstrap as bootstrap, update_host as host, update_transaction as txn
from .sysutil import run
from .update_source import PreparationError

SERVICE = 'vllm-llm'
WEB = backup.WEB
FLAGS = {'--max-num-seqs', '--max-num-batched-tokens'}
TIMERS = (paths.WATCH_TIMER, paths.BACKUP_TIMER, backup_remote.SYNC_TIMER)
STATES = ('planned', 'closing', 'quiesced', 'backed-up', 'opening-target',
          'opening-previous', 'cancelling', 'reopening-source', 'finished')
PROGRESS = 'service-state.json'
OVERLAY = 'private-web.yaml'
PRIVATE_WEB = b'services:\n  ragflow:\n    ports: !reset []\n'


def scheduler_args(command):
    """Split the allowlisted numeric scheduler flags from the fixed argv."""
    r.need(isinstance(command, list) and all(isinstance(x, str) for x in command),
           'vLLM command is not an argument list')
    fixed, flags = [], {}
    words = iter(command)
    for word in words:
        if word in FLAGS:
            r.need(word not in flags, 'repeated vLLM scheduler flag')
            value = next(words, '')
            r.need(re.fullmatch('[1-9][0-9]{0,5}', value) is not None, 'invalid vLLM scheduler flag')
            flags[word] = value
        else:
            fixed.append(word)
    return fixed, flags


def compatible(old, target, changed, declared, llm):
    """Prove a scheduler-only change. Data and image compatibility is structural."""
    r.need(declared == [SERVICE], 'service apply supports only a single vLLM scheduler change')
    a, b = (x['services'] for x in (old, target))
    r.need(set(a) == set(b) == {WEB, SERVICE, 'vllm-embed', 'vllm-rerank', *backup.DATA},
           'unexpected services in single installation')
    r.need({k: v for k, v in old.items() if k != 'services'}
           == {k: v for k, v in target.items() if k != 'services'}, 'Compose topology changed')
    r.need(all(a[k] == b[k] for k in a.keys() - {SERVICE}), 'other services or data dependencies changed')
    old_fixed, old_flags = scheduler_args(a[SERVICE]['command'])
    new_fixed, new_flags = scheduler_args(b[SERVICE]['command'])
    r.need(old_fixed == new_fixed and old_flags != new_flags
           and {k: v for k, v in a[SERVICE].items() if k != 'command'}
           == {k: v for k, v in b[SERVICE].items() if k != 'command'}, 'unverified vLLM configuration change')
    allowed = {'compose/compose.yaml', 'compose/llm/' + llm + '.yaml'}
    r.need(not any(p.startswith('compose/') and p not in allowed for p in changed),
           'a Compose support file changed')
    r.need(all(not svc.get('network_mode') and not svc.get('external_links') for svc in a.values())
           and set(a[WEB].get('networks', {})) == {'default', 'backend'}
           and a[WEB].get('restart') == 'on-failure:5'
           and all(net.get('driver', 'bridge') == 'bridge'
                   and not net.get('driver_opts') and not net.get('external')
                   for net in old['networks'].values())
           and old['networks']['backend'].get('internal') is True
           and not any(svc.get('ports') for name, svc in a.items() if name != WEB),
           'the standard isolated single stack is required')


def validate_config(data):
    r.fields(data, 'restart timers backup llm')
    r.need(data['restart'] == [SERVICE], 'invalid journal service restart set')
    entries = r.array(data['timers'], maximum=len(TIMERS))
    for timer in entries:
        r.choice(timer, TIMERS)
    r.need(len(entries) == len(set(entries)), 'duplicate saved timer')
    r.string(data['backup'], r'ragspark-[0-9]{8}-[0-9]{6}-before-update')
    r.string(data['llm'], r'[a-z0-9][a-z0-9.-]{0,79}')
    return data


def status(operation):
    path = operation / PROGRESS
    if not os.path.lexists(path):
        return 'planned'
    data = r.parse(v.bounded_read(path, 1024, trusted=True))
    return r.choice(data, STATES)


def save(operation, state):
    txn.replace(operation / PROGRESS, r.canonical(r.choice(state, STATES)), 0o600)


def systemctl(action, timer):
    code, _ = run(['systemctl', action, timer], timeout=45, log_output=False)
    if code:
        raise PreparationError('cannot ' + action + ' ' + timer)


def active_timers():
    enabled = []
    for timer in TIMERS:
        state = bootstrap.unit_state(timer, ('LoadState', 'ActiveState'))
        if state.get('LoadState') == 'not-found':
            continue
        r.need(state.get('LoadState') == 'loaded', 'cannot determine timer state: ' + timer)
        value = r.choice(state.get('ActiveState'), ('active', 'inactive', 'failed'))
        if value == 'active':
            enabled.append(timer)
    return enabled


def plan(root, tree, llm, containers, images):
    r.need(set(containers) == set(images), 'apply requires the running single stack')
    for name, ident in containers.items():
        actual = host.output(['docker', 'inspect', '--format', '{{.Image}}', ident]).decode().strip()
        r.need(actual == images[name], 'running image differs from the signed profile: ' + name)
    backup_name = 'ragspark-' + time.strftime('%Y%m%d-%H%M%S') + '-before-update'
    backup_path = backup_root() / backup_name
    r.need(not os.path.lexists(backup_path), 'an update backup already has this name')
    saved = validate_config({'restart': [SERVICE], 'timers': active_timers(), 'backup': backup_name, 'llm': llm})
    # Feature-probe Compose and prove the port is absent before any downtime.
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        overlay = Path(tmp) / OVERLAY
        txn.replace(overlay, PRIVATE_WEB, 0o600)
        prove_private(tree, saved, overlay)
    return saved


def compose_command(config, overlay=None):
    args = ['docker', 'compose', '--env-file', '.env', '-f', 'compose.yaml',
            '-f', 'llm/' + config['llm'] + '.yaml']
    if overlay is not None:
        args.extend(['-f', str(overlay)])
    return args


def prove_private(tree, config, overlay):
    raw = host.output([*compose_command(config, overlay), 'config', '--format', 'json'],
                      cwd=tree / 'compose')
    r.need(not json.loads(raw)['services'][WEB].get('ports'), 'private RAGFlow overlay leaves a published port')


def run_compose(tree, config, argv, overlay=None, timeout=2400):
    command = [*compose_command(config, overlay), *argv]
    code, _ = run(command, cwd=tree / 'compose', timeout=timeout, log_output=False)
    if code:
        raise PreparationError('Compose ' + argv[0] + ' failed')


def up(tree, config, services, overlay=None, keep=False):
    args = ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '2400']
    if keep:
        args.append('--no-recreate')
    run_compose(tree, config, [*args, *services], overlay)


def backup_root():
    return Path(paths.host(paths.BACKUP_DIR)) / 'update-safety'


def verified_backup(config):
    root = backup_root()
    target = root / config['backup']
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    txn.secure_directory(root)
    if not target.is_dir():
        mounts = backup._mounts()
        r.need(bool(mounts) and backup._space(str(root), mounts), 'cannot reserve space for update backup')
        from .manifest import Manifest
        manifest = Manifest.load()
        r.need(manifest is not None, 'installation manifest unavailable for backup')
        incomplete = root / ('.incomplete-' + config['backup'])
        if os.path.lexists(incomplete):
            txn.secure_directory(incomplete)
            shutil.rmtree(incomplete)
        backup._write(str(root), config['backup'], mounts, backup._describe(config['backup'], manifest))
    txn.secure_directory(target)
    info = r.parse(v.bounded_read(target / 'backup.json', 1 << 20, trusted=True))
    current_version = (Path(paths.install_dir()) / 'VERSION').read_text().strip()
    r.need(info.get('name') == config['backup'] and info.get('release') == current_version
           and backup.verify(str(target), info) is None, 'update backup verification failed')
    for record in list(info['volumes'].values()) + list(info['files'].values()) + [{'file': 'backup.json'}]:
        with v.regular_file(target / record['file'], trusted=True) as stream:
            os.fsync(stream.fileno())
    txn.sync_dir(target)
    txn.sync_dir(root)
    ui.status('OK', ui.t('up_backup_verified', path=target))


def quiesce(tree, operation, config):
    state = status(operation)
    if state in ('backed-up', 'opening-target'):
        return
    r.need(state in ('planned', 'closing', 'quiesced'), 'invalid forward maintenance state')
    for timer in config['timers']:
        systemctl('stop', timer)
    if state == 'planned':
        # After a reboot or an interrupted stop we may need only MySQL for
        # the parsing check. RAGFlow remains closed once we have stopped it.
        if backup.parsing() is None:
            up(tree, config, ['mysql'], keep=True)
        r.need(backup.wait_parsing(backup.WAIT_PARSE_MIN), 'RAGFlow parsing did not finish')
        save(operation, 'closing')
    if state in ('planned', 'closing'):
        run_compose(tree, config, ['stop', WEB])
        if backup.parsing() is None:
            up(tree, config, ['mysql'], keep=True)
        r.need(backup.parsing() == 0, 'documents entered parsing while closing RAGFlow')
        save(operation, 'quiesced')
    error = backup.stop(backup.DATA)
    r.need(not error, 'cannot quiesce RAGFlow data volumes')
    verified_backup(config)
    save(operation, 'backed-up')


def probe_model(tree, config):
    # Run on the model's loopback. The model is not published on the host.
    probe = r"""
import json, urllib.request
base = 'http://127.0.0.1:8000/v1'
with urllib.request.urlopen(base+'/models', timeout=10) as response:
    model = json.load(response)['data'][0]['id']
data = dict(model=model, max_tokens=32, temperature=0,
            messages=[dict(role='user', content='What is 2 + 2? Reply with one digit.')])
request = urllib.request.Request(base+'/chat/completions', data=json.dumps(data).encode(),
                                 headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request, timeout=60) as response:
    content = json.load(response)['choices'][0]['message']['content']
assert isinstance(content, str) and '4' in content
"""
    run_compose(tree, config, ['exec', '-T', SERVICE, 'python3', '-c', probe], timeout=90)


def verify_private_web(tree, config, overlay):
    prove_private(tree, config, overlay)
    create = ['up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', WEB]
    run_compose(tree, config, create, overlay)
    container = host.output([*compose_command(config, overlay), 'ps', '--all', '-q', WEB],
                            cwd=tree / 'compose').decode().strip()
    r.string(container, r'[a-f0-9]{12,64}')
    result = host.output(['docker', 'inspect', '--format', '{{json .HostConfig.PortBindings}}', container])
    r.need(not json.loads(result), 'private RAGFlow container has a published port')
    run_compose(tree, config, ['start', WEB], overlay)
    r.need(backup.ready([WEB]), 'private RAGFlow did not become healthy')
    run_compose(tree, config, ['exec', '-T', WEB, 'curl', '-fsS', '-o', '/dev/null',
                              '--max-time', '10', 'http://127.0.0.1/api/v1/system/healthz'], overlay, timeout=15)


def verify_start(tree, operation, config, side):
    state = status(operation)
    opening = 'opening-' + side
    if state != opening:
        r.need(state in ('planned', 'closing', 'quiesced', 'backed-up'),
               'maintenance direction changed after opening')
        run_compose(tree, config, ['stop', WEB])
        up(tree, config, backup.DATA, keep=True)
        up(tree, config, ['vllm-embed', 'vllm-rerank'], keep=True)
        run_compose(tree, config, ['stop', SERVICE])
        up(tree, config, [SERVICE])
        probe_model(tree, config)
        overlay = operation / OVERLAY
        txn.replace(overlay, PRIVATE_WEB, 0o600)
        verify_private_web(tree, config, overlay)
        save(operation, opening)
    up(tree, config, [WEB])


def cancel_prepublication(tree, operation, saved, config):
    state = status(operation)
    if state not in ('cancelling', 'reopening-source'):
        state = 'cancelling' if state == 'planned' else 'reopening-source'
        save(operation, state)
    if state == 'reopening-source':
        up(tree, config, backup.DATA, keep=True)
        up(tree, config, ['vllm-embed', 'vllm-rerank', SERVICE], keep=True)
        up(tree, config, [WEB], keep=True)
    txn.mark(operation, saved, 'reverted')


def finish(operation, config):
    if status(operation) != 'finished':
        for timer in config['timers']:
            systemctl('start', timer)
        save(operation, 'finished')

"""Plan a release on this host. No installation, pulls, imports or service changes."""
import hashlib
import os
from pathlib import Path

from . import backup, paths, release as r, release_inventory as inventory, release_verify as v, ui
from . import update_host as host, update_source as source


def code_artifact(release):
    return next(a for a in release['artifacts'] if a['kind'] == 'code')


def installed(root, old, key, revoked):
    signed = root / 'installed-release.json'
    if os.path.lexists(signed):
        raw = v.bounded_read(signed, r.MAX_MANIFEST, trusted=True)
        sig = v.bounded_read(root / 'installed-release.json.sig', v.MAX_SIGNATURE, trusted=True)
        v.verify_signature(raw, sig, key, 'ragspark-release-v1', revoked)
        release = r.manifest(raw)
        current = {'version': release['version'], 'manifest_sha256': hashlib.sha256(raw).hexdigest()}
        files = code_artifact(release)['files']
        sequence = release['sequence']
    else:
        record = root / 'trust' / 'baseline.json'
        if not os.path.lexists(record):
            raise source.PreparationError('installed release baseline is not configured')
        base = r.parse(v.bounded_read(record, r.MAX_MANIFEST, trusted=True))
        r.fields(base, 'source files')
        current, files = base['source'], base['files']
        r.need('legacy_commit' in current, 'invalid legacy baseline')
        sequence = 0
    report = inventory.compare(old, files, current)
    if not report['matches']:
        raise r.InvalidRelease('installed inventory differs: ' + ', '.join(list(report['differences'])[:8]))
    return current, files, sequence


def disk_plan(stage, old, models_dir, code, files, missing, transition):
    space = host.Space()
    space.add(stage, code['expanded_bytes'] + sum(a['bytes'] for a in missing))
    space.add(old, code['expanded_bytes'] + sum(rec['bytes'] for rec in files))
    images = sum(a['expanded_bytes'] for a in missing if a['kind'] == 'image')
    if images:
        store = Path(os.fsdecode(host.output(['docker', 'info', '--format', '{{.DockerRootDir}}'])).strip())
        r.need(store.is_absolute() and store.is_dir(), 'unknown Docker storage path')
        space.add(store, images)
    space.add(models_dir, sum(a['expanded_bytes'] for a in missing if a['kind'] == 'model'))
    if transition['backup'] == 'required':
        size = 1 << 30  # same margin as the backup command
        for volume in backup.VOLUMES:
            mount = backup.mountpoint(volume)
            if not mount or not os.path.isdir(mount):
                raise source.PreparationError('cannot estimate the data backup')
            size += backup.dir_bytes(mount)
        space.add(paths.host(paths.BACKUP_DIR), size)
    return list(space.filesystems.values())


def check(root, old, bundle, config):
    llm = r.string(config.get('llm'), r'[a-z0-9][a-z0-9.-]{0,79}')
    mode = 'stack' if (old / 'compose/node/llm' / (llm + '.yaml')).is_file() else 'single'
    models_dir = Path(config.get('models_dir', paths.DEFAULT_MODELS_DIR))
    r.need(models_dir.is_absolute(), 'invalid model cache path')
    models_dir = Path(paths.host(str(models_dir)))
    with source.workspace(root) as stage:
        release, identity, key, revoked = source.discover(root, bundle)
        current, files, sequence = installed(root, old, key, revoked)
        profiles = [p for p in release['profiles'] if p['mode'] == mode and p['llm'] == llm]
        r.need(len(profiles) == 1, 'no unique profile matching the installed model and mode')
        profile = profiles[0]
        available = host.hardware(profile['roles']['head']['requirements'])
        plan = {'from': current.get('version') or current['legacy_commit'][:7], 'to': release['version'],
                'mode': mode, 'llm': llm, 'available_ram': available}
        if current.get('version') == release['version']:
            r.need(current['manifest_sha256'] == identity['manifest_sha256'], 'installed release version reused')
            return dict(plan, same=True)
        transition = r.select_transition(release, current, profile['id'], mode, llm, release['target'], sequence)
        r.need(release['sequence'] > sequence, 'release sequence did not advance')
        code = code_artifact(release)
        artifacts = {a['id']: a for a in release['artifacts']}
        role = profile['roles']['head']
        required = sorted(set(role['images'].values()) | set(role['models'].values()))
        missing, reused = [code], []
        for ident in required:
            a = artifacts[ident]
            present = (host.installed_image(a) if a['kind'] == 'image'
                       else host.installed_model(models_dir, a))
            if present:
                reused.append(ident)
            else:
                missing.append(a)
        space = disk_plan(stage, old, models_dir, code, files, missing, transition)
        archive = source.stage_artifact(stage, bundle, code)
        candidate = stage / 'candidate'
        host.materialize(archive, candidate, code)
        before = {f['path']: f for f in files}
        after = {f['path']: f for f in code['files']}
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        host.impact(old, candidate, llm, mode, 'head', transition, profile, artifacts, changed)
        for a in missing[1:]:
            source.stage_artifact(stage, bundle, a)
        return dict(plan, same=False, transition=transition, missing=[a['id'] for a in missing],
                    reused=reused, space=space, size=sum(a['bytes'] for a in missing),
                    online=bundle is None)


def show(plan):
    ui.say(ui.t('up_version', **plan))
    if plan['mode'] == 'stack':
        ui.say(ui.t('up_stack'))
    if plan['same']:
        ui.say(ui.t('up_current'))
        return
    t = plan['transition']
    ui.say(ui.t('up_source_net' if plan['online'] else 'up_source_local',
                size=backup.human(plan['size']), ids=', '.join(plan['missing'])))
    if plan['reused']:
        ui.say(ui.t('up_reuse', ids=', '.join(plan['reused'])))
    for mount, needed, free in plan['space']:
        ui.say(ui.t('up_space', mount=mount, needed=backup.human(needed), free=backup.human(free)))
    ui.say(ui.t('up_ram', size=backup.human(plan['available_ram'])))
    for role, services in t['restart'].items():
        ui.say(ui.t('up_restart', role=role, services=', '.join(services) or '—'))
    if t['impact'] == 'systemd':
        ui.say(ui.t('up_units'))
    ui.say(ui.t('up_backup_yes' if t['backup'] == 'required' else 'up_backup_no'))
    ui.say(ui.t('up_pause_unknown' if t['impact'] != 'code' or t['backup'] == 'required' else 'up_pause_zero'))


def update(args):
    root = Path(paths.host('/var/lib/ragspark-update'))
    old = Path(paths.install_dir())
    try:
        state = r.parse(v.bounded_read(old / 'manifest.json', 4 << 20))
        r.need(isinstance(state, dict) and isinstance(state.get('config'), dict), 'invalid installation manifest')
        r.need(state.get('schema') == 1 and state.get('status') == 'installed', 'installation is not ready')
        config = state['config']
        if not args.lang and config.get('lang') in ('ru', 'en'):
            ui.set_lang(config['lang'])
        if r.choice(config.get('role', 'head'), ('head', 'node')) == 'node':
            ui.say(ui.t('up_node'))
            return 3
        bundle = Path(args.bundle).absolute() if args.bundle is not None else None
        show(check(root, old, bundle, config))
        return 0
    except source.Busy:
        ui.status('FAIL', ui.t('up_busy'))
        return 4
    except source.PreparationError as exc:
        ui.status('FAIL', ui.t('up_prepare', detail=exc))
        return 5
    except (r.InvalidRelease, ValueError, SyntaxError, KeyError, TypeError) as exc:
        detail = str(exc) if isinstance(exc, r.InvalidRelease) else ui.t('up_format')
        ui.status('FAIL', ui.t('up_reject', detail=detail))
        return 3
    except OSError as exc:
        ui.status('FAIL', ui.t('up_prepare', detail=exc.strerror))
        return 5

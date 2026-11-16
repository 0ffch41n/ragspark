"""Resolve only the requested catalog profile, from the committed Compose files."""
import json
import os
import subprocess

import yaml

from rsinst import release as r


def image_ref(ref):
    r.string(ref)
    first = ref.split('@')[0].split('/')[0]
    if '/' not in ref.split('@')[0]:
        return 'docker.io/library/' + ref
    if '.' not in first and ':' not in first and first != 'localhost':
        return 'docker.io/' + ref
    return ref


def render(directory, llm, mode, role, command):
    cwd = directory / 'compose'
    if role == 'node':
        cwd = cwd / 'node'
    files = ['compose.yaml']
    if mode == 'stack' and role == 'head':
        files.append('stack.yaml')
    files.append('llm/' + llm + '.yaml')
    # Only the freshly materialized snapshot supplies interpolation and env_file.
    env_file = cwd / '.env'
    env_file.write_bytes((directory / 'compose/env.example').read_bytes())
    args = [*command, '--env-file', str(env_file)]
    for filename in files:
        args.extend(['-f', filename])
    try:
        raw = subprocess.check_output([*args, 'config', '--format', 'json'], cwd=cwd,
                                      env={'PATH': os.defpath + ':/usr/local/bin', 'HOME': str(directory)},
                                      timeout=60)
    finally:
        env_file.unlink()
    return json.loads(raw)['services']


def resolve(directory, llm, mode, command=('docker', 'compose')):
    r.string(llm, r'[a-z0-9][a-z0-9.-]{0,79}')
    r.choice(mode, ('single', 'stack'))
    catalog = yaml.safe_load((directory / 'catalog/models.yaml').read_text())
    models = catalog['models']
    rows = [m for m in models['llm'] if m['id'] == llm and mode in m.get('modes', [])
            and m.get('compose') == 'compose/llm/' + llm + '.yaml']
    r.need(len(rows) == 1, 'no matching runnable catalog entry')
    result = {}
    for role in (['head'] if mode == 'single' else ['head', 'node']):
        services = render(directory, llm, mode, role, command)
        images = {name: image_ref(svc['image']) for name, svc in services.items()}
        weights = {}
        required = {'vllm-llm' if mode == 'single' else 'vllm-llm-rank': 'llm'}
        if mode == 'single' or role == 'node':
            required.update({'vllm-embed': 'embedding', 'vllm-rerank': 'rerank'})
        for service, logical in required.items():
            svc = services[service]
            args = (svc.get('entrypoint') or []) + (svc.get('command') or [])
            r.need(args.count('serve') == 1 and args.count('--revision') == 1, 'expected one pinned model command')
            pos = args.index('serve')
            rev = args.index('--revision')
            r.need(0 < pos < len(args)-1 and args[pos-1] == 'vllm' and rev < len(args)-1, 'invalid model command')
            repo = args[pos+1]
            revision = r.commit(args[rev+1])
            choices = rows if logical == 'llm' else models[logical]
            r.need(any(m['hf_repo'] == repo for m in choices), 'model repository differs from catalog')
            weights[logical] = {'repo': repo, 'revision': revision}
        result[role] = {'images': images, 'models': weights}
    if mode == 'stack':
        r.need(result['head']['models']['llm'] == result['node']['models']['llm'], 'stack LLM revisions differ')
    return result

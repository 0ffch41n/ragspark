"""`ragspark watch`: keeps the two halves of a stack-mode LLM together (D10, stage 5).

A systemd timer runs it every 30 seconds on both units; in single mode it does nothing. vLLM does
not recover a broken pair by itself (docs/VALIDATION.md, section 17):

- the head's half exits, with code 0, about a minute after it lost the node's half
  (VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS); Docker's restart policy `always` starts it again and it
  waits for a partner;
- the node's half never notices that the head's half went away: it waits for ever.

So on the node, watch restarts the local half once the head's API, after it had been seen healthy,
stays unreachable for a minute - unless the local half restarted since then anyway. After any
start of the local half it waits until the head has been healthy again before it acts, so a pair
that is still forming (at boot, both start together) is never interrupted.

On the head, watch sends a one-token request whenever the API reports healthy: /health stays 200
while the engine behind it hangs. Two failures in a row restart the head's half.
"""
import calendar
import json
import os
import re
import time

from . import catalog, paths, stackmode, ui
from .manifest import Manifest
from .sysutil import read_env, run

SERVICE = stackmode.RANK
PING_FAILS = 2           # head: failed one-token requests in a row before a restart
GRACE_S = 60             # node: how long the head must stay unreachable
PING_TIMEOUT_S = 90      # a request may wait behind a long prefill


# ------------------------------------------------------------------ decisions (no I/O)

def decide_head(state, rank, ping):
    """rank: (status, started_epoch, health) of the head's half or None; ping: callable -> bool.
    Returns (state, action) with action None or "restart"."""
    if rank is None or rank[0] != "running" or rank[2] != "healthy":
        state["fails"] = 0                 # missing, starting or waiting for its partner: nothing to fix here
        return state, None
    if ping():
        state["fails"] = 0
        return state, None
    state["fails"] = state.get("fails", 0) + 1
    if state["fails"] >= PING_FAILS:
        state["fails"] = 0
        return state, "restart"
    return state, None


def decide_node(state, rank, head_ok, now):
    """rank: (status, started_epoch, health) of the node's half or None; head_ok: the head's API
    answered /health. Returns (state, action) with action None or "restart"."""
    if rank is None or rank[0] != "running":
        return state, None                 # stopped or restarting: Docker brings it back
    if state.get("rank_started") != rank[1]:
        # The local half (re)started since the last look: wait for the pair to form first.
        state.update(rank_started=rank[1], head_seen_ok=False, head_lost_at=None)
    if head_ok:
        state.update(head_seen_ok=True, head_lost_at=None)
        return state, None
    if not state.get("head_seen_ok"):
        return state, None
    lost = state.get("head_lost_at") or now
    state["head_lost_at"] = lost
    if now - lost >= GRACE_S:
        state.update(head_seen_ok=False, head_lost_at=None)
        return state, "restart"
    return state, None


# ------------------------------------------------------------------ probes

def started_epoch(text):
    """Docker's StartedAt (RFC 3339, nanoseconds, UTC) -> seconds since the epoch, or 0."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", text or "")
    if not m:
        return 0
    return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))


def rank_state(project):
    """(container name, (status, started_epoch, health)) of the LLM half of `project`, or (None, None)."""
    code, out = run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + project,
                     "--filter", "label=com.docker.compose.service=" + SERVICE, "--format", "{{.Names}}"])
    names = out.split() if code == 0 else []
    if not names:
        return None, None
    code, out = run(["docker", "inspect", "-f",
                     "{{.State.Status}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                     names[0]])
    parts = out.strip().split("|") if code == 0 else []
    if len(parts) != 3:
        return names[0], None
    return names[0], (parts[0], started_epoch(parts[1]), parts[2])


# ------------------------------------------------------------------ the command

def _load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def watch(args):
    manifest = Manifest.load()
    if manifest is None or manifest.status != "installed":
        return 0                                   # nothing installed (yet): nothing to watch
    config = manifest.config
    if not stackmode.is_stack(config.get("llm")):
        return 0                                   # single mode
    entry = catalog.entry(catalog.load(), "llm", config.get("llm")) or {}
    role = paths.role()
    head_ip, _ = stackmode.addresses(config)
    try:
        head_ip = read_env(os.path.join(paths.compose_dir(), ".env")).get("RAGSPARK_HEAD_IP") or head_ip
    except OSError:
        pass
    state_path = os.path.join(paths.install_dir(), "watch.json")
    state = _load_state(state_path)

    name, rank = rank_state(paths.project())
    if role == "node":
        head_ok = stackmode.http_ok("http://%s:8000/health" % head_ip)
        state, action = decide_node(state, rank, head_ok, time.time())
        reason = ui.t("watch_head_lost", s=GRACE_S)
    else:
        url = "http://%s:8000/v1/chat/completions" % head_ip
        body = {"model": entry.get("served_name") or config.get("llm"), "max_tokens": 1, "temperature": 0,
                "messages": [{"role": "user", "content": "ping"}]}
        state, action = decide_head(state, rank, lambda: stackmode.http_ok(url, body, timeout=PING_TIMEOUT_S))
        reason = ui.t("watch_no_answer", n=PING_FAILS)
    if action == "restart" and name:
        code, out = run(["docker", "restart", "-t", "30", name])
        ui.status("DO" if code == 0 else "FAIL", ui.t("watch_restarted", name=name, reason=reason)
                  + ("" if code == 0 else ": " + out.strip()[-200:]))
        if code == 0 and role == "node":
            _, fresh = rank_state(paths.project())
            if fresh:
                state["rank_started"] = fresh[1]
    _save_state(state_path, state)
    return 0

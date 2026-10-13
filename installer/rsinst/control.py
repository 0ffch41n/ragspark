"""status, start, stop, restart, logs, acceptance, check."""
import json
import os
import time
import urllib.request

from . import paths, ui
from .install import compose_config, step_acceptance, vllm_image
from .manifest import Manifest
from .preflight import gpu_reload_test, run_checks
from .sysutil import compose, compose_json, host_address, project_containers, run


def _require_installed():
    m = Manifest.load()
    if m is None:
        ui.status("FAIL", ui.t("not_installed", dir=paths.INSTALL_DIR))
    return m


def _health_url(config):
    bind = config.get("http_bind", "0.0.0.0")
    host = "127.0.0.1" if bind in ("0.0.0.0", "", "::") else bind
    return "http://%s:%s/api/v1/system/healthz" % (host, config.get("http_port", 80))


def services():
    """[(service, state, health)] for the installed project."""
    rows = compose_json(["ps", "--all"]) or []
    out = []
    for r in rows:
        state = r.get("State", "?")
        # Docker keeps a stale health value on a stopped container; show it only while running.
        health = (r.get("Health", "") or "") if state == "running" else ""
        out.append((r.get("Service", "?"), state, health))
    return sorted(out)


def status(args):
    m = _require_installed()
    if m is None:
        others = project_containers()
        if others:
            ui.say(ui.t("status_foreign", dir=others[0]["dir"] or "?"))
        return 1
    c = m.config
    port = int(c.get("http_port", 80))
    ui.header(ui.t("status_title"))
    ui.say(ui.t("status_version", version=m.data.get("version", "?"), date=m.data.get("finished", m.data.get("started", "?"))))
    ui.say(ui.t("status_llm", llm=c.get("llm", "?")))
    ui.say(ui.t("status_url", url="http://%s%s/" % (host_address(), "" if port == 80 else ":%d" % port),
                email=c.get("admin_email", "?")))
    active, substate = unit_state()
    code2, out2 = run(["systemctl", "is-enabled", paths.UNIT_NAME])
    ui.say(ui.t("status_service", active=active, enabled=out2.strip() or "?"))
    if active == "activating" or (active, substate) == ("active", "running"):
        ui.status("INFO", ui.t("start_in_progress"))
    if m.status != "installed":
        ui.status("WARN", ui.t("status_incomplete"))
    rows = services()
    if not rows:
        ui.status("INFO", ui.t("status_no_containers"))
    for svc, state, health in rows:
        good = state == "running" and health in ("healthy", "")
        ui.status("OK" if good else "WARN", "%-12s %s%s" % (svc, state, " (%s)" % health if health else ""))
    try:
        with urllib.request.urlopen(_health_url(c), timeout=5) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 - any failure means "not answering"
        data = {"status": "nok", "error": str(e)[:80]}
    ok = data.get("status") == "ok"
    ui.status("OK" if ok else "WARN", ui.t("status_health", detail=json.dumps(data, ensure_ascii=False)))
    return 0 if ok else 1


def unit_state():
    """(ActiveState, SubState) of the unit: ("active", "running") while its
    `compose up` still runs, ("active", "exited") once it has finished."""
    code, out = run(["systemctl", "show", "-p", "ActiveState", "-p", "SubState", paths.UNIT_NAME])
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return values.get("ActiveState", "?"), values.get("SubState", "?")


def wait_healthy(timeout=2400):
    """Follow the unit's start until every service is healthy; print each change.

    Containers of services that wait for their turn stay "exited" until
    compose starts them, so a stopped container means a failure only once the
    unit's start command has finished.
    """
    code, out = compose(["config", "--services"])
    expected = set(out.split()) if code == 0 else set()
    t0, seen = time.time(), {}
    while time.time() - t0 < timeout:
        rows = services()
        for svc, state, health in rows:
            key = state + "/" + health
            if seen.get(svc) != key:
                seen[svc] = key
                ui.status("INFO", "%-12s %s%s" % (svc, state, " (%s)" % health if health else ""))
        present = {svc for svc, _, _ in rows}
        if rows and expected <= present and all(s == "running" and h in ("healthy", "") for _, s, h in rows):
            ui.status("OK", ui.t("all_healthy", min=(time.time() - t0) / 60))
            return True
        active, substate = unit_state()
        if active in ("failed", "inactive"):
            ui.status("FAIL", ui.t("unit_failed", unit=paths.UNIT_NAME))
            return False
        if (active, substate) == ("active", "exited"):
            for svc, state, _ in rows:
                if state in ("exited", "dead"):
                    ui.status("FAIL", ui.t("svc_exited", svc=svc))
                    return False
        time.sleep(10)
    ui.status("FAIL", ui.t("wait_timeout"))
    return False


def _all_healthy():
    code, out = compose(["config", "--services"])
    expected = set(out.split()) if code == 0 else set()
    rows = services()
    return bool(rows) and expected <= {s for s, _, _ in rows} and \
        all(st == "running" and h in ("healthy", "") for _, st, h in rows)


def stop_unready():
    """Stop project containers that run (or restart) without being healthy.

    At boot Docker restarts, all at once, the containers whose last exit code
    was not 0 (restart policy on-failure; a stop that ends in SIGKILL, Java's
    143, or a power loss, after which every exit code is 255). Started together,
    the vLLM services leave each other without memory for their cache. Healthy
    containers are left alone, so a running stack is not interrupted.
    """
    code, out = run(["docker", "ps", "--filter", "label=com.docker.compose.project=" + paths.PROJECT,
                     "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if code != 0:
        ui.status("WARN", out.strip()[-300:])
        return
    names = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1] in ("running", "restarting") and "(healthy)" not in parts[2]:
            names.append(parts[0])
    if names:
        ui.status("DO", ui.t("prestart_stopping", names=", ".join(sorted(names))))
        code, out = run(["docker", "stop"] + sorted(names))
        if code != 0:
            ui.status("WARN", out.strip()[-300:])


def prestart(args):
    """ExecStartPre of the systemd unit."""
    stop_unready()
    return 0


def start(args):
    if _require_installed() is None:
        return 1
    if _all_healthy():
        ui.status("OK", ui.t("already_running"))
        return 0
    active, substate = unit_state()
    if active == "activating" or (active, substate) == ("active", "running"):
        # The unit is starting the stack (at boot, for example): only follow it.
        ui.status("INFO", ui.t("start_in_progress"))
        return 0 if wait_healthy() else 1
    ui.say(ui.t("start_hint"))
    if active == "active":
        # The unit stays active after its start command finished (RemainAfterExit).
        # Start what is missing directly, without stopping the services that run.
        stop_unready()
        t0 = time.time()
        code, _ = compose(["up", "-d", "--wait", "--wait-timeout", "2400"], capture=False)
        if code != 0:
            ui.status("FAIL", ui.t("start_failed"))
            return 1
        ui.status("OK", ui.t("all_healthy", min=(time.time() - t0) / 60))
        return 0
    run(["systemctl", "reset-failed", paths.UNIT_NAME])
    code, out = run(["systemctl", "start", paths.UNIT_NAME])
    if code != 0:
        ui.status("FAIL", out.strip()[-300:])
        return 1
    return 0 if wait_healthy() else 1


def stop(args):
    if _require_installed() is None:
        return 1
    run(["systemctl", "stop", paths.UNIT_NAME])
    # The unit may be inactive or failed while containers still run (e.g. a start
    # that timed out at boot): stop them directly as well.
    if any(c["state"] == "running" for c in project_containers()):
        code, out = compose(["stop"])
        if code != 0:
            ui.status("FAIL", out.strip()[-300:])
            return 1
    ui.status("OK", ui.t("stopped"))
    return 0


def restart(args):
    return stop(args) or start(args)


def logs(args):
    if _require_installed() is None:
        return 1
    code, _ = compose(["logs", "-f", "--tail", str(args.tail)] + ([args.service] if args.service else []),
                      capture=False)
    return code


def acceptance(args):
    m = _require_installed()
    if m is None:
        return 1
    ui.header(ui.t("st_acceptance"))
    return 0 if step_acceptance(m.config.get("lang", ui.lang())) else 1


def check(args):
    m = Manifest.load()
    config = dict(m.config) if m else {}
    if args.http_port:
        config["http_port"] = args.http_port
    if args.models_dir:
        config["models_dir"] = args.models_dir
    report = run_checks(config, m, network=not args.no_network)
    result = 1 if report.failed else 0
    if args.gpu_reload:
        image = None
        if m is not None:
            image = vllm_image(compose_config() or {})
        if not image:
            image = _source_vllm_image()
        if not image or run(["docker", "image", "inspect", image])[0] != 0:
            ui.status("FAIL", ui.t("gr_no_image", image=image or "?"))
            return 1
        if not gpu_reload_test(image):
            result = 1
    return result


def _source_vllm_image():
    """The vLLM image named in the LLM compose files of this copy (before installation)."""
    import glob
    import re
    for f in sorted(glob.glob(os.path.join(paths.SOURCE_ROOT, "compose", "llm", "*.yaml"))):
        with open(f) as fh:
            m = re.search(r"^\s*image:\s*(\S*vllm\S*)", fh.read(), re.M)
            if m:
                return m.group(1)
    return None

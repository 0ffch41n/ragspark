"""Optional health checks for an installed Spark, with journal notifications."""
import json
import os
from pathlib import Path
import time

from . import backup, backup_remote, control, paths, release_verify, stackmode, ui
from .manifest import Manifest, make_dirs
from .preflight import _free_gib
from .sysutil import run
from .update_transaction import replace, secure_directory


TIMER = "ragspark-monitor.timer"
SERVICE = "ragspark-monitor.service"
STATE = "/run/ragspark-monitor/status.json"
MIN_DISK_GIB = 25
BACKUP_MAX_HOURS = 48
STALE_AFTER = 360
CHECKS = ("mon_web", "mon_models", "mon_peer", "mon_disk", "mon_backup", "mon_replica")
SERVICE_UNIT = f"""[Unit]
Description=RAGSpark health monitor
After=docker.service

[Service]
Type=oneshot
ExecStart={paths.CLI_PATH} monitor --scheduled
RuntimeDirectory=ragspark-monitor
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
TimeoutStartSec=50s
SuccessExitStatus=4 7
"""
TIMER_UNIT = """[Unit]
Description=RAGSpark periodic health check

[Timer]
OnActiveSec=30s
OnUnitActiveSec=2min
AccuracySec=5s

[Install]
WantedBy=timers.target
"""


def _state_path():
    return Path(paths.host(STATE))


def read_state():
    try:
        state = json.loads(release_verify.bounded_read(_state_path(), 2048, trusted=True))
        if (type(state["checked"]) is not int
                or any(type(state[key]) is not list or any(item not in CHECKS for item in state[key])
                       for key in ("failed", "active"))):
            return None
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _disk_ok(config, backups):
    code, docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=5)
    if code != 0 or not os.path.isabs(docker_root.strip()):
        return False
    locations = [paths.INSTALL_DIR, docker_root.strip(), config.get("models_dir", paths.DEFAULT_MODELS_DIR)]
    if backups:
        locations.append(paths.BACKUP_DIR)
    for location in locations:
        free, _ = _free_gib(paths.host(location))
        if free is None or free < MIN_DISK_GIB:
            return False
    return True


def _replica_ok():
    status_path = Path(paths.host(backup_remote.BASE + "/sender-status.json"))
    try:
        result = json.loads(release_verify.bounded_read(status_path, 4096))
        age = time.time() - result["time"]
        return result["ok"] is True and 0 <= age < BACKUP_MAX_HOURS * 3600
    except (OSError, ValueError, KeyError, TypeError):
        return False


def failures(manifest):
    config = manifest.config
    node = config.get("role") == "node"
    stacked = stackmode.is_stack(config.get("llm"))
    failed = set()
    if not node and control.ragflow_health(config).get("status") != "ok":
        failed.add("mon_web")
    models = {name: health for name, state, health in control.services(timeout=5) if state == "running"}
    expected = ["vllm-embed", "vllm-rerank"]
    if not node:
        expected.append("vllm-llm")
    if any(models.get(name) != "healthy" for name in expected):
        failed.add("mon_models")
    if stacked:
        if models.get(stackmode.RANK) != ("" if node else "healthy"):
            failed.add("mon_models")
        head, peer = stackmode.addresses(config)
        local, remote = (peer, head) if node else (head, peer)
        code, _ = run(["ping", "-n", "-c", "1", "-W", "2", "-I", local, remote], timeout=4)
        if code != 0:
            failed.add("mon_peer")
        if node and not stackmode.http_ok(f"http://{head}:8000/health", timeout=4):
            failed.add("mon_models")
    receiver = paths.host(backup_remote.BASE + "/receiver.json")
    backups = os.path.isfile(receiver) if node else bool(manifest.data.get("backup_timer"))
    replica = not node and os.path.isfile(backup_remote.config_path())
    if not _disk_ok(config, backups or replica):
        failed.add("mon_disk")
    if backups:
        latest = backup.last_copy()
        if latest is None or not 0 <= latest[1] < BACKUP_MAX_HOURS:
            failed.add("mon_backup")
    if replica and not _replica_ok():
        failed.add("mon_replica")
    return failed


def _summary(checks):
    return ", ".join(ui.t(key) for key in CHECKS if key in checks)


def record(failed):
    previous = read_state() or {"failed": [], "active": []}
    active = set(previous["active"])
    confirmed = failed & (active | set(previous["failed"]))
    state = {"checked": int(time.time()), "failed": sorted(failed), "active": sorted(confirmed)}
    path = _state_path()
    secure_directory(path.parent)
    replace(path, (json.dumps(state) + "\n").encode(), 0o600)
    if confirmed - active:
        ui.status("WARN", ui.t("mon_failed", checks=_summary(confirmed - active)))
    if active - confirmed:
        ui.status("OK", ui.t("mon_recovered", checks=_summary(active - confirmed)))


def show_status(manifest):
    if manifest.data.get("monitor_timer") != TIMER:
        return
    code, _ = run(["systemctl", "is-active", "--quiet", TIMER], timeout=5)
    if code != 0:
        ui.say(ui.t("mon_off"))
        return
    state = read_state()
    if state is None or not 0 <= time.time() - state["checked"] <= STALE_AFTER:
        ui.status("INFO", ui.t("mon_waiting"))
    elif state["active"]:
        ui.status("WARN", ui.t("mon_failed", checks=_summary(state["active"])))
    elif state["failed"]:
        ui.status("INFO", ui.t("mon_pending", checks=_summary(state["failed"])))
    else:
        ui.status("OK", ui.t("mon_ok"))


def stop(manifest):
    if manifest.data.get("monitor_timer") != TIMER:
        return True
    for command in (["disable", "--no-reload", "--now", TIMER], ["stop", SERVICE]):
        code, _ = run(["systemctl", *command])
        if code != 0:
            ui.status("FAIL", ui.t("mon_schedule_failed"))
            return False
    _state_path().unlink(missing_ok=True)
    return True


def schedule(manifest):
    units = [(paths.host("/etc/systemd/system/" + name), text)
             for name, text in ((SERVICE, SERVICE_UNIT), (TIMER, TIMER_UNIT))]
    for path, text in units:
        if os.path.lexists(path) and (
                path not in manifest.data["files"]
                or release_verify.bounded_read(Path(path), 4096, trusted=True) != text.encode()):
            ui.status("FAIL", ui.t("mon_unit_conflict", path=path))
            return 1
    for name, text in units:
        path = Path(name)
        if not path.exists():
            make_dirs(str(path.parent), manifest)
            manifest.add_file(name)
            replace(path, text.encode(), 0o644)
    manifest.set("monitor_timer", TIMER)
    # Avoid reloading the system manager while GPU containers are running.
    code, _ = run(["systemctl", "enable", "--no-reload", "--now", TIMER])
    if code != 0:
        ui.status("FAIL", ui.t("mon_schedule_failed"))
        return 1
    ui.status("OK", ui.t("mon_on"))
    return 0


def command(args):
    manifest = Manifest.load()
    if manifest is None or manifest.status != "installed":
        ui.status("FAIL", ui.t("not_installed", dir=paths.INSTALL_DIR))
        return 1
    try:
        if args.schedule == "on":
            return schedule(manifest)
        if args.schedule == "off":
            if not stop(manifest):
                return 1
            ui.say(ui.t("mon_off"))
            return 0
        failed = failures(manifest)
        if args.scheduled:
            record(failed)
            return 0
        if failed:
            ui.status("WARN", ui.t("mon_failed", checks=_summary(failed)))
            return 1
        ui.status("OK", ui.t("mon_ok"))
        return 0
    except (OSError, ValueError) as exc:
        ui.status("FAIL", ui.t("mon_error", detail=str(exc)))
        return 1

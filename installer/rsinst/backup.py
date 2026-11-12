"""`ragspark backup` and `ragspark restore`: cold copies of the head's data (stage 6, D21).

RAGFlow keeps its data in four Docker volumes - MySQL (knowledge bases, documents, chats, users,
settings), MinIO (the uploaded files), Elasticsearch (chunks and vectors) and Valkey (task queues) -
and writes the first three together. A copy is taken with them stopped, the way RAGFlow's own
docker/migration.sh does it, so that all of them show the same moment. The models keep running; the
web interface and the chat are away for a minute or two.

A copy is a directory BACKUP_DIR/ragspark-YYYYmmdd-HHMMSS: one compressed tar per volume, the
settings the data belongs to (`env`: the databases accept only the passwords they were created with),
the install manifest and backup.json - what is inside, the sha256 of every file, the release, the LLM
and the RAGFlow image. Model weights and images are not copied: they can be downloaded again.

A restore stops the same services, saves the current state as a copy of its own
(ragspark-...-before-restore, never pruned), unpacks the chosen copy into the volumes, writes the
copy's passwords and keys into .env and starts them again; if unpacking fails, the saved state is put
back.
"""
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import socket
import time

from . import paths, ui
from .manifest import Manifest
from .sysutil import compose, compose_json, read_env, run, set_env

VOLUMES = ("mysql", "minio", "esdata", "valkey")            # compose volume names, project prefix added
WEB = "ragflow"                                             # stopped first, so nothing writes any more
DATA = ("redis", "mysql", "minio", "es01")                  # started first, RAGFlow needs them ready
# The variables of .env the data belongs to: the passwords the databases were created with, the key
# RAGFlow signs and encrypts with, and the superuser the tools log in as.
DATA_ENV = ("DEFAULT_SUPERUSER_EMAIL", "ADMIN_DEFAULT_PASSWORD", "RAGFLOW_SECRET_KEY", "ELASTIC_PASSWORD",
            "MYSQL_PASSWORD", "MYSQL_DBNAME", "MINIO_USER", "MINIO_PASSWORD", "REDIS_PASSWORD")
KEEP_DAILY, KEEP_WEEKLY = 7, 4
WAIT_PARSE_MIN = 60                                         # a copy waits for documents being parsed
POLL_S = 30
START_TIMEOUT_S = 900
SAFETY = "-before-restore"
NAME_RE = re.compile(r"^ragspark-(\d{8}-\d{6})(%s)?$" % re.escape(SAFETY))
PARSING_SQL = "SELECT COUNT(*) FROM document WHERE run='1'"

BACKUP_SERVICE_UNIT = """[Unit]
Description=RAGSpark: copy of the data (knowledge bases, documents, chats, users)
Documentation=https://github.com/0ffch41n/ragspark
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart={python} {install_dir}/installer/ragspark.py backup --scheduled
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=3h
TimeoutStopSec=20min
KillMode=mixed
"""

# Not Persistent: a copy missed while the unit was off must not stop RAGFlow while the stack starts at boot.
BACKUP_TIMER_UNIT = """[Unit]
Description=RAGSpark: copy the data every night
Documentation=https://github.com/0ffch41n/ragspark

[Timer]
OnCalendar=*-*-* 03:30:00

[Install]
WantedBy=timers.target
"""


# ------------------------------------------------------------------ copies on disk (no Docker)

def root_dir(args=None):
    d = getattr(args, "dir", None)
    return os.path.abspath(d) if d else paths.host(paths.BACKUP_DIR)


def stamp(name):
    m = NAME_RE.match(name)
    try:
        return datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S") if m else None
    except ValueError:
        return None


def is_safety(name):
    m = NAME_RE.match(name)
    return bool(m and m.group(2))


def copies(root):
    """Complete copies in root, newest first: [(name, path, info)]."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    rows = []
    for name in names:
        path = os.path.join(root, name)
        if stamp(name) is None or not os.path.isfile(os.path.join(path, "backup.json")):
            continue
        try:
            with open(os.path.join(path, "backup.json"), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or info.get("format") != 1:
            continue
        rows.append((name, path, info))
    return sorted(rows, key=lambda r: r[0], reverse=True)


def to_keep(names, daily=KEEP_DAILY, weekly=KEEP_WEEKLY):
    """The newest copy of each of the `daily` latest days and the newest of each of the `weekly` latest calendar weeks.
    Copies saved before a restore are always kept."""
    regular = sorted((n for n in names if stamp(n) and not is_safety(n)), reverse=True)
    days, keep = set(), set()
    for name in regular:
        day = stamp(name).date()
        if day not in days and len(days) < daily:
            days.add(day)
            keep.add(name)
    weeks = []
    for name in regular:
        week = stamp(name).isocalendar()[:2]
        if week in weeks:
            continue
        weeks.append(week)
        if len(weeks) > weekly:
            break
        keep.add(name)
    keep.update(n for n in names if stamp(n) and is_safety(n))
    return keep


def prune(root):
    """Remove the copies to_keep() leaves out, and copies a crash left unfinished. Returns the names removed."""
    rows = copies(root)
    keep = to_keep([r[0] for r in rows])
    gone = []
    for name, path, _ in rows:
        if name not in keep:
            shutil.rmtree(path)
            gone.append(name)
    for name in os.listdir(root):
        if name.startswith(".incomplete-"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    return sorted(gone)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def dir_bytes(path):
    total = 0
    for base, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%d B" % n


def compression():
    """(name, extension, tar options): zstd where installed (DGX OS has it), gzip otherwise."""
    if shutil.which("zstd"):
        return "zstd", ".tar.zst", ["-I", "zstd -T1 -3"]
    return "gzip", ".tar.gz", ["-z"]


def tar_options(filename):
    return ["-I", "zstd"] if filename.endswith(".zst") else ["-z"]


def archive(src, dest, options):
    """One volume directory -> one compressed tar; numeric owners, so ids survive another host."""
    return run(["tar", "--numeric-owner", "--hard-dereference"] + options + ["-cf", dest, "-C", src, "."], log_output=False)


def clear(directory):
    """Empty a volume directory (Docker's _data) without removing it."""
    for name in os.listdir(directory):
        p = os.path.join(directory, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)


def extract(src, directory):
    clear(directory)
    return run(["tar", "--numeric-owner", "-p"] + tar_options(src) + ["-xf", src, "-C", directory],
               log_output=False)


def archive_check(filename):
    """Read the whole compressed stream before stopping services; reject unsafe tar members.

    These data volumes must contain only ordinary files and directories. Links, devices and
    paths outside the volume are not supported by this physical restore.
    """
    cmd = ["zstd", "-dc", filename] if filename.endswith(".zst") else ["gzip", "-dc", filename]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            total = 0
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                for member in tf:
                    parts = member.name.split("/")
                    if (member.name.startswith("/") or ".." in parts or
                            not (member.isfile() or member.isdir())):
                        raise ValueError("unsafe archive member")
                    if member.isfile():
                        total += member.size
            # tar may stop at its end marker before the decompressor verifies its trailer.
            while proc.stdout.read(1 << 20):
                pass
            if proc.wait() != 0:
                raise ValueError("invalid compressed stream")
            return total
        except BaseException:
            proc.kill()
            proc.wait()
            raise


def verify(path, info):
    """Return the invalid file/schema, or None. No service/data changes during validation."""
    try:
        if (info.get("format") != 1 or set(info["volumes"]) != set(VOLUMES) or
                set(info["files"]) != {"env", "manifest"} or
                set(info["images"]) != set((WEB,) + DATA)):
            return "backup.json"
        entries = list(info["volumes"].values()) + list(info["files"].values())
        seen = set()
        for e in entries:
            name = e["file"]
            if not isinstance(name, str) or name in ("", ".", "..") or os.path.basename(name) != name or name in seen:
                return "backup.json"
            seen.add(name)
            f = os.path.join(path, name)
            if os.path.islink(f) or not os.path.isfile(f) or sha256(f) != e["sha256"]:
                return name
        for e in info["volumes"].values():
            if not e["file"].endswith((".tar.zst", ".tar.gz")):
                return e["file"]
            total = archive_check(os.path.join(path, e["file"]))
            if type(e.get("source_bytes")) is not int or e["source_bytes"] != total:
                return e["file"]
        env = read_env(os.path.join(path, info["files"]["env"]["file"]))
        if not all(k in env and env[k] for k in DATA_ENV):
            return "env"
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError):
        return "backup.json / archive"


@contextmanager
def recoverable_signals():
    """Turn a service stop (SIGTERM) into an exception so recovery can run, also for Ctrl-C.
    SIGKILL/power loss cannot be recovered in-process; completed copies stay on disk.
    """
    def interrupted(signum, frame):
        raise InterruptedError("interrupted by signal %d" % signum)
    old = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


# ------------------------------------------------------------------ the running stack

def mountpoint(volume):
    """The host directory of a project volume, or None."""
    name = "%s_%s" % (paths.project(), volume)
    code, out = run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", name])
    path = out.strip() if code == 0 else ""
    return path or None


def states(services):
    rows = compose_json(["ps", "--all"] + list(services))
    if rows is None:
        raise RuntimeError("cannot read container states")
    return {r.get("Service"): (r.get("State", ""), r.get("Health", "") or "") for r in rows}


def ready(services, timeout=START_TIMEOUT_S):
    t0 = time.time()
    while True:
        st = states(services)
        if all(st.get(s, ("", ""))[0] == "running" and st[s][1] in ("healthy", "") for s in services):
            return True
        if time.time() - t0 >= timeout:
            return False
        time.sleep(5)


def parsing():
    """Documents being parsed now, or None when MySQL cannot be asked (the stack is down)."""
    script = 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B "$MYSQL_DATABASE" -e "%s"' % PARSING_SQL
    code, out = compose(["exec", "-T", "mysql", "sh", "-c", script], timeout=30)
    try:
        return int(out.strip().splitlines()[-1]) if code == 0 else None
    except (ValueError, IndexError):
        return None


def wait_parsing(minutes):
    """True once nothing is being parsed; False if documents are still parsed after `minutes`."""
    t0, said = time.time(), False
    while True:
        n = parsing()
        if n is None:
            return False
        if n == 0:
            return True
        if not said:
            ui.status("INFO", ui.t("bk_parsing_wait", n=n, min=minutes))
            said = True
        if time.time() - t0 >= minutes * 60:
            return False
        time.sleep(POLL_S)


def stop(services, check_parsing=False):
    """Stop RAGFlow first, then the databases. None, or the error."""
    for group in ([s for s in services if s == WEB], [s for s in services if s != WEB]):
        if group:
            code, out = compose(["stop"] + group)
            if code != 0:
                return out.strip()[-300:] or "docker compose stop failed"
            if WEB in group and check_parsing and "mysql" in services and parsing() != 0:
                return "parsing started before RAGFlow stopped, or its state cannot be checked"
    # Never archive or replace a volume still mounted by any running/paused container.
    for volume in VOLUMES:
        code, out = run(["docker", "ps", "-q", "--filter", "volume=%s_%s" % (paths.project(), volume)])
        if code != 0 or out.strip():
            return "volume is in use or cannot be checked: " + volume
    return None


def start(services, recreate=False):
    """Start the databases, wait until they are ready, then RAGFlow. recreate: they read .env again."""
    verb = ["up", "-d", "--no-deps"] if recreate else ["start"]
    for group in ([s for s in services if s != WEB], [s for s in services if s == WEB]):
        if not group:
            continue
        code, out = compose(verb + group)
        if code != 0 or not ready(group):
            return False
    return True


def ragflow_image():
    cfg = compose_json(["config"]) or [{}]
    return ((cfg[0].get("services") or {}).get(WEB) or {}).get("image", "")


def images():
    cfg = compose_json(["config"])
    if not cfg:
        raise RuntimeError("cannot read compose image versions")
    return {s: cfg[0]["services"][s]["image"] for s in (WEB,) + DATA}


def _lock(root):
    f = open(os.path.join(paths.install_dir(), "backup.lock"), "w")
    os.chmod(f.name, 0o600)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _prepare(root):
    os.makedirs(root, mode=0o700, exist_ok=True)
    if os.path.islink(root):
        raise ValueError("backup directory must not be a symlink")
    os.chmod(root, 0o700)
    return _lock(root)


def _write(root, name, mounts, info):
    """The copy itself; the services are stopped. Returns its directory; raises on failure."""
    tmp = os.path.join(root, ".incomplete-" + name)
    os.makedirs(tmp, mode=0o700)
    kind, ext, options = compression()
    info.update(compression=kind, volumes={}, files={})
    try:
        for key, src in (("env", os.path.join(paths.compose_dir(), ".env")), ("manifest", paths.manifest_path())):
            dest = os.path.join(tmp, key if key == "env" else "manifest.json")
            shutil.copyfile(src, dest)
            os.chmod(dest, 0o600)
            info["files"][key] = {"file": os.path.basename(dest), "sha256": sha256(dest)}
        for volume in VOLUMES:
            t0, dest = time.time(), os.path.join(tmp, volume + ext)
            # Docker MySQL creates this runtime link to /var/run; it is not database data.
            opts = options + (["--exclude=./mysql.sock"] if volume == "mysql" else [])
            code, out = archive(mounts[volume], dest, opts)
            if code != 0:
                raise RuntimeError("%s: %s" % (volume, out.strip()[-300:]))
            os.chmod(dest, 0o600)
            source_bytes = archive_check(dest)
            info["volumes"][volume] = {"file": os.path.basename(dest), "bytes": os.path.getsize(dest),
                                       "source_bytes": source_bytes, "sha256": sha256(dest)}
            ui.status("OK", ui.t("bk_volume_done", name=volume, size=human(os.path.getsize(dest)), s=time.time() - t0))
        with open(os.path.join(tmp, "backup.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
            f.write("\n")
        final = os.path.join(root, name)
        os.rename(tmp, final)
        return final
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _describe(name, manifest):
    env = read_env(os.path.join(paths.compose_dir(), ".env"))
    try:
        with open(os.path.join(paths.install_dir(), "VERSION")) as f:
            release = f.read().strip()
    except OSError:
        release = manifest.data.get("version", "?")
    return {"format": 1, "images": images(), "name": name, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": socket.gethostname(),
            "release": release, "llm": manifest.config.get("llm"), "compose_file": env.get("COMPOSE_FILE", ""),
            "ragflow_image": ragflow_image()}


def _check(manifest):
    if manifest is None or manifest.status != "installed":
        ui.status("FAIL", ui.t("not_installed", dir=paths.INSTALL_DIR))
        return False
    if paths.role() == "node":
        ui.status("FAIL", ui.t("bk_node"))
        return False
    return True


def _mounts():
    mounts = {}
    for volume in VOLUMES:
        mounts[volume] = mountpoint(volume)
        if not mounts[volume] or not os.path.isdir(mounts[volume]):
            ui.status("FAIL", ui.t("bk_volume_missing", name="%s_%s" % (paths.project(), volume)))
            return None
    return mounts


def _space(root, mounts):
    for p in mounts.values():
        if os.path.commonpath([os.path.realpath(root), os.path.realpath(p)]) == os.path.realpath(p):
            raise ValueError("backup directory must be outside data volumes")
    need = sum(dir_bytes(p) for p in mounts.values()) + (1 << 30)
    free = shutil.disk_usage(root).free
    if free < need:
        ui.status("FAIL", ui.t("bk_space", dir=root, need=need / 2.0 ** 30, free=free / 2.0 ** 30))
        return False
    return True


def _unit_starting():
    code, out = run(["systemctl", "show", "-p", "ActiveState", "--value", paths.UNIT_NAME])
    return code == 0 and out.strip() == "activating"


# ------------------------------------------------------------------ the commands

def take(manifest, root, wait_minutes):
    """One copy. Returns (exit code, copy directory or None)."""
    if _unit_starting():
        ui.status("WARN", ui.t("bk_stack_starting"))
        return 1, None
    lock = _prepare(root)
    if lock is None:
        ui.status("FAIL", ui.t("bk_locked", path=root))
        return 1, None
    try:
        mounts = _mounts()
        if mounts is None or not _space(root, mounts):
            return 1, None
        running = [s for s, (state, _) in states((WEB,) + DATA).items() if state == "running"]
        if running and "mysql" not in running:
            raise RuntimeError("MySQL is not running: cannot check parsing")
        if running and not wait_parsing(wait_minutes):
            ui.status("WARN", ui.t("bk_parsing_busy", min=wait_minutes))
            return 1, None
        name = "ragspark-" + time.strftime("%Y%m%d-%H%M%S")
        info = _describe(name, manifest)
        t0 = time.time()
        ui.status("DO", ui.t("bk_stopping"))
        err, path = None, None
        try:
            err = stop(running, check_parsing=True)
            if err is None:
                path = _write(root, name, mounts, info)
        except BaseException as e:  # restore availability even on Ctrl-C or SIGTERM
            err = str(e)[-300:] or type(e).__name__
        finally:
            ui.status("DO", ui.t("bk_starting"))
            started = start(running)
        down = time.time() - t0
        if path:
            info["downtime_s"] = round(down, 1)
            meta = os.path.join(path, "backup.json")
            with open(meta + ".tmp", "w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(meta + ".tmp", meta)
        if not started:
            ui.status("FAIL", ui.t("bk_start_failed"))
        if err:
            ui.status("FAIL", ui.t("bk_failed", err=err))
            return 1, None
        size = sum(v["bytes"] for v in info["volumes"].values())
        ui.status("OK", ui.t("bk_done", name=name, size=human(size), down=down, dir=root))
        gone = prune(root)
        if gone:
            ui.status("OK", ui.t("bk_pruned", names=", ".join(gone)))
        return (0 if started else 1), path
    finally:
        lock.close()


def list_copies(root):
    rows = copies(root)
    if not rows:
        ui.say(ui.t("bk_list_empty", dir=root))
        return 0
    ui.header(ui.t("bk_list_title", dir=root))
    for name, path, info in rows:
        size = sum(v.get("bytes", 0) for v in info.get("volumes", {}).values())
        ui.say("  " + ui.t("bk_list_row", name=name, size=human(size), release=info.get("release", "?"),
                           llm=info.get("llm", "?"), down=float(info.get("downtime_s", 0) or 0),
                           note=ui.t("bk_list_safety") if is_safety(name) else ""))
    return 0


def schedule(manifest, on):
    """The nightly timer: written and enabled, or disabled and removed. New unit files need no
    daemon-reload (systemd loads them when first used), so a live stack is not disturbed."""
    from .install import _write_unit                   # install imports this module: no cycle at load time
    if on:
        python = shutil.which("python3") or "/usr/bin/python3"
        changed = _write_unit(paths.host(paths.BACKUP_SERVICE_PATH),
                              BACKUP_SERVICE_UNIT.format(python=python, install_dir=paths.INSTALL_DIR), manifest)
        changed = _write_unit(paths.host(paths.BACKUP_TIMER_PATH), BACKUP_TIMER_UNIT, manifest) or changed
        if changed:
            run(["systemctl", "daemon-reload"])
        code, out = run(["systemctl", "enable", "--now", paths.BACKUP_TIMER])
        if code != 0:
            ui.status("FAIL", ui.t("bk_schedule_failed", timer=paths.BACKUP_TIMER, err=out.strip()[-300:]))
            return False
        manifest.set("backup_timer", paths.BACKUP_TIMER)
        manifest.save()
        ui.status("OK", ui.t("bk_schedule_on", timer=paths.BACKUP_TIMER, dir=paths.BACKUP_DIR))
        return True
    run(["systemctl", "disable", "--now", paths.BACKUP_TIMER])
    for p in (paths.BACKUP_TIMER_PATH, paths.BACKUP_SERVICE_PATH):
        p = paths.host(p)
        if os.path.exists(p):
            os.remove(p)
        if p in manifest.data.get("files", []):
            manifest.data["files"].remove(p)
    manifest.set("backup_timer", None)
    manifest.save()
    ui.status("OK", ui.t("bk_schedule_off", timer=paths.BACKUP_TIMER, dir=paths.BACKUP_DIR))
    return True


def backup(args):
    manifest = Manifest.load()
    if not _check(manifest):
        return 1
    root = root_dir(args)
    if getattr(args, "list", False):
        return list_copies(root)
    if getattr(args, "schedule", None):
        return 0 if schedule(manifest, args.schedule == "on") else 1
    if not getattr(args, "scheduled", False):
        ui.open_log(os.path.join(paths.install_dir(), "install.log"))
    ui.header(ui.t("bk_title"))
    wait = getattr(args, "wait_minutes", None)
    with recoverable_signals():
        try:
            code, _ = take(manifest, root, WAIT_PARSE_MIN if wait is None else wait)
            return code
        except (OSError, ValueError, RuntimeError) as e:
            ui.status("FAIL", ui.t("bk_failed", err=str(e)[-300:]))
            return 1


def restore(args):
    with recoverable_signals():
        try:
            return _restore(args)
        except (OSError, ValueError, RuntimeError) as e:
            ui.status("FAIL", ui.t("bk_failed", err=str(e)[-300:]))
            return 1


def _restore(args):
    manifest = Manifest.load()
    if not _check(manifest):
        return 1
    root = root_dir(args)
    src = os.path.abspath(args.name) if os.sep in args.name else os.path.join(root, args.name)
    ui.open_log(os.path.join(paths.install_dir(), "install.log"))
    ui.header(ui.t("rs_title"))
    lock = _prepare(root)
    if lock is None:
        ui.status("FAIL", ui.t("bk_locked", path=root))
        return 1
    try:
        try:
            with open(os.path.join(src, "backup.json"), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            ui.status("FAIL", ui.t("rs_incomplete", path=src))
            return 1
        bad = verify(src, info)
        if bad:
            ui.status("FAIL", ui.t("rs_damaged", name=os.path.basename(src), file=bad))
            return 1
        ui.status("OK", ui.t("rs_verified", name=os.path.basename(src)))
        current_images = images()
        if info["images"] != current_images:
            ui.status("FAIL", ui.t("rs_images_mismatch"))
            return 1
        if _unit_starting():
            ui.status("FAIL", ui.t("bk_stack_starting"))
            return 1
        mounts = _mounts()
        if mounts is None or not _space(root, mounts):
            return 1
        for directory in mounts.values():
            if os.path.commonpath([os.path.realpath(src), os.path.realpath(directory)]) == os.path.realpath(directory):
                raise ValueError("restore source must be outside data volumes")
        # Restored files may be much larger than the current data: reserve their expansion too.
        needed = {}
        locations = {}
        for directory, count in [(root, sum(dir_bytes(p) for p in mounts.values()))] + [
                (mounts[v], e["source_bytes"]) for v, e in info["volumes"].items()]:
            dev = os.stat(directory).st_dev
            needed[dev] = needed.get(dev, 0) + count
            locations[dev] = directory
        if any(shutil.disk_usage(locations[dev]).free < count + (1 << 30) for dev, count in needed.items()):
            raise RuntimeError("insufficient space for restored data and safety copy")
        safety = "ragspark-" + time.strftime("%Y%m%d-%H%M%S") + SAFETY
        ui.say(ui.t("rs_plan", created=info.get("created", "?"), safety=safety, dir=root))
        if not ui.confirm(ui.t("rs_confirm", name=os.path.basename(src)), default=False):
            ui.say(ui.t("cancelled"))
            return 1
        env_path = os.path.join(paths.compose_dir(), ".env")
        services = (WEB,) + DATA
        running = [s for s, (state, _) in states(services).items() if state == "running"]
        t0, saved, changed, err = time.time(), None, [], None
        try:
            ui.status("DO", ui.t("bk_stopping"))
            failure = stop(services)
            if failure:
                raise RuntimeError(failure)
            saved = _write(root, safety, mounts, _describe(safety, manifest))
            ui.status("OK", ui.t("rs_safety_done", name=safety))
            for volume in VOLUMES:
                entry = info["volumes"][volume]
                code, out = extract(os.path.join(src, entry["file"]), mounts[volume])
                if code != 0:
                    raise RuntimeError("%s: %s" % (volume, out.strip()[-300:]))
                ui.status("OK", ui.t("rs_volume_done", name=volume))
            old, new = read_env(env_path), read_env(os.path.join(src, info["files"]["env"]["file"]))
            changed = [k for k in DATA_ENV if new[k] != old.get(k)]
            for k in changed:
                set_env(env_path, k, new[k])
            if changed:
                ui.status("OK", ui.t("rs_env", keys=", ".join(changed)))
            if not start(running, recreate=bool(changed)):
                raise RuntimeError("restored services failed their health checks")
        except BaseException as e:
            err = str(e)[-300:] or type(e).__name__
            if saved:
                try:
                    failure = stop(services)
                    if failure:
                        raise RuntimeError(failure)
                    with open(os.path.join(saved, "backup.json"), encoding="utf-8") as f:
                        saved_info = json.load(f)
                    for volume in VOLUMES:
                        entry = saved_info["volumes"][volume]
                        code, out = extract(os.path.join(saved, entry["file"]), mounts[volume])
                        if code != 0:
                            raise RuntimeError(out.strip()[-300:])
                    shutil.copyfile(os.path.join(saved, "env"), env_path)
                    os.chmod(env_path, 0o600)
                    # Recreate even after a failed forward restart which may have changed container envs.
                    if not start(running, recreate=True):
                        raise RuntimeError("previous services failed their health checks")
                    ui.status("WARN", ui.t("rs_rollback", err=err, safety=safety))
                except BaseException as e2:
                    stop(services)
                    ui.status("FAIL", ui.t("rs_rollback_failed", err="%s; %s" % (err, str(e2)[-200:]), safety=safety))
                    return 1
            elif not start(running):
                ui.status("FAIL", ui.t("bk_start_failed"))
            ui.status("FAIL", ui.t("rs_failed", err=err))
            return 1
        ui.status("OK", ui.t("rs_done", name=os.path.basename(src), safety=safety, down=time.time() - t0))
        return 0
    finally:
        lock.close()


def last_copy():
    """(name, hours ago) of the newest copy in the default directory, or None."""
    rows = copies(paths.host(paths.BACKUP_DIR))
    if not rows:
        return None
    return rows[0][0], (datetime.datetime.now() - stamp(rows[0][0])).total_seconds() / 3600

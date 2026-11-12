"""Cable backup transport. SSH can write to incoming, never to the root-owned vault.

A root receiver timer snapshots uploads, verifies them and publishes them without replacement.
Receipts are read-only through the same forced SSH command. The sender never deletes remote data.
No Docker calls, database stops or extraction occur in this module.
"""
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import time

from . import backup, paths, ui
from .sysutil import run

BASE = "/var/lib/ragspark-backup"
INCOMING = "/srv/ragspark/backup-incoming"
VAULT = "/srv/ragspark/backups"
USER = "ragspark-backup"
MAX_META = 1 << 20
RECEIVE_SERVICE = "ragspark-backup-receive.service"
RECEIVE_TIMER = "ragspark-backup-receive.timer"
SYNC_SERVICE = "ragspark-backup-sync.service"
SYNC_TIMER = "ragspark-backup-sync.timer"


def config_path():
    return paths.host(BASE + "/sender.json")


def config():
    try:
        with open(config_path()) as f:
            c = json.load(f)
    except FileNotFoundError:
        return None
    c["peer"] = str(ipaddress.IPv4Address(c["peer"]))
    c["source"] = str(ipaddress.IPv4Address(c["source"]))
    for k in ("key", "known_hosts"):
        if not os.path.isabs(c[k]):
            raise ValueError("invalid sender path")
    return c


def _atomic_json(path, value, mode=0o600):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ssh_args(c):
    return ["ssh", "-F", "/dev/null", "-i", c["key"], "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "-o", "UserKnownHostsFile=" + c["known_hosts"], "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-b", c["source"]]


def receipt(c, name, digest):
    code, out = run(ssh_args(c) + [USER + "@" + c["peer"], "receipt " + name], timeout=25,
                    log_output=False)
    if code != 0:
        return False
    try:
        r = json.loads(out)
        return r["name"] == name and r["sha256"] == digest
    except (ValueError, TypeError, KeyError):
        return False


def _upload(c, directory, name):
    dest = USER + "@" + c["peer"] + ":" + name + "/"
    # backup.json is uploaded last. No --delete, owner changes, symlinks or remote shell snippets.
    common = ["rsync", "-rt", "--checksum", "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=", "--timeout=60",
              "-e", shlex.join(ssh_args(c))]
    code, out = run(common + ["--exclude=backup.json", "--exclude=*.tmp", directory + "/", dest],
                    timeout=3600, log_output=False)
    if code != 0:
        return False
    code, _ = run(common + [os.path.join(directory, "backup.json"), dest], timeout=120, log_output=False)
    return code == 0


def sync(root, wait_seconds=90, required=False):
    try:
        return _sync(root, wait_seconds, required)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
        ui.status("WARN", ui.t("bk_remote_error", err=str(e)[-200:]))
        if os.path.isfile(config_path()):
            _atomic_json(paths.host(BASE + "/sender-status.json"),
                         {"time": time.time(), "ok": False, "copies": 0, "peer": "?"})
        return False


def _sync(root, wait_seconds=90, required=False):
    """Retry retained complete local copies. Success requires the node's verified receipt."""
    c = config()
    if c is None:
        if required:
            ui.status("FAIL", ui.t("bk_remote_not_configured"))
            return False
        return True                         # local-only installations remain local-only
    lock = backup._prepare(root)
    if lock is None:
        ui.status("WARN", ui.t("bk_locked", path=root))
        return False
    ok, checked = True, 0
    try:
        for name, directory, info in backup.copies(root):
            checked += 1
            digest = backup.sha256(os.path.join(directory, "backup.json"))
            if receipt(c, name, digest):
                continue
            if backup.verify(directory, info) or not _upload(c, directory, name):
                ui.status("WARN", ui.t("bk_remote_failed", name=name))
                ok = False
                continue
            deadline = time.monotonic() + wait_seconds
            while True:
                if receipt(c, name, digest):
                    ui.status("OK", ui.t("bk_remote_done", name=name, peer=c["peer"]))
                    break
                if time.monotonic() >= deadline:
                    ui.status("WARN", ui.t("bk_remote_pending", name=name))
                    ok = False
                    break
                time.sleep(5)
        _atomic_json(paths.host(BASE + "/sender-status.json"),
                     {"time": time.time(), "ok": ok, "copies": checked, "peer": c["peer"]})
        return ok
    finally:
        lock.close()


def dispatch(original):
    """Forced-command entry point, executed as the unprivileged receiving account."""
    match = re.fullmatch(r"receipt (ragspark-\d{8}-\d{6}(?:-before-restore)?)", original)
    if match and backup.stamp(match[1]):
        path = paths.host(BASE + "/receipts/" + match[1] + ".json")
        try:
            with open(path) as f:
                value = f.read(MAX_META)
            print(value)
            return 0
        except FileNotFoundError:
            return 3
    if original.startswith("rsync --server "):
        os.umask(0o077)
        # rrsync validates rsync options and confines the destination; pulling/deleting is denied.
        os.execv("/usr/bin/rrsync", ["rrsync", "-wo", "-no-del", "-munge", paths.host(INCOMING)])
    return 2


def _read_file(dirfd, name, maximum):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError("not an ordinary upload file")
        if maximum is not None and st.st_size > maximum:
            raise ValueError("metadata too large")
        value = f.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("metadata grew during read")
        return value


def _snapshot(source, temp):
    """Read via directory fd + O_NOFOLLOW, never follow uploader-controlled paths as root."""
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = _read_file(fd, "backup.json", MAX_META)
        info = json.loads(raw)
        if set(info["volumes"]) != set(backup.VOLUMES) or set(info["files"]) != {"env", "manifest"}:
            raise ValueError("invalid upload metadata")
        names = ["env", "manifest.json"]
        if info["files"]["env"]["file"] != "env" or info["files"]["manifest"]["file"] != "manifest.json":
            raise ValueError("invalid metadata names")
        for v in backup.VOLUMES:
            n = info["volumes"][v]["file"]
            if n not in (v + ".tar.gz", v + ".tar.zst"):
                raise ValueError("invalid volume filename")
            names.append(n)
        for n in names:
            srcfd = os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(srcfd, "rb") as src:
                st = os.fstat(src.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise ValueError("not an ordinary upload file")
                if st.st_size + (1 << 30) > shutil.disk_usage(temp).free:
                    raise ValueError("not enough space to verify the upload")
                with open(os.path.join(temp, n), "xb") as dst:
                    remaining = st.st_size
                    while remaining:
                        block = src.read(min(remaining, 1 << 20))
                        if not block:
                            raise ValueError("upload changed during verification")
                        dst.write(block)
                        remaining -= len(block)
                    if src.read(1):
                        raise ValueError("upload grew during verification")
                    dst.flush()
                    os.fsync(dst.fileno())
        with open(os.path.join(temp, "backup.json"), "xb") as dst:
            dst.write(raw)
            dst.flush()
            os.fsync(dst.fileno())
        tempfd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(tempfd)
        finally:
            os.close(tempfd)
        return info, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def receive():
    """Root timer: immutable vault publication, receipts, local retention. Never restores data."""
    vault, incoming = paths.host(VAULT), paths.host(INCOMING)
    receipt_dir = paths.host(BASE + "/receipts")
    lockfd = open(paths.host(BASE + "/receive.lock"), "w")
    try:
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        # A previous killed receiver can leave a private verification directory.
        for old in os.listdir(vault):
            if old.startswith(".receive-"):
                shutil.rmtree(os.path.join(vault, old))
        ok = True
        for name in sorted(os.listdir(incoming)):
            if not backup.stamp(name):
                continue
            source = os.path.join(incoming, name)
            if not os.path.lexists(os.path.join(source, "backup.json")):
                continue                         # partial uploads are never published
            temp = tempfile.mkdtemp(prefix=".receive-", dir=vault)
            try:
                info, digest = _snapshot(source, temp)
                if info["name"] != name or backup.verify(temp, info):
                    raise ValueError("upload validation failed")
                target = os.path.join(vault, name)
                if os.path.lexists(target):
                    if os.path.islink(target) or backup.sha256(os.path.join(target, "backup.json")) != digest:
                        raise ValueError("a different copy with this name already exists")
                    if backup.verify(target, info):
                        raise ValueError("existing vault copy is damaged")
                else:
                    os.rename(temp, target)
                    temp = None
                dfd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
                _atomic_json(os.path.join(receipt_dir, name + ".json"),
                             {"name": name, "sha256": digest, "verified": time.time()}, mode=0o644)
                shutil.rmtree(source)              # fd-safe rmtree, incoming only; never vault via SSH
                ui.status("OK", ui.t("bk_remote_done", name=name, peer="local vault"))
            except (OSError, ValueError, KeyError, TypeError) as e:
                ui.status("WARN", ui.t("bk_remote_failed", name=name))
                ui.log("receiver: " + str(e)[:200])
                ok = False
            finally:
                if temp is not None:
                    shutil.rmtree(temp)
        gone = backup.prune(vault)
        for name in gone:
            p = os.path.join(receipt_dir, name + ".json")
            if os.path.exists(p):
                os.unlink(p)
        return 0 if ok else 1
    finally:
        lockfd.close()

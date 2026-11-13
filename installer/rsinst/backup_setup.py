"""Explicit root setup, run by the administrator; never called during ordinary backup."""
import ipaddress
import json
import os
import pwd
import re
import shutil
import stat

from . import backup_remote as remote, paths
from .sysutil import run


def _private_dir(path, mode=0o700):
    os.makedirs(path, mode=mode, exist_ok=True)
    st = os.lstat(path)
    if os.path.islink(path) or st.st_uid != os.geteuid():
        raise ValueError("not an administrator-owned directory: " + path)
    os.chmod(path, mode)


def _incoming_parent(path):
    """Allow receiver traversal without exposing directory listings or the vault."""
    os.makedirs(path, mode=0o701, exist_ok=True)
    st = os.lstat(path)
    mode = stat.S_IMODE(st.st_mode)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or mode & 0o022:
        raise ValueError("unsafe incoming parent directory: " + path)
    os.chmod(path, mode | 0o001)


def _write(path, text, mode=0o600):
    if os.path.lexists(path) and os.path.islink(path):
        raise ValueError("refusing a symbolic link: " + path)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def _timers(manifest, kind):
    from .install import _write_unit
    service = remote.RECEIVE_SERVICE if kind == "receive" else remote.SYNC_SERVICE
    timer = remote.RECEIVE_TIMER if kind == "receive" else remote.SYNC_TIMER
    option = "--receive" if kind == "receive" else "--sync"
    trigger = "OnBootSec=2min\nOnUnitInactiveSec=30s" if kind == "receive" else "OnCalendar=hourly"
    unit = ("[Unit]\nDescription=RAGSpark backup %s\n\n[Service]\nType=oneshot\n"
            "ExecStart=/usr/bin/python3 %s/installer/ragspark.py backup %s\nNice=10\n"
            "IOSchedulingClass=idle\nTimeoutStartSec=3h\n" % (kind, paths.INSTALL_DIR, option))
    content = "[Unit]\nDescription=RAGSpark backup %s timer\n\n[Timer]\n%s\n\n[Install]\nWantedBy=timers.target\n" % (kind, trigger)
    changed = _write_unit(paths.host("/etc/systemd/system/" + service), unit, manifest)
    changed = _write_unit(paths.host("/etc/systemd/system/" + timer), content, manifest) or changed
    if changed:
        raise ValueError("existing backup unit changed: reload systemd in a maintenance window before enabling")
    code, out = run(["systemctl", "enable", "--now", timer])
    if code:
        raise RuntimeError(out[-200:])
    manifest.set("backup_remote_units", [service, timer])


def sender(manifest, peer, source, host_key_file):
    if paths.role() != "head":
        raise ValueError("sender setup belongs on the head")
    peer, source = str(ipaddress.IPv4Address(peer)), str(ipaddress.IPv4Address(source))
    for tool in ("rsync", "ssh", "ssh-keygen"):
        if not shutil.which(tool):
            raise ValueError("missing " + tool)
    with open(host_key_file) as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not lines or any(not re.fullmatch(re.escape(peer) + r" ssh-ed25519 [A-Za-z0-9+/=]+", line) for line in lines):
        raise ValueError("host key file must contain the independently verified peer IPv4 and Ed25519 key")
    base = paths.host(remote.BASE)
    _private_dir(base)
    key, hosts = os.path.join(base, "id_ed25519"), os.path.join(base, "known_hosts")
    if not os.path.exists(key):
        code, out = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], log_output=False)
        if code:
            raise RuntimeError("key creation failed")
    if os.path.islink(key) or os.stat(key).st_uid != os.geteuid():
        raise ValueError("invalid sender key")
    os.chmod(key, 0o600)
    _write(hosts, "\n".join(lines) + "\n")
    remote._atomic_json(remote.config_path(), {"peer": peer, "source": source, "key": key, "known_hosts": hosts})
    _timers(manifest, "sync")
    with open(key + ".pub") as f:
        print(f.read().strip())                    # public key only, for authorization on the node


def receiver(manifest, source, public_key_file):
    if paths.role() != "node":
        raise ValueError("receiver setup belongs on the second node")
    source = str(ipaddress.IPv4Address(source))
    if not shutil.which("rrsync"):
        raise ValueError("missing rrsync")
    with open(public_key_file) as f:
        public_key = f.read().strip()
    parts = public_key.split()
    if len(parts) not in (2, 3) or parts[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
        raise ValueError("expected one Ed25519 public key")
    base, incoming, vault = (paths.host(p) for p in (remote.BASE, remote.INCOMING, remote.VAULT))
    _private_dir(base, 0o755)
    _private_dir(base + "/receipts", 0o755)
    _incoming_parent(os.path.dirname(incoming))
    _private_dir(vault)
    home = base + "/receiver"
    marker = base + "/receiver.json"
    try:
        user = pwd.getpwnam(remote.USER)
        if not os.path.isfile(marker):
            raise ValueError("pre-existing account is not managed by RAGSpark backups")
        with open(marker) as f:
            managed = json.load(f)
        if managed["uid"] != user.pw_uid or user.pw_dir != home or user.pw_shell != "/bin/sh":
            raise ValueError("existing receiver account does not match the dedicated account")
    except KeyError:
        code, _ = run(["useradd", "--system", "--home-dir", home, "--shell", "/bin/sh", remote.USER])
        if code:
            raise RuntimeError("cannot create receiver account")
        user = pwd.getpwnam(remote.USER)
    if user.pw_uid == 0 or set(os.getgrouplist(remote.USER, user.pw_gid)) != {user.pw_gid}:
        raise ValueError("receiver must be unprivileged and have no supplementary groups")
    remote._atomic_json(marker, {"uid": user.pw_uid, "home": home, "source": source})
    # Root owns the home and authorized_keys: the receiving account cannot alter its SSH restrictions.
    _private_dir(home, 0o755)
    _private_dir(home + "/.ssh", 0o755)
    os.makedirs(incoming, mode=0o700, exist_ok=True)
    if os.path.islink(incoming) or os.stat(incoming).st_uid not in (os.geteuid(), user.pw_uid):
        raise ValueError("unexpected incoming directory owner")
    os.chmod(incoming, 0o700)
    os.chown(incoming, user.pw_uid, user.pw_gid)
    command = "/usr/bin/python3 -I %s/tools/backup_receive.py" % paths.INSTALL_DIR
    auth = 'restrict,from="%s",command="%s" %s %s\n' % (source, command, parts[0], parts[1])
    _write(home + "/.ssh/authorized_keys", auth, 0o644)
    _timers(manifest, "receive")

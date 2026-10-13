"""Running commands and small system helpers."""
import json
import os
import re
import shutil
import socket
import subprocess

from . import paths, ui


def run(cmd, capture=True, cwd=None, env=None, stdin=None, timeout=None, log_output=True):
    """Run a command. capture=False shows its output live (progress bars).

    Returns (exit code, captured stdout+stderr). A missing program is exit 127.
    log_output=False keeps the output out of the install log (it may hold passwords).
    """
    ui.log("$ " + " ".join(str(c) for c in cmd) + (" (in %s)" % cwd if cwd else ""))
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        if capture:
            p = subprocess.run(cmd, cwd=cwd, env=full_env, stdin=stdin, timeout=timeout,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               errors="replace")
            out, err = p.stdout or "", p.stderr or ""
            if log_output:
                if out.strip():
                    ui.log(out.rstrip()[-4000:])
                if err.strip():
                    ui.log("stderr: " + err.rstrip()[-2000:])
            if p.returncode != 0:
                # On failure the caller shows the output: include the error stream.
                return p.returncode, (out + "\n" + err).strip()
            # On success only stdout: warnings on stderr must not be parsed as data.
            return p.returncode, out
        p = subprocess.run(cmd, cwd=cwd, env=full_env, stdin=stdin, timeout=timeout)
        ui.log("exit %d" % p.returncode)
        return p.returncode, ""
    except FileNotFoundError:
        ui.log("not found: " + cmd[0])
        return 127, "%s: not found" % cmd[0]
    except subprocess.TimeoutExpired:
        ui.log("timeout")
        return 124, "timeout"


def which(program):
    return shutil.which(program)


def version_tuple(text):
    """'29.6.2' or 'v2.39.4-desktop.1' -> (29, 6, 2)."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return ()
    return tuple(int(x) for x in m.groups(default="0"))


def compose(args, capture=True, stdin=None, timeout=None):
    """docker compose in the installed compose directory (.env picks the files).

    The output of `config` is never logged: it contains the passwords from .env.
    """
    return run(["docker", "compose"] + args, capture=capture, cwd=paths.compose_dir(),
               stdin=stdin, timeout=timeout, log_output=args[:1] != ["config"])


def compose_json(args):
    """docker compose ... --format json; accepts one array or one object per line."""
    code, out = compose(args + ["--format", "json"])
    if code != 0:
        return None
    out = out.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except ValueError:
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        return rows


def read_env(path):
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k] = v
    return values


def set_env(path, key, value):
    """Set KEY=value in an env file, keeping comments and order."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    done = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = "%s=%s\n" % (key, value)
            done = True
    if not done:
        lines.append("%s=%s\n" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def host_address():
    """The address of the default route, for the URL shown to the user.

    Connecting a UDP socket sends nothing; it only picks the outgoing address.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addr = s.getsockname()[0]
            if addr and not addr.startswith("127."):
                return addr
    except OSError:
        pass
    code, out = run(["hostname", "-I"])
    if code == 0:
        for item in out.split():
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", item) and not item.startswith("127."):
                return item
    return "localhost"


def project_containers():
    """Containers of the compose project `ragspark`, running or not."""
    code, out = run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + paths.PROJECT,
                     "--format", "{{.Names}}\t{{.State}}\t{{.Label \"com.docker.compose.project.working_dir\"}}"])
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"name": parts[0], "state": parts[1], "dir": parts[2]})
    return rows

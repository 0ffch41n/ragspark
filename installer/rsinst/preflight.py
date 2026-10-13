"""System check before installation (`ragspark check`)."""
import glob
import json
import os
import platform
import re
import shutil
import socket
import ssl
import sys
import time

from . import paths, ui
from .sysutil import project_containers, run, version_tuple

MIN_DOCKER = (24, 0, 0)
MIN_COMPOSE = (2, 24, 0)
REGISTRIES = ["registry-1.docker.io", "ghcr.io", "huggingface.co"]
SLOW_HANDSHAKE_S = 5.0      # Docker gives up on a registry TLS handshake after 10 s


class Report:
    def __init__(self):
        self.rows = []
        self.blockers = 0

    def add(self, kind, text):
        self.rows.append((kind, text))
        ui.status(kind, text)

    def block(self, text):
        """A failure that --force must not override: going on would damage existing data."""
        self.blockers += 1
        self.add("FAIL", text)

    @property
    def failed(self):
        return any(k == "FAIL" for k, _ in self.rows)

    @property
    def warnings(self):
        return sum(1 for k, _ in self.rows if k == "WARN")


def _os_name():
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def check_platform(r):
    arch = platform.machine()
    if arch == "aarch64":
        r.add("OK", ui.t("pf_arch_ok", arch=arch, os=_os_name()))
    else:
        r.add("FAIL", ui.t("pf_arch_bad", arch=arch))
    if sys.version_info >= (3, 10):
        r.add("OK", ui.t("pf_python_ok", ver=platform.python_version()))
    else:
        r.add("FAIL", ui.t("pf_python_bad", ver=platform.python_version()))


def check_gpu(r):
    code, out = run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    if code != 0 or not out.strip():
        r.add("FAIL", ui.t("pf_gpu_none"))
        return
    name, _, driver = out.strip().splitlines()[0].partition(",")
    name, driver = name.strip(), driver.strip()
    if "GB10" in name:
        r.add("OK", ui.t("pf_gpu_ok", name=name))
    else:
        r.add("WARN", ui.t("pf_gpu_other", name=name))
    if driver.split(".")[0] == "580":
        r.add("OK", ui.t("pf_driver_ok", ver=driver))
    else:
        r.add("WARN", ui.t("pf_driver_other", ver=driver))


def check_memory(r):
    total = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    if total >= 110:
        r.add("OK", ui.t("pf_mem_ok", gib=total))
    elif total >= 90:
        r.add("WARN", ui.t("pf_mem_low", gib=total))
    else:
        r.add("FAIL", ui.t("pf_mem_bad", gib=total))


def docker_info():
    code, out = run(["docker", "info", "--format", "{{json .}}"])
    if code != 0:
        return None
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def check_docker(r):
    """Returns docker info (dict) or None."""
    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        r.add("FAIL", ui.t("pf_docker_none"))
        return None
    ver = out.strip().splitlines()[-1] if out.strip() else ""
    if version_tuple(ver) >= MIN_DOCKER:
        r.add("OK", ui.t("pf_docker_ok", ver=ver))
    else:
        r.add("FAIL", ui.t("pf_docker_old", ver=ver, need="%d.%d" % MIN_DOCKER[:2]))
    code, out = run(["docker", "compose", "version", "--short"])
    cver = out.strip().splitlines()[-1] if code == 0 and out.strip() else ""
    if not cver:
        r.add("FAIL", ui.t("pf_compose_none"))
    elif version_tuple(cver) >= MIN_COMPOSE:
        r.add("OK", ui.t("pf_compose_ok", ver=cver))
    else:
        r.add("FAIL", ui.t("pf_compose_old", ver=cver, need="%d.%d" % MIN_COMPOSE[:2]))
    info = docker_info()
    if info is None:
        r.add("FAIL", ui.t("pf_docker_info"))
        return None
    runtimes = info.get("Runtimes") or {}
    cdi = bool(glob.glob("/etc/cdi/nvidia*.yaml") or glob.glob("/var/run/cdi/nvidia*.yaml"))
    if "nvidia" in runtimes or cdi:
        r.add("OK", ui.t("pf_nvidia_runtime_ok"))
    else:
        r.add("WARN", ui.t("pf_nvidia_runtime_unknown"))
    r.add("INFO", ui.t("pf_cgroup", driver=info.get("CgroupDriver", "?")))
    mirrors = (info.get("RegistryConfig") or {}).get("Mirrors") or []
    if mirrors:
        r.add("INFO", ui.t("pf_mirrors", mirrors=", ".join(mirrors)))
    return info


def _free_gib(path):
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    try:
        return shutil.disk_usage(path or "/").free / 1024 ** 3, path or "/"
    except OSError:
        return None, path


def check_disk(r, info, models_dir):
    seen = set()
    for label, path in (("docker", (info or {}).get("DockerRootDir", "/var/lib/docker")),
                        ("models", models_dir)):
        free, existing = _free_gib(paths.host(path))
        if free is None:
            continue
        dev = os.stat(existing).st_dev
        if dev in seen:
            continue
        seen.add(dev)
        text = ui.t("pf_disk", path=path, gib=free)
        if free < 25:
            r.add("FAIL", text + " — " + ui.t("pf_disk_bad"))
        elif free < 100:
            r.add("WARN", text + " — " + ui.t("pf_disk_low"))
        else:
            r.add("OK", text)


def check_port(r, bind, port):
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.bind((bind, int(port)))
        r.add("OK", ui.t("pf_port_ok", bind=bind, port=port))
    except PermissionError:
        r.add("WARN", ui.t("pf_port_unknown", port=port))
    except OSError:
        r.add("FAIL", ui.t("pf_port_busy", bind=bind, port=port))
    finally:
        s.close()


def check_existing(r, manifest):
    if manifest is not None:
        if manifest.status == "installed":
            r.add("INFO", ui.t("pf_installed", dir=paths.INSTALL_DIR))
        else:
            r.add("WARN", ui.t("pf_interrupted"))
        return
    # Without a manifest nothing of ours exists yet: whatever is there belongs to someone else.
    if os.path.exists(paths.install_dir()):
        r.block(ui.t("pf_dir_exists", dir=paths.INSTALL_DIR))
    if os.path.exists(paths.host(paths.UNIT_PATH)):
        r.block(ui.t("pf_unit_exists", unit=paths.UNIT_PATH))
    rows = project_containers()
    code, out = run(["docker", "volume", "ls", "--filter", "label=com.docker.compose.project=" + paths.PROJECT,
                     "--format", "{{.Name}}"])
    volumes = out.split() if code == 0 else []
    if rows:
        where = rows[0]["dir"] or "?"
        r.block(ui.t("pf_foreign_stack", n=len(rows), dir=where))
    elif volumes:
        r.block(ui.t("pf_foreign_volumes", volumes=" ".join(volumes)))
    else:
        r.add("OK", ui.t("pf_no_stack"))


def handshake(host, family, timeout=20.0):
    """Seconds for TCP connect + TLS handshake, or an error string."""
    try:
        infos = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if not infos:
        return None
    t0 = time.monotonic()
    try:
        with socket.create_connection(infos[0][4][:2], timeout=timeout) as s:
            with ssl.create_default_context().wrap_socket(s, server_hostname=host):
                return time.monotonic() - t0
    except (OSError, ssl.SSLError) as e:
        return "%s" % (e.__class__.__name__ if not str(e) else str(e)[:60])


def check_network(r, info):
    hosts = list(REGISTRIES)
    for m in ((info or {}).get("RegistryConfig") or {}).get("Mirrors") or []:
        h = re.sub(r"^https?://", "", m).split("/")[0]
        if h and h not in hosts:
            hosts.append(h)
    for h in hosts:
        parts, times = [], []
        for fam, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
            res = handshake(h, fam)
            if res is None:
                continue                      # no address of this family
            if isinstance(res, float):
                parts.append("%s %.1f s" % (label, res))
                times.append(res)
            else:
                parts.append("%s %s" % (label, res))
        detail = ", ".join(parts) or ui.t("pf_net_noaddr")
        if not times:
            r.add("WARN", ui.t("pf_net_fail", host=h, detail=detail))
        elif max(times) > SLOW_HANDSHAKE_S:
            r.add("WARN", ui.t("pf_net_slow", host=h, detail=detail))
        else:
            r.add("OK", ui.t("pf_net_ok", host=h, detail=detail))


def run_checks(config, manifest, network=True, header=True):
    if header:
        ui.header(ui.t("pf_title"))
    r = Report()
    check_platform(r)
    check_gpu(r)
    check_memory(r)
    info = check_docker(r)
    check_disk(r, info, config.get("models_dir", paths.DEFAULT_MODELS_DIR))
    if manifest is None:
        check_port(r, config.get("http_bind", "0.0.0.0"), config.get("http_port", 80))
    check_existing(r, manifest)
    if network:
        check_network(r, info)
    if r.failed:
        ui.say("\n" + ui.t("pf_failed"))
    elif r.warnings:
        ui.say("\n" + ui.t("pf_warnings", n=r.warnings))
    else:
        ui.say("\n" + ui.t("pf_passed"))
    return r


def gpu_reload_test(image):
    """Does `systemctl daemon-reload` take the GPU away from running containers?

    Starts a throw-away container, checks nvidia-smi inside it, reloads
    systemd and checks again. Must not run while the stack is up.
    """
    ui.header(ui.t("gr_title"))
    if any(c["state"] == "running" for c in project_containers()):
        ui.status("FAIL", ui.t("gr_stack_running"))
        return False
    name = "ragspark-gpu-reload-check"
    run(["docker", "rm", "-f", name])
    code, out = run(["docker", "run", "-d", "--rm", "--name", name, "--gpus", "all",
                     "--entrypoint", "sleep", image, "600"])
    if code != 0:
        ui.status("FAIL", ui.t("gr_start_failed", err=out.strip()[-200:]))
        return False
    try:
        code, out = run(["docker", "exec", name, "nvidia-smi", "-L"])
        if code != 0:
            ui.status("FAIL", ui.t("gr_no_gpu", err=out.strip()[-200:]))
            return False
        ui.status("OK", ui.t("gr_before", gpu=out.strip().splitlines()[0]))
        run(["systemctl", "daemon-reload"])
        time.sleep(3)
        code, out = run(["docker", "exec", name, "nvidia-smi", "-L"])
        if code == 0:
            ui.status("OK", ui.t("gr_after_ok"))
            ui.say(ui.t("gr_verdict_ok"))
            return True
        ui.status("FAIL", ui.t("gr_after_lost", err=out.strip()[-200:]))
        ui.say(ui.t("gr_verdict_lost"))
        return False
    finally:
        run(["docker", "rm", "-f", name])

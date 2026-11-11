"""Host paths used by the installer.

RAGSPARK_PREFIX is for tests only: it is prepended to every host path, so a
test run writes into a scratch directory instead of /opt, /etc and /usr.
"""
import os

PREFIX = os.environ.get("RAGSPARK_PREFIX", "").rstrip("/")


def host(path):
    """A host path as the installer writes it (with the test prefix, if any)."""
    return PREFIX + path


# Where this code runs from: a clone of the repository or the installed copy.
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INSTALL_DIR = "/opt/ragspark"
UNIT_NAME = "ragspark.service"
UNIT_PATH = "/etc/systemd/system/" + UNIT_NAME
CLI_PATH = "/usr/local/bin/ragspark"
DEFAULT_MODELS_DIR = "/srv/ragspark/hf-cache"
PROJECT = "ragspark"
NODE_PROJECT = "ragspark-node"                      # the second unit of stack mode (compose/node/)
WATCH_SERVICE = "ragspark-watch.service"            # `ragspark watch`, run by the timer
WATCH_TIMER = "ragspark-watch.timer"
WATCH_SERVICE_PATH = "/etc/systemd/system/" + WATCH_SERVICE
WATCH_TIMER_PATH = "/etc/systemd/system/" + WATCH_TIMER
ROLES = ("head", "node")

# What the installed copy contains; .env is never copied, it is created in place.
COPY_DIRS = ["compose", "tools", "installer", "acceptance", "catalog"]
COPY_FILES = ["LICENSE", "README.md", "README.ru.md"]


# The role of this unit: "head" (RAGFlow and everything in single mode) or "node" (the second unit
# of stack mode). Set from the manifest by the command line, or from --role by the installer.
_role = {"value": "head"}


def set_role(role):
    _role["value"] = role if role in ROLES else "head"


def role():
    return _role["value"]


def project():
    """The compose project of this unit."""
    return NODE_PROJECT if role() == "node" else PROJECT


def install_dir():
    return host(INSTALL_DIR)


def compose_dir():
    """Where `docker compose` runs: compose/ on the head, compose/node/ on the node."""
    base = os.path.join(host(INSTALL_DIR), "compose")
    return os.path.join(base, "node") if role() == "node" else base


def manifest_path():
    return os.path.join(host(INSTALL_DIR), "manifest.json")

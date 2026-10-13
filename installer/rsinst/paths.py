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

# What the installed copy contains; .env is never copied, it is created in place.
COPY_DIRS = ["compose", "tools", "installer", "acceptance", "catalog"]
COPY_FILES = ["LICENSE", "README.md", "README.ru.md"]


def install_dir():
    return host(INSTALL_DIR)


def compose_dir():
    return os.path.join(host(INSTALL_DIR), "compose")


def manifest_path():
    return os.path.join(host(INSTALL_DIR), "manifest.json")

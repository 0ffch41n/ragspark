"""Stand-in for RAGFlow's common.settings in tests. RAGSPARK_FAKE_NO_SERVICES=1 makes it fail,
like a container where RAGFlow's services cannot start."""
import os


def init_settings():
    if os.environ.get("RAGSPARK_FAKE_NO_SERVICES") == "1":
        raise RuntimeError("no database connection (test)")

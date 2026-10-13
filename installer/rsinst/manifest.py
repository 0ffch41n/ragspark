"""The install manifest: everything the installer created, so uninstall can mirror it.

Things that existed before the installation (directories, images, model
weights) are never listed, so uninstall never removes them.
"""
import json
import os
import time

from . import paths


class Manifest:
    def __init__(self, data=None):
        self.data = data or {
            "schema": 1,
            "status": "installing",
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "config": {},
            "created_dirs": [],
            "files": [],
            "images_pulled": [],
            "models_downloaded": [],
            "unit": None,
        }

    @classmethod
    def load(cls):
        path = paths.manifest_path()
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def save(self):
        path = paths.manifest_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)

    def _add(self, key, value):
        if value not in self.data[key]:
            self.data[key].append(value)
            self.save()

    def add_dir(self, path):
        self._add("created_dirs", path)

    def add_file(self, path):
        self._add("files", path)

    def add_image(self, ref):
        self._add("images_pulled", ref)

    def add_model(self, repo):
        self._add("models_downloaded", repo)

    @property
    def config(self):
        return self.data["config"]

    @property
    def status(self):
        return self.data.get("status")

    def set(self, key, value):
        self.data[key] = value
        self.save()


def make_dirs(path, manifest):
    """mkdir -p that records every directory it creates (outermost first).

    All directories are created before any is recorded: the manifest itself
    lives in the install directory, which may be one of them.
    """
    missing = []
    current = path
    while current and current != "/" and not os.path.exists(current):
        missing.append(current)
        current = os.path.dirname(current)
    missing.reverse()
    for d in missing:
        os.mkdir(d)
    if manifest is not None:
        for d in missing:
            manifest.add_dir(d)

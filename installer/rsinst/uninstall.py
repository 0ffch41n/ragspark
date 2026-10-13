"""`ragspark uninstall`: remove what the manifest says the installer created.

Never removes anything that existed before the installation: pre-existing
directories, images and model weights are not in the manifest.
"""
import os
import shutil

from . import paths, ui
from .manifest import Manifest
from .sysutil import compose, run

# Left by huggingface_hub in a cache directory; safe to delete once our models are gone.
HF_LEFTOVERS = [os.path.join("hub", ".locks"), os.path.join("hub", "version.txt"), "version.txt"]


def _model_dirs(models_dir, repo):
    base = os.path.join(paths.host(models_dir), "hub")
    name = "models--" + repo.replace("/", "--")
    return [os.path.join(base, name), os.path.join(base, ".locks", name)]


def _remove_install_dir(created):
    """The whole directory if the installer created it, otherwise only what it put there."""
    install_dir = paths.install_dir()
    if install_dir in created:
        shutil.rmtree(install_dir, ignore_errors=True)
        return
    for name in paths.COPY_DIRS:
        shutil.rmtree(os.path.join(install_dir, name), ignore_errors=True)
    for name in paths.COPY_FILES + ["VERSION", "install.log", "manifest.json"]:
        path = os.path.join(install_dir, name)
        if os.path.exists(path):
            os.remove(path)


def _rmdir(path):
    """Remove a directory only if it is empty; True if it is gone."""
    if not os.path.isdir(path):
        return not os.path.exists(path)
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def uninstall(args):
    manifest = Manifest.load()
    if manifest is None:
        ui.status("FAIL", ui.t("not_installed", dir=paths.INSTALL_DIR))
        return 1
    data, config = manifest.data, manifest.config
    models_dir = config.get("models_dir", paths.DEFAULT_MODELS_DIR)
    created = data.get("created_dirs", [])

    ui.header(ui.t("un_title"))
    ui.say(ui.t("un_will_remove"))
    ui.say("  - " + ui.t("un_item_data"))
    ui.say("  - " + ui.t("un_item_files", dir=paths.INSTALL_DIR, unit=paths.UNIT_PATH, cli=paths.CLI_PATH))
    images = [] if args.keep_images else data.get("images_pulled", [])
    models = [] if args.keep_models else data.get("models_downloaded", [])
    if images:
        ui.say("  - " + ui.t("un_item_images", n=len(images), list=", ".join(images)))
    if models:
        ui.say("  - " + ui.t("un_item_models", list=", ".join(models), dir=models_dir))
    ui.say(ui.t("un_kept"))
    if data.get("images_pulled") and args.keep_images:
        ui.say("  - " + ui.t("un_kept_images", n=len(data["images_pulled"])))
    if data.get("models_downloaded") and args.keep_models:
        ui.say("  - " + ui.t("un_kept_models", list=", ".join(data["models_downloaded"])))
    ui.say("  - " + ui.t("un_kept_preexisting"))
    if not ui.confirm(ui.t("un_confirm"), default=False):
        ui.say(ui.t("cancelled"))
        return 1

    if data.get("unit"):
        code, out = run(["systemctl", "disable", "--now", data["unit"]])
        if code == 0:
            ui.status("OK", ui.t("un_service_stopped", unit=data["unit"]))
        else:
            ui.status("WARN", ui.t("un_service_failed", unit=data["unit"], err=out.strip()[-160:]))
    if os.path.exists(os.path.join(paths.compose_dir(), ".env")):
        code, _ = compose(["down", "-v", "--remove-orphans"], capture=False)
        if code != 0:
            # .env holds the only copy of the database passwords: keep everything.
            ui.status("FAIL", ui.t("un_stack_failed"))
            ui.say(ui.t("un_aborted"))
            return 1
        ui.status("OK", ui.t("un_stack_removed"))

    for f in data.get("files", []):
        if os.path.exists(f):
            os.remove(f)
            ui.status("OK", ui.t("un_file_removed", path=f))
    if data.get("unit"):
        run(["systemctl", "daemon-reload"])
    for ref in images:
        # Only the exact reference the installer pulled; other tags of the image stay.
        code, out = run(["docker", "rmi", ref])
        if code == 0:
            ui.status("OK", ui.t("un_image_removed", ref=ref))
        else:
            ui.status("WARN", ui.t("un_image_failed", ref=ref, err=out.strip()[-160:]))
    for repo in models:
        for d in _model_dirs(models_dir, repo):
            shutil.rmtree(d, ignore_errors=True)
        ui.status("OK", ui.t("un_model_removed", repo=repo))

    _remove_install_dir(created)
    ui.status("OK", ui.t("un_dir_removed", path=paths.install_dir()))
    # Directories the installer created, innermost first, only when nothing of anyone else is left.
    for d in sorted(created, key=len, reverse=True):
        if d == paths.install_dir() or not os.path.exists(d):
            continue
        if d == paths.host(models_dir) and not args.keep_models:
            for leftover in HF_LEFTOVERS:
                p = os.path.join(d, leftover)
                if os.path.isfile(p):
                    os.remove(p)
                else:
                    _rmdir(p)
            _rmdir(os.path.join(d, "hub"))
        if _rmdir(d):
            ui.status("OK", ui.t("un_dir_removed", path=d))
        else:
            ui.status("INFO", ui.t("un_dir_kept", path=d))
    ui.say("\n" + ui.t("un_done"))
    return 0

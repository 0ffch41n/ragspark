"""`ragspark llm [name]`: show the LLM, or change it on a running installation.

Order matters: the new image and weights are fetched while the old LLM still
answers; only then does the LLM stop, the page cache is dropped (GB10: vLLM
sizes its memory share from free memory, which the cache of freshly read
weights hides) and the new LLM starts alone, while RAGFlow keeps serving the
web interface, datasets and search. If the new LLM does not become healthy,
the previous file is put back and started again. Once it is ready, compose
recreates ragflow (its environment names the model; about a minute; ragflow
depends on vllm-llm, so a plain `up` would keep it down for the whole LLM
start). RAGFlow then gets the new model, its chats and scan datasets move to
it (tools/ragflow_models.py), and the acceptance test runs.
"""
import os
import time

from . import catalog, install, paths, ui
from .manifest import Manifest
from .sysutil import compose, mem_total_gib, read_env, set_env

TOTAL = 4


def drop_page_cache():
    try:
        os.sync()
        with open(paths.host("/proc/sys/vm/drop_caches"), "w") as f:
            f.write("3\n")
        ui.status("OK", ui.t("sw_cache_dropped"))
    except OSError as e:
        ui.status("WARN", ui.t("sw_cache_failed", err=str(e)))


def restart_llm(env, llm, manifest):
    """Point .env at `llm` and restart the LLM alone, then the rest of the stack.
    Returns None when everything is healthy, "llm" when the LLM did not start,
    "stack" when the LLM runs but another service (ragflow) did not."""
    set_env(env, "COMPOSE_FILE", "compose.yaml:llm/%s.yaml" % llm)
    manifest.data["config"]["llm"] = llm
    manifest.save()
    compose(["stop", "vllm-llm"])
    drop_page_cache()
    code, _ = compose(["up", "-d", "--wait", "--wait-timeout", "2400", "--no-deps", "vllm-llm"], capture=False)
    if code != 0:
        return "llm"
    ui.status("OK", ui.t("sw_llm_ready", llm=llm))
    code, _ = compose(["up", "-d", "--wait", "--wait-timeout", "600"], capture=False)
    return None if code == 0 else "stack"


def show(manifest):
    current = manifest.config.get("llm", "?")
    cat, files, choices = install.llm_offer()
    ui.say(ui.t("sw_now", llm=current))
    if cat is None:
        ui.status("WARN", ui.t("llm_no_catalog"))
        for number, name in enumerate(choices, start=1):
            ui.say("  %d) %s%s" % (number, name, " — " + ui.t("llm_now") if name == current else ""))
    else:
        for line in catalog.menu(cat, choices, files, mem_total_gib(), current=current):
            ui.say(line)
    ui.say("\n" + ui.t("sw_hint"))
    return 0


def llm(args):
    manifest = Manifest.load()
    if manifest is None:
        ui.status("FAIL", ui.t("not_installed", dir=paths.INSTALL_DIR))
        return 1
    if manifest.status != "installed":
        ui.status("FAIL", ui.t("sw_incomplete"))
        return 1
    if not args.name:
        return show(manifest)
    current = manifest.config.get("llm")
    cat, files, choices = install.llm_offer()
    target = catalog.pick(args.name, choices)
    if not target:
        ui.status("FAIL", ui.t("cfg_llm_bad", llm=args.name, choices=", ".join(choices)))
        return 1
    ui.open_log(os.path.join(paths.install_dir(), "install.log"))
    if target == current:
        # Also finishes a change that was interrupted after the restart.
        ui.status("OK", ui.t("sw_same", llm=target))
        return 0 if install.step_models() else 1

    ui.header(ui.t("sw_title"))
    ui.say("  " + ui.t("sw_now", llm=current))
    ui.say("  " + ui.t("sw_next", llm=target))
    e = catalog.entry(cat, "llm", target)
    if e is not None:
        ui.say("     " + catalog.facts(e, mem_total_gib()))
    for key in ("sw_plan_prepare", "sw_plan_restart", "sw_plan_ragflow", "sw_plan_rollback"):
        ui.say("  - " + ui.t(key, llm=current))
    if not ui.confirm(ui.t("sw_confirm"), default=True):
        ui.say(ui.t("cancelled"))
        return 1

    t0 = time.time()
    files_args = install.llm_files(target)
    ui.step(1, TOTAL, ui.t("sw_prepare"))
    cfg = install.compose_config(files_args)
    if cfg is None:
        ui.status("FAIL", ui.t("compose_unreadable"))
        return 1
    if not install.step_images(manifest, files_args) or not install.step_weights(cfg, manifest.config, manifest):
        ui.say(ui.t("sw_nothing_changed", llm=current))
        return 1
    manifest.save()

    ui.step(2, TOTAL, ui.t("sw_restart"))
    env = os.path.join(paths.compose_dir(), ".env")
    failed = restart_llm(env, target, manifest)
    if failed == "stack":
        ui.status("FAIL", ui.t("sw_stack_failed", llm=target))
        ui.say(ui.t("sw_back_hint", llm=current))
        return 1
    if failed == "llm":
        ui.status("FAIL", ui.t("sw_up_failed", llm=target))
        ui.status("DO", ui.t("sw_rollback", llm=current))
        if restart_llm(env, current, manifest) is None and install.step_models():
            ui.status("OK", ui.t("sw_rollback_ok", llm=current))
        else:
            ui.status("FAIL", ui.t("sw_rollback_failed", llm=current))
        return 1
    ui.status("OK", ui.t("all_healthy", min=(time.time() - t0) / 60))

    ui.step(3, TOTAL, ui.t("st_models"))
    if not install.step_models():
        ui.say(ui.t("sw_back_hint", llm=current))
        return 1
    ui.step(4, TOTAL, ui.t("st_acceptance"))
    passed = install.step_acceptance(manifest.config.get("lang", ui.lang()))
    ui.say("\n" + ui.t("sw_done", llm=target, min=(time.time() - t0) / 60))
    ui.say(ui.t("sw_back_hint", llm=current))
    return 0 if passed else 1

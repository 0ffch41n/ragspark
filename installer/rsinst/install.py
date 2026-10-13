"""`ragspark install`: every step is idempotent, so an interrupted install resumes."""
import glob
import json
import os
import shutil
import sys
import time

from . import paths, ui
from .manifest import Manifest, make_dirs
from .preflight import run_checks
from .sysutil import compose, host_address, project_containers, read_env, run, set_env

TOTAL = 11
DEFAULT_EMAIL = "admin@ragspark.local"

UNIT = """[Unit]
Description=RAGSpark (RAGFlow and vLLM on DGX Spark)
Documentation=https://github.com/0ffch41n/ragspark
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
# Starts the stack in its dependency order (compose health checks) and returns:
# a boot is not held up by the ~10 minutes the models need.
Type=exec
RemainAfterExit=yes
WorkingDirectory={compose_dir}
# Docker itself restarts, out of order, containers whose last exit code was not 0
# (restart policy on-failure; after a power loss every container). Stop those that
# are not healthy yet, so that compose starts them in order.
ExecStartPre={python} {install_dir}/installer/ragspark.py prestart
ExecStart={docker} compose up -d --wait --wait-timeout 2400
ExecStop={docker} compose stop
TimeoutStartSec=5min
TimeoutStopSec=5min

[Install]
WantedBy=multi-user.target
"""

CLI_SCRIPT = """#!/bin/sh
# RAGSpark control tool, installed by the RAGSpark installer.
exec python3 {install_dir}/installer/ragspark.py "$@"
"""

# Mode "online" downloads the files vLLM needs, skipping duplicate formats (*.bin,
# *.onnx) that some repositories carry. Mode "local" only reads the cache: the
# snapshot that refs/main points to must hold config.json and every weight file.
# huggingface_hub's own offline check is not used: it also demands auxiliary
# files (checksums, quantizer metadata) that vLLM does not read and that vLLM's
# own download skips, and would send a complete cache back to the network.
WEIGHTS_SCRIPT = r'''
import json, os, sys
repo, mode = sys.argv[1], sys.argv[2]
if mode == "online":
    from huggingface_hub import snapshot_download
    patterns = ["*.json", "*.safetensors", "*.py", "*.txt", "*.model", "*.tiktoken", "*.jinja", "tokenizer*"]
    try:
        path = snapshot_download(repo, allow_patterns=patterns)
    except Exception as e:
        print("ERROR", type(e).__name__, str(e)[:300]); sys.exit(2)
else:
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(os.environ.get("HF_HOME") or "/root/.cache/huggingface", "hub")
    base = os.path.join(hub, "models--" + repo.replace("/", "--"))
    try:
        with open(os.path.join(base, "refs", "main")) as f:
            path = os.path.join(base, "snapshots", f.read().strip())
    except OSError:
        print("MISSING refs/main"); sys.exit(3)
    if not os.path.isdir(path):
        print("MISSING snapshot", os.path.basename(path)); sys.exit(3)
missing = []
if not os.path.exists(os.path.join(path, "config.json")):
    missing.append("config.json")
index = os.path.join(path, "model.safetensors.index.json")
if os.path.exists(index):
    shards = sorted(set(json.load(open(index))["weight_map"].values()))
    missing += [s for s in shards if not os.path.exists(os.path.join(path, s))]
elif not [f for f in os.listdir(path) if f.endswith(".safetensors")]:
    missing.append("*.safetensors")
if missing:
    print("MISSING", " ".join(missing[:5])); sys.exit(3)
print("COMPLETE", path)
'''


def llm_choices():
    files = sorted(glob.glob(os.path.join(paths.SOURCE_ROOT, "compose", "llm", "*.yaml")))
    return [os.path.splitext(os.path.basename(f))[0] for f in files]


def gather_config(args, manifest):
    """Settings for this install: from the interrupted run, from flags or from questions."""
    if manifest is not None:
        saved = dict(manifest.config)
        given = {"admin_email": args.admin_email, "http_port": args.http_port, "http_bind": args.http_bind,
                 "models_dir": args.models_dir and os.path.abspath(args.models_dir), "llm": args.llm}
        ignored = [k for k, v in given.items() if v is not None and str(v) != str(saved.get(k))]
        if ignored:
            ui.status("WARN", ui.t("resume_flags_ignored", flags=", ".join(ignored)))
        return saved
    if ui.interactive():
        ui.header(ui.t("cfg_title"))
    email = args.admin_email or ui.ask(ui.t("cfg_email"), DEFAULT_EMAIL)
    while "@" not in email:
        email = ui.ask(ui.t("cfg_email_bad"), DEFAULT_EMAIL)
    port = str(args.http_port or ui.ask(ui.t("cfg_port"), "80"))
    while not (port.isdigit() and 0 < int(port) < 65536):
        port = ui.ask(ui.t("cfg_port_bad"), "80")
    models_dir = args.models_dir or ui.ask(ui.t("cfg_models_dir"), paths.DEFAULT_MODELS_DIR)
    choices = llm_choices()
    llm = args.llm or (choices[0] if len(choices) == 1 else ui.ask(ui.t("cfg_llm", choices=", ".join(choices)), choices[0]))
    if llm not in choices:
        raise SystemExit(ui.t("cfg_llm_bad", llm=llm, choices=", ".join(choices)))
    return {"lang": ui.lang(), "admin_email": email, "http_bind": args.http_bind or "0.0.0.0",
            "http_port": int(port), "models_dir": os.path.abspath(models_dir), "llm": llm}


def show_plan(config, resume):
    if resume:
        ui.say(ui.t("plan_resume"))
    for line in ("plan_files", "plan_models", "plan_service", "plan_cli", "plan_docker", "plan_downloads"):
        ui.say("  - " + ui.t(line, dir=paths.INSTALL_DIR, models=config["models_dir"], unit=paths.UNIT_PATH,
                              cli=paths.CLI_PATH))
    ui.say("  " + ui.t("plan_settings", email=config["admin_email"], port=config["http_port"], llm=config["llm"]))
    ui.say("  " + ui.t("plan_untouched"))


# ---------------------------------------------------------------- steps

def step_files(manifest):
    dst = paths.install_dir()
    if os.path.realpath(paths.SOURCE_ROOT) != os.path.realpath(dst):
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*")
        for d in paths.COPY_DIRS:
            src = os.path.join(paths.SOURCE_ROOT, d)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dst, d), dirs_exist_ok=True, ignore=ignore)
        for f in paths.COPY_FILES:
            src = os.path.join(paths.SOURCE_ROOT, f)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dst, f))
        code, out = run(["git", "-c", "safe.directory=*", "-C", paths.SOURCE_ROOT,
                         "describe", "--tags", "--always", "--dirty"])
        version = out.strip().splitlines()[-1] if code == 0 and out.strip() else "unknown"
        with open(os.path.join(dst, "VERSION"), "w") as f:
            f.write(version + "\n")
        manifest.set("version", version)
    ui.status("OK", ui.t("files_copied", dir=paths.INSTALL_DIR, version=manifest.data.get("version", "?")))
    cli = paths.host(paths.CLI_PATH)
    if os.path.exists(cli) and cli not in manifest.data["files"]:
        ui.status("WARN", ui.t("cli_exists", path=paths.CLI_PATH))
    else:
        make_dirs(os.path.dirname(cli), manifest)
        with open(cli, "w") as f:
            f.write(CLI_SCRIPT.format(install_dir=paths.INSTALL_DIR))
        os.chmod(cli, 0o755)
        manifest.add_file(cli)
        ui.status("OK", ui.t("cli_installed", path=paths.CLI_PATH))
    return True


def step_settings(config):
    env = os.path.join(paths.compose_dir(), ".env")
    if not os.path.exists(env):
        code, out = run(["bash", os.path.join(paths.compose_dir(), "init-env.sh")])
        if code != 0:
            ui.status("FAIL", ui.t("env_failed", err=out.strip()[-300:]))
            return False
        ui.status("OK", ui.t("env_created"))
    else:
        ui.status("OK", ui.t("env_kept"))
    for key, value in (("COMPOSE_FILE", "compose.yaml:llm/%s.yaml" % config["llm"]),
                       ("RAGSPARK_HTTP_BIND", config["http_bind"]),
                       ("RAGSPARK_HTTP_PORT", config["http_port"]),
                       ("RAGSPARK_HF_CACHE", config["models_dir"]),
                       ("RAGSPARK_LANG", config["lang"]),
                       ("DEFAULT_SUPERUSER_EMAIL", config["admin_email"])):
        set_env(env, key, value)
    tz = read_env(env).get("RAGSPARK_TZ", "?")
    ui.status("OK", ui.t("env_values", email=config["admin_email"], port=config["http_port"], tz=tz))
    code, out = compose(["config", "--quiet"])
    if code != 0:
        ui.status("FAIL", ui.t("compose_invalid", err=out.strip()[-400:]))
        return False
    return True


def compose_config():
    code, out = compose(["config", "--format", "json"])
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def step_images(manifest):
    code, out = compose(["config", "--images"])
    if code != 0:
        ui.status("FAIL", ui.t("compose_invalid", err=out.strip()[-300:]))
        return False
    refs = sorted(set(x.strip() for x in out.splitlines() if x.strip()))
    missing = [r for r in refs if run(["docker", "image", "inspect", r])[0] != 0]
    ui.status("OK" if not missing else "INFO", ui.t("images_state", have=len(refs) - len(missing), total=len(refs)))
    for ref in missing:
        ui.status("DO", ui.t("image_pulling", ref=ref))
        for attempt in range(1, 4):
            code, _ = run(["docker", "pull", ref], capture=False)
            if code == 0:
                manifest.add_image(ref)
                ui.status("OK", ui.t("image_pulled", ref=ref))
                break
            if attempt < 3:
                ui.status("WARN", ui.t("image_retry", n=attempt))
                time.sleep(float(os.environ.get("RAGSPARK_RETRY_DELAY", "10")) * attempt)
        else:
            ui.status("FAIL", ui.t("image_failed", ref=ref))
            return False
    return True


def vllm_image(cfg):
    return ((cfg.get("services") or {}).get("vllm-llm") or {}).get("image")


def step_gpu(cfg):
    image = vllm_image(cfg)
    if not image:
        ui.status("FAIL", ui.t("compose_unreadable"))
        return False
    code, out = run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint", "nvidia-smi", image, "-L"],
                    timeout=300)
    gpu = next((line for line in out.splitlines() if line.startswith("GPU")), "")
    if code == 0 and gpu:
        ui.status("OK", ui.t("gpu_container_ok", gpu=gpu.strip()))
        return True
    ui.status("FAIL", ui.t("gpu_container_failed", err=out.strip()[-300:]))
    return False


def model_repos(cfg):
    repos = []
    for name, svc in sorted((cfg.get("services") or {}).items()):
        cmd = svc.get("command") or []
        if name.startswith("vllm-") and "serve" in cmd and cmd.index("serve") + 1 < len(cmd):
            repos.append(cmd[cmd.index("serve") + 1])
    return repos


# Passed to the download container when set (sudo HF_TOKEN=... ./install.sh).
PASS_ENV = ["HF_TOKEN", "HF_ENDPOINT", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy"]
WEIGHTS_CONTAINER = "ragspark-weights"


def _weights(image, models_dir, repo, mode, capture=True):
    """Check (mode local) or download (mode online) one repository in a vLLM container."""
    run(["docker", "rm", "-f", WEIGHTS_CONTAINER])
    cmd = ["docker", "run", "--rm", "--name", WEIGHTS_CONTAINER] + \
        (["-t"] if not capture and sys.stdout.isatty() else []) + [
        "-v", "%s:/root/.cache/huggingface" % models_dir,
        "-e", "HF_HUB_DISABLE_TELEMETRY=1"]
    if mode == "local":
        cmd += ["-e", "HF_HUB_OFFLINE=1"]
    for var in PASS_ENV:
        if os.environ.get(var):
            cmd += ["-e", var]
    cmd += ["--entrypoint", "python3", image, "-c", WEIGHTS_SCRIPT, repo, mode]
    code, out = run(cmd, capture=capture, timeout=600 if mode == "local" else None)
    if code == 124:
        run(["docker", "rm", "-f", WEIGHTS_CONTAINER])       # the timeout killed the client only
    return code, out


def step_weights(cfg, config, manifest):
    image = vllm_image(cfg)
    repos = model_repos(cfg)
    if not image or not repos:
        ui.status("FAIL", ui.t("compose_unreadable"))
        return False
    models_dir = config["models_dir"]
    make_dirs(paths.host(models_dir), manifest)
    for repo in repos:
        cache = os.path.join(paths.host(models_dir), "hub", "models--" + repo.replace("/", "--"))
        existed = os.path.exists(cache) and repo not in manifest.data["models_downloaded"]
        code, out = _weights(image, models_dir, repo, "local")
        if code == 0:
            ui.status("OK", ui.t("weights_present", repo=repo))
            continue
        ui.status("DO", ui.t("weights_downloading", repo=repo))
        if not existed:
            manifest.add_model(repo)
        _weights(image, models_dir, repo, "online", capture=False)
        code, out = _weights(image, models_dir, repo, "local")
        if code != 0:
            ui.status("FAIL", ui.t("weights_failed", repo=repo, err=out.strip()[-300:]))
            return False
        ui.status("OK", ui.t("weights_done", repo=repo))
    return True


def step_service(manifest):
    unit = paths.host(paths.UNIT_PATH)
    docker = shutil.which("docker") or "/usr/bin/docker"
    python = shutil.which("python3") or "/usr/bin/python3"
    content = UNIT.format(compose_dir=os.path.join(paths.INSTALL_DIR, "compose"), docker=docker,
                          python=python, install_dir=paths.INSTALL_DIR)
    current = None
    if os.path.exists(unit):
        with open(unit) as f:
            current = f.read()
    if current == content and run(["systemctl", "is-enabled", paths.UNIT_NAME])[1].strip() == "enabled":
        ui.status("OK", ui.t("service_unchanged", unit=paths.UNIT_NAME))
        return True
    # A systemd reload can cut running GPU containers off the GPU when Docker
    # uses the systemd cgroup driver: stop the stack first if it is up (resume).
    if any(c["state"] == "running" for c in project_containers()):
        ui.status("DO", ui.t("service_stop_first"))
        compose(["stop"])
    make_dirs(os.path.dirname(unit), manifest)
    with open(unit, "w") as f:
        f.write(content)
    manifest.add_file(unit)
    run(["systemctl", "daemon-reload"])
    code, out = run(["systemctl", "enable", paths.UNIT_NAME])
    if code != 0:
        ui.status("FAIL", ui.t("service_failed", err=out.strip()[-300:]))
        return False
    manifest.set("unit", paths.UNIT_NAME)
    ui.status("OK", ui.t("service_ok", unit=paths.UNIT_NAME))
    return True


def step_start():
    ui.say(ui.t("start_hint"))
    t0 = time.time()
    code, _ = compose(["up", "-d", "--wait", "--wait-timeout", "2400"], capture=False)
    minutes = (time.time() - t0) / 60
    if code != 0:
        ui.status("FAIL", ui.t("start_failed"))
        return False
    run(["systemctl", "start", paths.UNIT_NAME])
    ui.status("OK", ui.t("start_ok", min=minutes))
    return True


def step_models():
    with open(os.path.join(paths.install_dir(), "tools", "ragflow_models.py")) as f:
        code, _ = compose(["exec", "-T", "ragflow", "sh", "-c",
                           "cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python -"], capture=False, stdin=f)
    if code != 0:
        ui.status("FAIL", ui.t("models_failed"))
        return False
    ui.status("OK", ui.t("models_ok"))
    return True


def step_acceptance(lang):
    src = os.path.join(paths.install_dir(), "acceptance")
    code, out = compose(["cp", src + "/.", "ragflow:/tmp/ragspark-acceptance"])
    if code != 0:
        ui.status("FAIL", ui.t("acceptance_copy_failed", err=out.strip()[-300:]))
        return False
    with open(os.path.join(paths.install_dir(), "tools", "acceptance.py")) as f:
        code, _ = compose(["exec", "-T", "ragflow", "sh", "-c",
                           "cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --lang %s" % lang],
                          capture=False, stdin=f)
    if code != 0:
        ui.status("FAIL", ui.t("acceptance_failed"))
        return False
    ui.status("OK", ui.t("acceptance_ok"))
    return True


def finish(config, manifest):
    manifest.data["status"] = "installed"
    manifest.data["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest.save()
    port = int(config["http_port"])
    url = "http://%s%s/" % (host_address(), "" if port == 80 else ":%d" % port)
    ui.header(ui.t("done_title"))
    ui.say(ui.t("done_url", url=url))
    ui.say(ui.t("done_login", email=config["admin_email"]))
    ui.say(ui.t("done_password", env=os.path.join(paths.INSTALL_DIR, "compose", ".env")))
    ui.say(ui.t("done_sample"))
    ui.say(ui.t("done_commands"))


def install(args):
    manifest = Manifest.load()
    if manifest is not None and manifest.status == "installed":
        ui.status("FAIL", ui.t("already_installed", dir=paths.INSTALL_DIR))
        return 1
    resume = manifest is not None
    config = gather_config(args, manifest)
    ui.set_lang(config["lang"])

    ui.step(1, TOTAL, ui.t("st_check"))
    report = run_checks(config, manifest, network=not args.no_network, header=False)
    if report.blockers:
        if args.force:
            ui.status("FAIL", ui.t("pf_not_forceable"))
        return 1
    if report.failed and not args.force:
        return 1
    ui.step(2, TOTAL, ui.t("st_plan"))
    show_plan(config, resume)
    if not ui.confirm(ui.t("plan_confirm"), default=True):
        ui.say(ui.t("cancelled"))
        return 1

    if manifest is None:
        manifest = Manifest()
        make_dirs(paths.install_dir(), manifest)
    manifest.data["config"] = config
    manifest.save()
    ui.open_log(os.path.join(paths.install_dir(), "install.log"))

    steps = [
        ("st_files", lambda: step_files(manifest)),
        ("st_settings", lambda: step_settings(config)),
        ("st_images", lambda: step_images(manifest)),
        ("st_gpu", lambda: step_gpu(compose_config() or {})),
        ("st_weights", lambda: step_weights(compose_config() or {}, config, manifest)),
        ("st_service", lambda: step_service(manifest)),
        ("st_start", step_start),
        ("st_models", step_models),
        ("st_acceptance", lambda: True if args.skip_acceptance else step_acceptance(config["lang"])),
    ]
    for i, (title, func) in enumerate(steps, start=3):
        ui.step(i, TOTAL, ui.t(title))
        if not func():
            ui.say("\n" + ui.t("install_failed", log=os.path.join(paths.INSTALL_DIR, "install.log")))
            return 1
    finish(config, manifest)
    return 0

"""Command line: ragspark <command> [options]."""
import argparse
import os
import sys

from . import ui
from .manifest import Manifest

COMMANDS = ["install", "check", "status", "start", "stop", "restart", "logs", "acceptance", "uninstall", "prestart"]


def _common(suppress):
    """--lang and --yes, accepted before and after the command.

    The sub-command copy uses SUPPRESS so that it does not reset a value given
    before the command; the two copies must not share action objects.
    """
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--lang", choices=["ru", "en"], default=argparse.SUPPRESS if suppress else None,
                   help="language of messages / язык сообщений")
    c.add_argument("--yes", "-y", action="store_true", default=argparse.SUPPRESS if suppress else False,
                   help="answer yes to every question")
    return c


def build_parser():
    common = _common(suppress=True)
    p = argparse.ArgumentParser(prog="ragspark", parents=[_common(suppress=False)],
                                description="RAGSpark: RAGFlow with local models on NVIDIA DGX Spark.")
    sub = p.add_subparsers(dest="command", metavar="command")

    i = sub.add_parser("install", parents=[common], help="install RAGSpark (run from the repository)")
    i.add_argument("--admin-email", help="superuser e-mail (default admin@ragspark.local)")
    i.add_argument("--http-port", type=int, help="web port (default 80)")
    i.add_argument("--http-bind", help="address for the web port (default 0.0.0.0)")
    i.add_argument("--models-dir", help="model weights directory (default /srv/ragspark/hf-cache)")
    i.add_argument("--llm", help="LLM file in compose/llm/ without .yaml")
    i.add_argument("--skip-acceptance", action="store_true", help="do not run the acceptance test")
    i.add_argument("--no-network", action="store_true", help="skip the registry latency check")
    i.add_argument("--force", action="store_true", help="continue despite failed checks")

    c = sub.add_parser("check", parents=[common], help="check the system")
    c.add_argument("--gpu-reload", action="store_true",
                   help="test whether systemctl daemon-reload takes the GPU from containers")
    c.add_argument("--http-port", type=int)
    c.add_argument("--models-dir")
    c.add_argument("--no-network", action="store_true")

    sub.add_parser("status", parents=[common], help="state of the installed stack")
    sub.add_parser("start", parents=[common], help="start the stack and wait until it is ready")
    sub.add_parser("stop", parents=[common], help="stop the stack (data is kept)")
    sub.add_parser("restart", parents=[common], help="stop and start")
    lg = sub.add_parser("logs", parents=[common], help="follow container logs")
    lg.add_argument("service", nargs="?", help="ragflow, vllm-llm, vllm-embed, vllm-rerank, es01, mysql, ...")
    lg.add_argument("--tail", type=int, default=100)
    sub.add_parser("acceptance", parents=[common], help="run the acceptance test again")
    sub.add_parser("prestart", parents=[common])          # used by the systemd unit
    u = sub.add_parser("uninstall", parents=[common], help="remove RAGSpark and all its data")
    u.add_argument("--keep-images", action="store_true", help="keep the Docker images the installer pulled")
    u.add_argument("--keep-models", action="store_true", help="keep the model weights the installer downloaded")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    manifest = Manifest.load()
    lang = args.lang or (manifest.config.get("lang") if manifest else None) or ui.detect_lang()
    ui.set_lang(lang)
    ui.set_yes(args.yes)
    if os.geteuid() != 0 and os.environ.get("RAGSPARK_ALLOW_NONROOT") != "1":
        tool = "ragspark" if manifest is not None else "./install.sh"
        print(ui.t("need_root", cmd="sudo " + " ".join([tool] + sys.argv[1:])), file=sys.stderr)
        return 1
    if args.command == "install" and not args.lang and manifest is None:
        choice = ui.ask("Language / Язык (ru, en)", lang)
        ui.set_lang(choice if choice in ("ru", "en") else lang)

    from . import control, install, uninstall
    handlers = {
        "install": install.install, "uninstall": uninstall.uninstall,
        "check": control.check, "status": control.status, "start": control.start,
        "stop": control.stop, "restart": control.restart, "logs": control.logs,
        "acceptance": control.acceptance, "prestart": control.prestart,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\n" + ui.t("interrupted"), file=sys.stderr)
        return 130

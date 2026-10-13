"""Terminal output in English or Russian, prompts and the install log."""
import os
import sys
import time

from .i18n import MESSAGES

_state = {"lang": "en", "yes": False, "log": None, "pending": []}

_COLORS = {"OK": "32", "WARN": "33", "FAIL": "31", "INFO": "36", "SKIP": "90", "DO": "34"}
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def detect_lang():
    """Language from the environment: Russian locales get Russian."""
    for var in ("RAGSPARK_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    return "en"


def set_lang(lang):
    _state["lang"] = "ru" if lang == "ru" else "en"


def lang():
    return _state["lang"]


def set_yes(value):
    _state["yes"] = bool(value)


def t(key, **kw):
    """The message `key` in the current language, formatted with `kw`."""
    en, ru = MESSAGES[key]
    text = ru if _state["lang"] == "ru" else en
    return text.format(**kw) if kw else text


def open_log(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _state["log"] = open(path, "a", encoding="utf-8")
    os.chmod(path, 0o600)
    log("=" * 60)
    log("started " + time.strftime("%Y-%m-%d %H:%M:%S"))
    for line in _state["pending"]:
        log(line)
    _state["pending"] = []


def log(text):
    """Append to the install log; lines before it is opened are kept and written then."""
    if _state["log"]:
        _state["log"].write(text.rstrip("\n") + "\n")
        _state["log"].flush()
    else:
        _state["pending"].append(text)


def _paint(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if _TTY else text


def say(text=""):
    print(text, flush=True)
    log(text)


def status(kind, text):
    """One result line: [ OK ] text."""
    tag = "[%s]" % kind.center(4)
    print("  " + _paint(_COLORS.get(kind, "0"), tag) + " " + text, flush=True)
    log("  %s %s" % (tag, text))


def header(text):
    line = "== " + text + " =="
    print("\n" + _paint("1", line), flush=True)
    log(line)


def step(number, total, text):
    header("%d/%d  %s" % (number, total, text))


def interactive():
    """Questions are asked only with a terminal and without --yes."""
    return not _state["yes"] and sys.stdin.isatty()


def ask(prompt, default):
    """A question with a default; --yes or no terminal takes the default."""
    if _state["yes"] or not sys.stdin.isatty():
        log("%s [%s] -> %s (default)" % (prompt, default, default))
        return default
    answer = input("%s [%s]: " % (prompt, default)).strip()
    log("%s [%s] -> %s" % (prompt, default, answer or default))
    return answer or default


def confirm(prompt, default=False):
    """Yes/no; --yes answers yes. Without a terminal and without --yes: no."""
    if _state["yes"]:
        log(prompt + " -> yes (--yes)")
        return True
    if not sys.stdin.isatty():
        log(prompt + " -> no (no terminal)")
        say(t("need_yes"))
        return False
    hint = t("yes_no_default_yes") if default else t("yes_no_default_no")
    answer = input("%s %s: " % (prompt, hint)).strip().lower()
    log("%s -> %s" % (prompt, answer))
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")

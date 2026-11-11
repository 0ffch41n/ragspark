"""The model catalog (catalog/models.yaml) as the installer shows it (D9).

PyYAML comes with DGX OS (python3-yaml: netplan, cloud-init and
ubuntu-pro-client depend on it). Without it, or with an unreadable catalog,
load() returns None and the installer falls back to a plain list of the LLM
files, the catalog's default first.
"""
import os

from . import paths, ui


def load():
    """The parsed catalog, or None."""
    try:
        import yaml
    except ImportError:
        return None
    try:
        with open(os.path.join(paths.SOURCE_ROOT, "catalog", "models.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _entries(cat, kind):
    return [e for e in ((cat or {}).get("models") or {}).get(kind) or [] if isinstance(e, dict)]


def entry(cat, kind, model_id):
    """The catalog record `model_id` of `kind` (llm, embedding, rerank), or None."""
    for e in _entries(cat, kind):
        if e.get("id") == model_id:
            return e
    return None


def offered(cat, files, mode="single"):
    """The LLM files offered for `mode`, in the order given. A file without a
    catalog record is offered; a record whose modes lack `mode` is not."""
    out = []
    for name in files:
        e = entry(cat, "llm", name)
        if e is None or mode in (e.get("modes") or [mode]):
            out.append(name)
    return out


def not_ready(cat, files):
    """Catalog LLMs that have no file in compose/llm/ yet."""
    return [e["id"] for e in _entries(cat, "llm") if e.get("id") and e["id"] not in files]


def license_short(e):
    """The license as the menu and the plan show it: the part before " (" of the record."""
    short = str((e or {}).get("license") or "").split(" (")[0].strip()
    return short if short and short != "TBD" else ui.t("llm_license_unknown")


def _context(tokens):
    return "%dk" % (tokens // 1024) if tokens % 1024 == 0 else str(tokens)


def facts(e, total_gib):
    """Speed, context, memory, features, license and status of one LLM record."""
    parts = []
    tok = (e.get("measured") or {}).get("decode_tok_s_single")
    if isinstance(tok, (int, float)):
        parts.append(ui.t("llm_speed", tok=tok))
    ctx = (e.get("context") or {}).get("default")
    if isinstance(ctx, int):
        parts.append(ui.t("llm_context", ctx=_context(ctx)))
    share = e.get("memory_share")
    if isinstance(share, (int, float)) and total_gib:
        parts.append(ui.t("llm_memory", gib=share * total_gib))
    nodes = e.get("nodes")
    if isinstance(nodes, int) and nodes > 1:
        parts.append(ui.t("llm_nodes", n=nodes, gib=e.get("memory_gib_per_node", "?")))
    features = [ui.t(key) for key, field in (("llm_tools", "tool_calling"), ("llm_vision", "vision"))
                if e.get(field) is True]
    if features:
        parts.append(", ".join(features))
    parts.append(license_short(e))
    parts.append(ui.t("llm_validated") if e.get("validated") is True else ui.t("llm_experimental"))
    return " · ".join(parts)


def menu(cat, choices, files, total_gib, current=None):
    """Menu lines: every offered LLM with its facts and summary, choices[0] marked as the
    default (or `current` as the running one), then the catalog LLMs that cannot be installed yet."""
    marks = {current: ui.t("llm_now")} if current else {choices[0]: ui.t("llm_default")} if choices else {}
    lines = [ui.t("llm_menu_title")]
    for number, name in enumerate(choices, start=1):
        lines.append("  %d) %s%s" % (number, name, " — " + marks[name] if name in marks else ""))
        e = entry(cat, "llm", name)
        if e is None:
            lines.append("     " + ui.t("llm_no_record"))
            continue
        lines.append("     " + facts(e, total_gib))
        summary = (e.get("summary") or {}).get(ui.lang())
        if summary:
            lines.append("     " + str(summary).strip())
    rest = not_ready(cat, files)
    if rest:
        lines.append("  " + ui.t("llm_not_ready", ids=", ".join(rest)))
    return lines


def pick(answer, choices):
    """A menu answer: a number from the menu or a model name; None for anything else."""
    answer = (answer or "").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        return choices[int(answer) - 1]
    return answer if answer in choices else None


def plan_lines(cat, llm):
    """What the install downloads and under which license, shown before any download."""
    if cat is None:
        return [ui.t("plan_licenses_unknown")]
    defaults = cat.get("defaults") or {}
    rows = (("role_llm", entry(cat, "llm", llm)),
            ("role_embedding", entry(cat, "embedding", defaults.get("embedding"))),
            ("role_rerank", entry(cat, "rerank", defaults.get("rerank"))))
    lines = [ui.t("plan_licenses")]
    for role, e in rows:
        if e is not None:
            lines.append("  " + ui.t("plan_license", role=ui.t(role), repo=e.get("hf_repo") or e.get("id"),
                                     license=license_short(e)))
    return lines

"""Stack mode (D10, stage 5): an LLM split across two DGX Spark units on a direct QSFP cable.

The head keeps RAGFlow and runs the first half of the LLM; the node (`install --role node`) runs the
other half, the embedding model and the reranker (compose/stack.yaml, compose/node/). An LLM is a
stack-mode LLM when it has a node file: compose/node/llm/<id>.yaml. This needs no catalog, so it
also holds where PyYAML is missing.
"""
import json
import os
import urllib.request

from . import paths

DEFAULT_HEAD_IP = "192.168.100.10"      # the two ends of the cable, as NVIDIA's two-Spark playbook sets them
DEFAULT_NODE_IP = "192.168.100.11"
NODE_PORTS = (("role_embedding", 8001), ("role_rerank", 8002))   # compose/node/compose.yaml; message keys
RANK = "vllm-llm-rank"                  # the service of the LLM half on either unit


def is_stack(llm):
    return bool(llm) and os.path.exists(os.path.join(paths.SOURCE_ROOT, "compose", "node", "llm", llm + ".yaml"))


def compose_file(llm, role="head"):
    """COMPOSE_FILE of .env for `llm` (relative to the unit's compose directory)."""
    if role == "node":
        return "compose.yaml:llm/%s.yaml" % llm
    if is_stack(llm):
        return "compose.yaml:stack.yaml:llm/%s.yaml" % llm
    return "compose.yaml:llm/%s.yaml" % llm


def compose_args(llm, role="head"):
    """The same files as `-f` options: compose with them instead of the ones .env names."""
    args = []
    for f in compose_file(llm, role).split(":"):
        args += ["-f", f]
    return args


def addresses(config):
    """(head address, node address) of the cable from the install settings."""
    return config.get("head_ip") or DEFAULT_HEAD_IP, config.get("node_ip") or DEFAULT_NODE_IP


def http_ok(url, body=None, timeout=5):
    """True when `url` answers 2xx (a JSON POST when `body` is given)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:      # refused, timed out, HTTP error: all mean "not answering"
        return False

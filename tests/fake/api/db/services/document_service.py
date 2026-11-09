"""Stand-in for RAGFlow's DocumentService in tests: a document's whole record, raptor included,
from tests/mock_providers.py (which, like RAGFlow's API, hides raptor everywhere else)."""
import json
import os
import urllib.request


class _Document:
    def __init__(self, record):
        self.id = record["id"]
        self.parser_config = record["parser_config"]


class DocumentService:
    @classmethod
    def get_by_id(cls, doc_id):
        with urllib.request.urlopen(os.environ["RAGFLOW_API"] + "/mock/documents/" + doc_id, timeout=10) as r:
            record = json.loads(r.read() or b"null")
        return (True, _Document(record)) if record else (False, None)

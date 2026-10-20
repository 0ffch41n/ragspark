#!/usr/bin/env python3
"""Let vector search find chunks that share no word with the question.

RAGFlow 0.27.2 sends Elasticsearch a full-text query and a kNN query, and
passes the full-text query to kNN as its filter as well:

    filter=bool_query.to_dict(),  # filter=_build_knn_filter_query(bool_query, vector_similarity_weight),

So vector search only ranks chunks that already match the question's words.
RAGFlow does not reduce Russian words to a common form, and with different
word forms ("плановые работы" against "плановое обслуживание ... работах")
nothing matches: retrieval returns no candidates at all, even at threshold 0,
and the embedding and rerank models never see the right chunk.

RAGFlow already has the function that drops the full-text clause from the kNN
filter when the vector side dominates the fusion (it does for Elasticsearch:
weights "0.001,1"), only the call is commented out. This patch makes that call.
Other filters (datasets, documents, availability) stay on the kNN query, and
RAGFlow's own scoring, reranker and similarity threshold still decide what is
returned.

Usage: patch_knn_filter.py <ragflow source root>     (idempotent)
"""
import os
import sys

MARK = "# ragspark: kNN without the full-text filter"
FILE = os.path.join("rag", "utils", "es_conn.py")
OLD = ("                    filter=bool_query.to_dict(),  "
       "# filter=_build_knn_filter_query(bool_query, vector_similarity_weight),\n")
NEW = ("                    filter=_build_knn_filter_query(bool_query, vector_similarity_weight),  %s\n" % MARK)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = os.path.join(sys.argv[1], FILE)
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("patch_knn_filter: already applied")
        return
    if src.count(OLD) != 1 or "def _build_knn_filter_query(" not in src:
        sys.exit("patch_knn_filter: expected code not found exactly once in %s - RAGFlow version changed?" % path)
    open(path, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("patch_knn_filter: vector search no longer requires a word match")


if __name__ == "__main__":
    main()

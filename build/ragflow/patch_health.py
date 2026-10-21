#!/usr/bin/env python3
"""The health check returns its database connection to the pool.

Found on RAGFlow 0.27.2 under RAGSpark's compose, which checks the container's
health every 15 s through /api/v1/system/healthz; the measurements are in
RAGSpark's docs/VALIDATION.md.

`check_db` in api/utils/health_utils.py probes the database with
`DB.execute_sql("SELECT 1")` outside `connection_context`, so the worker thread
that serves the request checks a connection out of the pool and never returns
it. After every request RAGFlow calls `close_connection()`, which for MySQL is
`DB.close_stale(age=30)`: peewee then closes every connection checked out more
than 30 s ago - including the one that thread still holds. The next probe on it
fails with InterfaceError (0, ''), `RetryingPooledMySQLDatabase` reconnects
after 1 s and the probe passes, leaving "Database connection issue (attempt
1/5): (0, '')" in ragflow_server.log: every third check, 3 x 15 + 1 = 46 s,
about 1900 times a day. Harmless by itself, but a real outage starts with the
same line.
The probe now runs inside `connection_context`, which returns the connection
to the pool as soon as it is done, so it is never held long enough for
`close_stale` to close it. The rest of the health check is unchanged.

Usage: patch_health.py <ragflow source root>     (idempotent; checks the
anchor before it writes anything)
"""
import os
import sys

HEALTH = os.path.join("api", "utils", "health_utils.py")

PROBE_OLD = """        # lightweight probe; works for MySQL/Postgres
        DB.execute_sql("SELECT 1")
"""
PROBE_NEW = """        # lightweight probe; works for MySQL/Postgres
        # ragspark: the probe returns its connection to the pool - see build/ragflow/patch_health.py
        # in RAGSpark. Held outside connection_context, the connection was closed under the
        # worker thread by close_stale(age=30) after a later request, and every third check failed.
        with DB.connection_context():
            DB.execute_sql("SELECT 1")
"""

EDITS = [
    ("the health check returns its connection to the pool", HEALTH,
     "# ragspark: the probe returns its connection to the pool", [
         (PROBE_OLD, PROBE_NEW),
     ]),
]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    texts, todo = {}, []
    for name, rel, mark, subs in EDITS:
        path = os.path.join(root, rel)
        if path not in texts:
            texts[path] = open(path, encoding="utf-8").read()
        if mark in texts[path]:
            print("patch_health: %s - already applied" % name)
            continue
        for old, _ in subs:
            if texts[path].count(old) != 1:
                sys.exit("patch_health: %s - expected code not found exactly once in %s; "
                         "RAGFlow version changed? Nothing was written." % (name, path))
        todo.append((name, path, subs))
    for name, path, subs in todo:
        for old, new in subs:
            texts[path] = texts[path].replace(old, new)
        print("patch_health: %s" % name)
    for path in {p for _, p, _ in todo}:
        with open(path, "w", encoding="utf-8") as f:
            f.write(texts[path])


if __name__ == "__main__":
    main()

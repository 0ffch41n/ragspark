#!/usr/bin/env python3
"""pin_images.py - pin the images in compose files to the digests of local images.

  sudo python3 tools/pin_images.py compose/compose.yaml compose/llm/*.yaml
  sudo python3 tools/pin_images.py --check compose/compose.yaml compose/llm/*.yaml

For every `image:` line:
  name:tag             -> rewritten to name:tag@sha256:... using the registry
                          digest of the local image (pull it first);
                          with --check only reported
  name:tag@sha256:...  -> compared with the local image, if there is one
  ${VARIABLE}          -> skipped

Run it after changing a tag and pulling the new image. Exit code 1 means
something is unpinned (--check), missing locally or does not match.
"""
import json, re, subprocess, sys

LINE = re.compile(r"^(\s*image:\s*)(\S+)(.*)$")


def repo_of(ref):
    """Repository part, normalised: docker.io/library/x -> x."""
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:                      # strip the tag, keep a registry port
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    for prefix in ("docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.startswith("library/"):
        name = name[len("library/"):]
    return name


def local_digests(ref):
    """Registry digests recorded for a local image, or None if it is not present."""
    r = subprocess.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    repo = repo_of(ref)
    out = []
    for item in json.loads(r.stdout.strip() or "[]") or []:
        if "@" in item and repo_of(item) == repo:
            out.append(item.split("@", 1)[1])
    return out


def main():
    args = sys.argv[1:]
    check = "--check" in args
    files = [a for a in args if a != "--check"]
    if not files:
        sys.exit(__doc__)
    problems = 0
    for path in files:
        with open(path) as f:
            lines = f.readlines()
        changed = False
        for i, line in enumerate(lines):
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            head, ref, tail = m.groups()
            if "${" in ref:
                print("[SKIP] %s: %s (variable)" % (path, ref))
                continue
            if "@" in ref:
                tagged, digest = ref.split("@", 1)
                if local_digests(repo_of(tagged) + "@" + digest) is not None:
                    note = ""
                    by_tag = local_digests(tagged)
                    if by_tag and digest not in by_tag:
                        note = " (the local tag now points to %s)" % by_tag[0][:19]
                    print("[OK]   %s: %s%s" % (path, ref, note))
                else:
                    print("[OK]   %s: %s (not pulled here)" % (path, ref))
                continue
            digests = local_digests(ref)
            if not digests:
                print("[FAIL] %s: %s - not present locally; docker pull %s" % (path, ref, ref))
                problems += 1
                continue
            if check:
                print("[TODO] %s: %s -> @%s" % (path, ref, digests[0]))
                problems += 1
                continue
            lines[i] = "%s%s@%s%s\n" % (head, ref, digests[0], tail)
            changed = True
            print("[SET]  %s: %s@%s" % (path, ref, digests[0]))
        if changed:
            with open(path, "w") as f:
                f.writelines(lines)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()

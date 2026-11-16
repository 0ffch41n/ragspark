# Building a format-1 release

Run these publisher tools as an ordinary user in a reviewed source checkout.
They require Python 3.10+, PyYAML and Git. Profile resolution and builds also
require Docker Compose with JSON config output. The comparison command works
without Git. The installed CLI and the production stack play no part in a build.
[Release format](../../docs/UPDATES.md) · [Русский](README.ru.md)

## Read the pins

Use a full commit ID. The tool reads Git blobs, even if the checkout contains
local edits or an ignored .env. It renders the selected role's Compose configuration
in a private temporary tree with placeholder values from the committed env.example.
For example, to inspect the stack profile in the first verification commit:

```sh
python3 build/release/package.py pins --commit 06a01e9dea81402cfbb4366fff009d478cc65042 --llm deepseek-v4-flash-vision --mode stack
```

The output maps every service to its pinned registry image, including the head
relays. Weights are listed per role with their repository and full revision.
Docker Hub shorthand is expanded to an explicit registry/repository.
Use --compose-bin for a standalone Compose binary.

## Prepare the inputs

Provide a recipe JSON with these fields. Values for host requirements and tested
transitions come from the publisher's validation of that release.

| Field | Content |
|---|---|
| version, sequence, created_at | Immutable target version, release number and fixed UTC timestamp |
| code_urls | HTTPS locations of the reproducible code archive, or an empty list for local delivery |
| profiles | List of objects with id, llm, mode and requirements. The requirements map has one entry per role, with python_min, docker_min, compose_min, driver_min and min_total_ram_bytes |
| transitions | Tested source, target profile, impact, restart list per role, backup, migration and rollback as specified in the release contract |

Provide a dependencies JSON array of format-1 image/model artifact records and a
local store containing their archives under each record's payload path. The record
includes the pinned image reference and config digest, or the pinned model revision
and file inventory. The publisher supplies these records from its artifact preparation
pipeline. For each selected profile the builder joins the records to the rendered
pins by identity and verifies the selected archive bytes. For model archives it
also verifies the file inventory. The image config/layer mapping still requires
independent publisher evidence and the offline import validation planned for U5.
Validation of a recipe cannot establish whether an application migrates real data.

## Build and sign

```sh
python3 build/release/package.py build --commit "$COMMIT" --recipe recipe.json --dependencies dependencies.json --store ./artifacts --output ./release-out
```

Use a new output directory. The result is release.json and payload/code.tar.gz.
The code archive uses sorted paths, fixed metadata and Git executable bits.
Rebuilding with the same inputs and compression toolchain gives identical bytes.
The output appears only after the manifest and selected artifacts pass verification.
The dependency archives stay in the store for use in offline packaging.

Review the manifest before signing on the publisher's signing machine. Use the
publisher key dedicated to releases, and distribute its public key independently.

```sh
ssh-keygen -Y sign -f "$RELEASE_KEY" -n ragspark-release-v1 release-out/release.json
```

This creates release.json.sig alongside the manifest. Publishing the release
and the separately signed stable-channel pointer is a subsequent operation.

## Check an existing installation

A legacy baseline consists of the reviewed commit and the SHA256 of its canonical
file inventory. The reference for e09da62 covers 45 files. This is a code identity
reference, not evidence that an upgrade from it is compatible. Run comparison
with access to VERSION, manifest.json and release-owned files.

```sh
python3 build/release/package.py compare --installed /opt/ragspark --baseline build/release/baselines/e09da62.json --commit e09da628575d0245ed3afe2524e19ce34b7dfc97 --inventory-sha256 ed1c6ea18e863e84d284f4113402118b1e27dead64943f8445cf366628d15bba
```

Exit 0 means that release-owned files, modes and the two version markers agree.
Exit 3 reports a mismatch or inability to check. The report lists missing,
unexpected and changed paths inside managed code directories. Runtime env and
bytecode cache directories are excluded. A stale manifest version is reported
even when all code files match. Comparison is read-only and requires a stable
installation during the scan. A later updater must repeat it under its lock.

To review another source, use **baseline --commit FULL_COMMIT --files**. Keep its
commit and inventory hash in a trusted record after review. Supplying the hash
from an untrusted inventory alongside that same inventory does not establish trust.

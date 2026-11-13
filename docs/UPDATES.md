# Update release contract — U1, format 1

**Status: proposed specification, not an implemented feature.** The commands
below are not available in the current installer. U2 implements local updates,
U3–U4 pairing and coordinated updates, U5 offline delivery, U6 public release.
This document defines their shared contract. See [INSTALL.md](INSTALL.md) for
commands available today. A signed package is trusted publisher code with root
privileges; signing establishes its origin, not its correctness.

## 1. User interface

| Proposed command | Contract |
|---|---|
| `sudo ragspark update` | inspect the stable release, present changes and downtime, ask once, prepare and apply |
| `sudo ragspark update --check` | report availability/compatibility; no service or installation changes |
| `sudo ragspark update --from /path/to/bundle` | same plan from a local directory; no external network access |
| `sudo ragspark update --resume` | inspect the durable operation journal and resume safely |
| `sudo ragspark update --rollback` | offer the previous compatible release; explicit confirmation |

`--from` and `--check` may be combined. `--resume` and `--rollback` select an
existing operation and cannot select a new source. Same installed version and
manifest hash means “already installed”; same version with a different hash is
an error. Normal update never downgrades, changes the chosen LLM or enables a
previously disabled timer. No automatic installation of updates. Head coordinates
stack updates; node directs the user to head. Existing RU/EN language selection
is retained. `--yes`, if supplied, skips confirmation only, never safety checks.

The plan shows current/target version, affected units and services, download and
peak disk requirements, estimated web/model interruption (or “not measured”),
backup requirement and rollback support. Check mode may cache verified metadata
and its monotonic sequence, but does not change installed files, configuration,
models, containers or timers. It never caches passwords or documents. Download failure does not stop working services.

Exit codes: 0 success/no update (or a compatible check); 2 invalid arguments;
3 incompatible/untrusted/incomplete input; 4 busy; 5 preparation or connectivity
failure; 6 update failed, previous state recovered; 7 recovery required;
8 user cancelled. A transport failure must not be presented as “no updates”.

## 2. Versions and discovery

Release names use the stable `MAJOR.MINOR.PATCH` subset of
[SemVer 2.0.0](https://semver.org/); no leading zeros, prereleases or build suffixes
in the format-1 stable channel. Tags are `vVERSION`. Published contents are
immutable; rebuilds with different bytes need a new version. SemVer describes
public CLI/configuration compatibility, not database rollback safety. Every
transition still needs explicit compatibility evidence.

Online discovery uses `stable.json` and detached `stable.json.sig` at the
publisher endpoint configured in the trusted bootstrap. A Git branch, an unsigned
GitHub “latest” response or a redirect cannot establish trust. Hosting may change
without changing the signature contract. The exact public endpoint and first
version are chosen before publishing U6, not invented by this specification.

`stable.json` is UTF-8 JSON with exactly these fields:

| Field | Type / constraint |
|---|---|
| `format`, `product`, `channel` | integer 1, string `ragspark`, string `stable` |
| `sequence` | positive monotonically increasing integer for channel metadata |
| `issued_at`, `expires_at` | UTC `YYYY-MM-DDTHH:MM:SSZ`; lifetime at most 14 days |
| `version`, `release_sequence` | stable version; positive release sequence |
| `manifest_url`, `signature_url` | HTTPS locations for the release metadata |
| `manifest_bytes`, `manifest_sha256` | bounded length and lowercase SHA256 of the exact release.json bytes |

The publisher refreshes this signed pointer when needed even without a release.
Reject expired/future-issued metadata (5-minute clock tolerance), a lower channel
sequence than previously accepted, and an equal sequence with different bytes.
A newer pointer cannot select a lower release sequence than previously observed.
Normal offline updates also enforce the persisted release-sequence floor; an
explicit compatible rollback is the only downgrade path and never lowers that floor.
Persist accepted sequence/hash in a root-only security cache; an incorrect clock
produces an actionable error, never automatic clock modification. Expiration
limits a stale pointer but cannot prove that a newer unpublished/unseen release
exists. Offline delivery cannot discover new revocations while disconnected.

## 3. Package contents and manifest

The local bundle is a directory, not a self-executing installer:

```text
release.json
release.json.sig
payload/code.tar.gz
payload/images/<id>.tar       # present for offline delivery when required
payload/models/<id>.tar       # present for offline delivery when required
```

Online and offline use identical signed release bytes. Offline packaging adds
artifacts already described by that manifest; it does not rewrite or re-sign it.
Code-only online delivery may omit dependency archives if they are obtainable
from the signed artifact URLs. For the selected profile, every missing local
dependency in offline mode is an error before stopping services. Already verified
installed images/model files satisfy a dependency without keeping another archive.
Otherwise its archive must be present locally. Local cable SSH
between paired units is allowed; external network requests are forbidden.

JSON uses UTF-8, no BOM, duplicate keys, NaN or Infinity. Sign and hash exact bytes;
do not re-serialize before verification. Publishers emit sorted keys, two-space
indentation, LF and a final newline. Unknown fields or enum values are rejected
in format 1. Integers are bounded to 0..2^63-1; booleans are not integers.

Required `release.json` fields:

| Field | Type / meaning |
|---|---|
| `format`, `product` | 1, `ragspark` |
| `version`, `sequence` | stable version; unique increasing release sequence |
| `source_commit`, `created_at` | full 40-character lowercase Git commit; UTC timestamp |
| `updater_protocol`, `pair_protocol`, `installation_schema` | 1, 1, 1 for the initial implementation |
| `target` | object: `os: linux`, `arch: aarch64`, `gpu: GB10` |
| `artifacts` | list of artifact records below; unique IDs and paths |
| `profiles` | nonempty list of model/mode profiles below; unique IDs |
| `transitions` | nonempty list of tested source-to-profile transitions below |

Artifact common fields: `id` (lowercase letters/digits/hyphens, 1..80 characters),
`kind` (`code`, `image`, `model`), `path` under `payload/`, `bytes`, `sha256`,
`expanded_bytes`, `urls` (HTTPS list, may be empty for offline-only delivery).
SHA256 fields are 64 lowercase hex digits. File counts/sizes must agree with
archive contents. Zero-length ordinary files are allowed; archives are nonempty.

- **code:** exactly one per release, gzip tar. Additional `files` array contains
  `path`, `bytes`, `sha256`, `mode` (`0644` or `0755`) for every ordinary file.
  Installed paths are restricted to `installer/`, `tools/`, `catalog/`,
  `acceptance/`, `compose/`, `LICENSE`, `README.md`, `README.ru.md`. Generated
  VERSION, state, `.env`, secrets, locks and logs are never payload files.
- **model:** uncompressed tar of regular snapshot files; additional `repo`, full
  40-hex `revision`, and `files` as above. Portable snapshots materialize their
  contents instead of shipping HF-cache symlinks. No unpinned model download.
- **image:** Docker-save tar; additional `image_ref` (registry/repository pinned
  by `@sha256:...`), `config_digest` (`sha256:...`), `platform: linux/arm64`.
  Verify archive hash before import and loaded content identity before use. A
  Docker-save archive's checksum or tag alone does not establish its registry
  manifest digest. The online build must derive and verify their relationship;
  the offline resolver must prove the imported config/layers correspond and that
  the resulting Compose plan uses that image without a pull. U5 must test this
  on supported Docker/Compose versions before offline support is advertised.

Each profile has `id`, `mode` (`single` or `stack`), `llm` (existing catalog ID),
and `roles`. `roles` has exactly `head` for single, `head` and `node` for stack.
Each role contains `requirements`, `images` (Compose service name -> image artifact ID) and
`models` (logical `llm`, `embedding`, `rerank` -> model artifact ID, as applicable).
`requirements` contains `python_min`, `docker_min`, `compose_min`, `driver_min`
(each an array of three nonnegative integers) and `min_total_ram_bytes`. Compare
numeric version components, not strings; report unmet host prerequisites without
installing OS/driver packages automatically. Available-memory and peak-disk checks
are additional runtime checks, not satisfied merely by total RAM.
These maps cover the effective Compose configuration for that role, including
relays; all referenced artifacts must exist and have the matching kind. Code
is required on both roles. The publisher derives the maps from rendered Compose
and verifies them against the catalog. Remote-code files used by a model are
included in its pinned snapshot, not fetched later. Adding support for a model
in a release does not select it on existing installations.

Each transition contains exactly:

| Field | Constraint |
|---|---|
| `source` | either `{version, manifest_sha256}` or `{legacy_commit, inventory_sha256}` |
| `profile` | target profile ID, preserving the installed mode and model |
| `impact` | `code`, `services`, or `systemd` |
| `restart` | object with role -> list of affected Compose services; empty lists for code-only |
| `backup` | `none` or `required`; services/systemd transitions require it in v1 |
| `migration` | only `none` in format 1 |
| `rollback` | only `compatible` in format 1 |

No version ranges or “any older version” in v1. At most one transition matches a
source/profile; ambiguity fails. Full validation compares the effective old/new
configuration: metadata cannot label an image, model or unit change “code-only”.
Systemd changes override ordinary service-restart planning with the tested
GPU-safe maintenance procedure. Transition tests must establish that both old
and new code can use the same data. `migration: none` is an assertion that must
be tested, including application startup; it cannot suppress an upstream migration.

The legacy inventory hashes the JSON array sorted by relative path of `{path, bytes, sha256, mode}`
for the exact administrator-reviewed installed code baseline (same serialization
as above). Exclude state, generated VERSION and secrets. The bootstrap recognizes
only recorded baselines, records the full source commit and reports any mismatch.
A short VERSION value or matching Git checkout alone is insufficient evidence.

## 4. Trust and verification

Use Ed25519 file signatures through OpenSSH `ssh-keygen -Y sign/verify`.
Signer identity is `ragspark-release`; namespaces are `ragspark-release-v1` for
release metadata and `ragspark-channel-v1` for the channel. The separately trusted
allowed-signers file restricts both namespaces. Verification uses an explicit
root-owned trust file outside the payload, e.g.
`/var/lib/ragspark-update/trust/allowed_signers`, and an explicit revocation file
when present. Do not use `check-novalidate`, trust a key supplied by the bundle,
or reuse the backup/administration key. The private publisher key is never
installed on either Spark.

Verifier invocation (illustrative, this is not an update command):

```bash
ssh-keygen -Y verify -f /var/lib/ragspark-update/trust/allowed_signers -I ragspark-release -n ragspark-release-v1 -s release.json.sig < release.json
```

Fresh installs and the one-time legacy bootstrap establish the public-key
fingerprint through a trusted distribution/administrator procedure. “Download a
key next to the archive” is not verification. Version 1 requires explicit
administrator replacement/revocation of trust anchors; automatic key rotation
and automatic trust-store replacement by a payload are excluded. Loss or
compromise of the signing key requires a separately authenticated recovery.

Order: bound metadata reads -> verify channel if online -> verify release
signature -> parse/validate release -> check source/profile/versions -> prepare
artifacts -> verify bytes/hashes -> safely inspect/extract/import -> recheck
both units -> confirm readiness for the already approved plan -> apply. Never
execute code from a candidate release to decide whether its signature is valid.
Publisher metadata contains no shell commands or arbitrary migration hooks.

Limits: stable pointer 64 KiB, signature 16 KiB, release metadata 16 MiB; code
archive expanded size 1 GiB and 20,000 files maximum. Model size is bounded by
signed totals and verified free space, streamed with limits rather than loaded
into RAM. Reject absolute paths, `..`, empty or dot components, backslashes,
NUL/control characters, duplicate paths, path collisions and escapes through
existing symlinks. Code/model tar permits only regular files/directories, no
links, devices, sparse files, setuid bits or special owners. Directories are
created by the extractor; code owned by root with normalized modes. Image layer
semantics are handled by Docker after archive validation, not extracted into
the host filesystem by the updater. JSON and tar readers must have local bounds
that an artifact cannot increase. HTTPS redirects remain HTTPS, with bounded retries
and total download size; candidate paths are copied into root-only staging rather
than trusted in place. A local source can change, so verify the staged bytes used
for application, not an earlier read from a user-writable bundle.

## 5. Compatibility and recovery matrix

| Situation | Required behavior |
|---|---|
| Already at the exact version/hash | no changes |
| Valid exact transition, code-only | preserve data/configuration; no container restart |
| Compatible service/image change | prefetch, backup, planned restart and functional checks |
| Units change | planned GPU-safe maintenance, not a hidden daemon-reload |
| Model ID/mode changes, unknown migration, local code edits, unknown baseline | refuse and explain before stopping |
| One stack unit missing/not ready | no application of the update on either unit |
| Wrong key/hash/platform/protocol, truncated bundle, insufficient disk | refuse before stopping |
| Link/process loss during commit | durable state on both units; no false success; resume/recover |
| Explicit rollback | validate previous release and current data compatibility, show plan |

Roll back code/configuration automatically only while the operation still owns
its maintenance window and the tested transition permits it. Restoring an older
data backup after user writes resume would discard new data: never do that
silently. Preserve safety copies and the operation journal outside the replaced
release. Readiness must include a functional check, not just container existence.
Each unit independently verifies signature, artifact identity and its local
preconditions; the head cannot substitute a remote success claim for those checks.
Two machines cannot be switched atomically; U4 defines phase acknowledgements and
recovery after split state. The memory fuse must remain effective during loading.

All mutating lifecycle commands and backup jobs must obey a shared maintenance
lock/protocol. Already-running jobs are awaited or cause a safe refusal. Record
prior timer states and restore those states, not “enable everything”. The old
recovery runner must remain usable if new code fails. No database migration or
automatic data downgrade is included in format 1.

## 6. Implementation gates

U1 acceptance is approval of this contract, not availability of `update`.
U2 adds strict parsers/build validation and a reproducible unsigned artifact build
(fixed ordering/modes/mtime, gzip timestamp, explicit source commit), then trusted
publisher signing. Private keys and production release publication remain admin
operations. Archive reproducibility does not require identical signature bytes.

Before U2 acceptance: valid signature plus tampering/wrong-key/namespace tests;
malformed metadata/archives, version replay, unknown source, code drift, disk-full,
interruptions and resume tests. U3 adds restricted management trust; U4 adds real
pair-failure drills; U5 adds disconnected install/update and image-identity tests.
Existing installations need a one-time reviewed bootstrap with administrator sudo.
Until U3–U4 pass, local-update testing is not a supported stack-upgrade path.

Sources: [OpenSSH file signatures and allowed signers](https://man.openbsd.org/ssh-keygen),
[SemVer 2.0.0](https://semver.org/). These define primitives and version syntax;
all package fields, limits and transition policies above are RAGSpark design choices.

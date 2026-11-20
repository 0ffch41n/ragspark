# Update release contract — U1, format 1

**Implementation status.** U2.3 provides `update --check`. U2.4 adds signed
code-only application and recovery for a bootstrapped single installation.
U2.5 adds the first scheduler-only service transition. U3–U4 implement pairing
and coordinated code updates. U4.4 verified rollback and resume on a running
Spark pair. U5 covers full offline bundles and U6 covers the public release.
The tables below describe the full contract. See [INSTALL.md](INSTALL.md) for
installation commands. A signature identifies the publisher of the code that
runs with root privileges.

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

### U2.1 implementation boundary

`installer/rsinst/release.py` validates metadata and selects exact transitions;
`release_verify.py` verifies signatures with external trust files and inspects
artifact bytes. These are read-only libraries, not an `update` command. Parsing
alone does not establish trust. Source/profile selection does not replace later
checks against the installed inventory, catalog, effective Compose or host.
Channel high-water state is returned to the caller; persistence, fetching,
staging, application, restart and rollback are not implemented by these modules.
Image results explicitly report `archive-bytes-only`, not a verified/imported image.

The bounded code/model reader accepts the POSIX USTAR subset: no PAX/GNU extension
records, at most 32 path components, USTAR name/prefix byte limits (100/155),
100,000 total headers including directories, and the 20,000 ordinary-code-file
limit above. Owners must be numeric 0:0, directory modes 0755, ordinary-file modes
must match the inventory. `expanded_bytes` is the sum of ordinary-file sizes;
header/padding overhead has a separate local bound. Gzip is allowed for code only.
The future publisher must emit this subset rather than assume arbitrary tar works.

JSON nesting is capped at 32. Other collection limits: 10,000 artifacts and
transitions, 1,000 profiles and entries per role map/restart list, 32 URLs per
artifact, 100,000 ordinary model files. Trust input is bounded to 256 KiB,
revocations to 4 MiB, and OpenSSH verification to 15 s. Source files are regular,
single-link and opened without following any path-component symlink. Trust files
must belong to the verifier's effective UID (root for production) and not be
group/world writable; their ancestors must be root/verifier-owned and protected
(root-owned sticky temporary directories are permitted for isolated tests).
Checks do not freeze user-writable source files for future use: a later updater
must own private staging and check the actual bytes it applies.

### U2.3 — checking a release

Both commands require an installed version of the CLI that includes U2.3 and
administrator-provisioned trust. Use the first command for a configured stable
channel and the second for a local release directory.

```bash
sudo ragspark update --check
sudo ragspark update --check --from /path/to/bundle
```

The administrator provisions `/var/lib/ragspark-update` as a root-owned directory
with mode `0700`. Within its `trust/` directory use:

- `allowed_signers` with the independently verified publisher key, and optional
  `revoked` with the corresponding OpenSSH revocation list.
- `stable-url` with the HTTPS URL of the signed channel pointer. The signature
  is served at that URL with `.sig` appended. Local checks skip the channel.
- `baseline.json` for a legacy installation. Use the full inventory verified
  for that installation. A signed installation uses `installed-release.json`
  and `installed-release.json.sig` at the root of the update state directory.

The trust files and their parent directories must be protected from group and
world writes. A candidate bundle supplies neither the signing key nor the
baseline. The public stable endpoint and production trust bootstrap are scheduled
for the public release. Missing setup produces an error.

The check verifies the signed release, compares the installed code inventory,
checks the head's requirements and effective Compose configuration, and shows
the proposed restart and backup requirements. Disk use is estimated per
filesystem and includes staging and space for application. Models may take time
to hash. Missing artifacts are copied or downloaded into private staging and
verified, so the check can transfer large files. Image archives are checked
against the signed length and digest. Import and verification of the imported
image are later steps.

Temporary payload files are removed after the check. The accepted release
sequence stays in `accepted.json` for replay protection. In stack mode,
`update --check` checks the local role using its release profile. A code-only
transition requires the role's verified images and weights to be present
locally. The check also records the running service identities and probes
RAGFlow and the LLM on the head, or embedding and reranking on the node.
Coordinated application on the pair remains a separate step.

### U2.4 — applying a code-only release on a single unit

After one-time preparation and provisioning of the trust files above, use:

```bash
sudo ragspark update --from /path/to/bundle
sudo ragspark update --resume
sudo ragspark update --rollback
```

`sudo ragspark update` with no bundle uses the configured stable channel.
Application and explicit rollback prompt for confirmation. `--yes` confirms the
operation in unattended use. Dependencies must already match the signed profile.
Changes to services, models, units or data require a separate supported transition.

The administrator prepares the runner during a maintenance window, with the
RAGSpark stack and its backup/watch timers and jobs stopped. From a reviewed copy
of the repository, run `sudo python3 installer/bootstrap_update.py --yes`. This
copies the runner to the private state directory, installs the managed CLI
entry point, and writes systemd drop-ins. Verify the result before reloading the
manager. Perform `systemctl daemon-reload` during the maintenance window with
GPU workloads stopped, then restore the previously enabled timers and start
the stack. The runner checks that systemd has loaded the expected command paths
before it can apply an update. Provision an exact legacy inventory for the
installed revision as described above. Update preparation rejects locally
modified or group/world-writable code directories.

The runner holds a shared maintenance lock for normal commands and systemd
jobs. Publication and recovery take the exclusive lock on the same inode.
An unfinished journal blocks lifecycle commands even after a reboot. Run
`update --resume` to continue the recorded direction, or `update --rollback`
to request restoration from the sealed previous files. Start the stack
normally after recovery if it was stopped. The latest operation is retained
for explicit rollback. Rollback of a completed update refuses changes to
the installation manifest made since publication, to protect later resource
and configuration changes. A rollback leaves the accepted release sequence at
its high-water mark.

Code publication leaves the active container IDs, data, and model weights in
place. The runner verifies the installed inventory, starts a fresh CLI help
process, checks the identities of running Compose containers, and checks
RAGFlow health when services are up.

### U2.5 — first compatible service transition

The first single-node service transition covers the numeric vLLM scheduler
flags `--max-num-seqs` and `--max-num-batched-tokens` on `vllm-llm`. It
requires an independent runner provisioned from this revision. A runner
previously provisioned for U2.4 retains its code-only behavior. Image,
model, data-service, network and systemd changes require their own tested
transition. The currently running container image IDs must match the signed
profile.

The runner fetches and verifies the code before downtime. After confirmation
it records and pauses active timers, waits for RAGFlow parsing to finish,
stops the web writer, confirms that parsing is still idle and freezes the data
volumes for a verified physical backup. It keeps the backup under
`/srv/ragspark/backups/update-safety` and prints its path. Restoring this
backup is a separate administrator action.

The runner starts the original data containers and unchanged pooling services.
It stops the previous LLM before recreating the changed service and runs a
short generation test. During validation, RAGFlow is created with the port
removed from Compose. The runner inspects the container's actual port bindings
before starting it, then verifies its internal HTTP endpoint. RAGFlow is next
recreated with its normal port. The data containers are stopped and started for
the backup, and the runner checks that their container IDs still match.

An interruption or error before the durable opening phase can be recovered
from the journal with `--resume` or `--rollback`. The runner records the
opening phase before enabling the public port. Once that phase begins, recovery
proceeds forward because clients may have written new data. A later return to
the previous service configuration requires its own signed maintenance
transition. The runner never restores old data automatically. The recorded
active timers are started only after recovery reaches a terminal state.

[Publisher tools and legacy inventory](../build/release/README.md)

Sources: [OpenSSH file signatures and allowed signers](https://man.openbsd.org/ssh-keygen),
[SemVer 2.0.0](https://semver.org/). These define primitives and version syntax;
all package fields, limits and transition policies above are RAGSpark design choices.

### U3 — pairing the second Spark

Both Sparks need a RAGSpark installation with the pairing command. Use the
administrator consoles you already trust, with the configured cable link up.

1. On the head, run **sudo ragspark pair** and copy the invitation.
2. On the second Spark, run **sudo ragspark pair accept**. Paste the invitation
   at the prompt and confirm the head's access to status. Copy the confirmation
   and keep the displayed SSH host fingerprint for the next step.
3. On the head, run **sudo ragspark pair finish**. Paste the confirmation from
   the second Spark's trusted console and compare the displayed fingerprint
   with that console before approving. Pairing completes after an authenticated
   SSH status reply from the expected cable address.

Run **sudo ragspark pair status** to check the pairing. Run **sudo ragspark pair
revoke** first on the second Spark to remove authorization and then on the
head to remove the local key. Follow that order before replacing a Spark or
removing RAGSpark. A pending invitation can be displayed again with **sudo
ragspark pair**. Repeat **sudo ragspark pair accept** with the same invitation
if enrollment stopped on the second Spark.

The second Spark uses its local Ed25519 SSH host key. The dedicated pairing
account accepts status requests from the head's cable address and uses a
separate key from backups. RAGSpark reports the node identity, installed
version and systemd state. Coordinated updates remain part of U4.

After provisioning the independent update runner and pairing the Sparks, run
`sudo ragspark pair update-invite` on the head. Paste the invitation into
`sudo ragspark pair update-accept` on the second Spark and confirm the update
key fingerprint. The dedicated update key uses the pinned U3 host key and the
same cable addresses. It gives the head permission to prepare, apply and
confirm signed code updates on the second Spark. Pair revocation on the second
Spark removes both SSH grants.

### U4.3 — coordinating a code-only update

With the independent runners, signing trust and update grant provisioned,
start `sudo ragspark update` on the head. Both Sparks verify and save the
signed release before code is published. The head records the commit decision
after verification on both sides and reports success after both confirm it.
Running services keep their container identities through code publication.

After a disconnect, run `sudo ragspark update --resume` on the head. It uses
the saved operation and bundle. Use `sudo ragspark update --rollback` before
the commit decision to restore the prior code on both units. Once that
decision is saved, use `--resume` to finish it. The
[live U4.4 validation](VALIDATION.md#20-signed-updates-on-a-spark-pair-u44)
used a signed local bundle and checked the running stack before and after
both recovery paths.

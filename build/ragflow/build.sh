#!/usr/bin/env bash
# Build the RAGFlow arm64 image used by RAGSpark.
#
#   sudo build/ragflow/build.sh           build the image locally
#   sudo build/ragflow/build.sh --push    build and push to GHCR
#   build/ragflow/build.sh --prepare-only  clone and patch only (no Docker, no root needed),
#                                          e.g. RAGSPARK_BUILD_DIR=/tmp/check to inspect the tree
#
# The upstream tag is pinned to a commit: if the tag ever moves, the build
# stops instead of silently building different code.
set -euo pipefail
umask 022   # the clone and the patched files: writable by the user who builds only

RAGFLOW_REPO="https://github.com/infiniflow/ragflow.git"
RAGFLOW_TAG="v0.27.2"
RAGFLOW_COMMIT="a024bea0cd93f39e6652a42bf84dd20c55bc560b"
REVISION="r7"   # bump whenever the RAGSpark patch set changes (r1: Chrome; r2: + reranker input;
                # r3: + kNN filter, agentic pipeline, Naive by default;
                # r4: + research budget, query language, claim shapes;
                # r5: + scans: Cyrillic text layer, vision at temperature 0, whole tables;
                # r6: + health check returns its connection; base images pinned by digest;
                # r7: + DOCX chunks keep a separator between paragraphs)
IMAGE="ghcr.io/0ffch41n/ragspark-ragflow"
TAG="${RAGFLOW_TAG#v}-arm64-${REVISION}"
# The images the upstream Dockerfile builds from, by floating tags there; pinned
# here to the digests r4 and r5 were built from (docs/BUILD.md, build inputs).
BASE_IMAGE="ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac"
DEPS_IMAGE="infiniflow/ragflow_deps:latest@sha256:e69762c256ea2338a786a9b2f003da8e91e5e69b26bb6797c2206276ce32e5ff"
# The whole RAGFlow tree is the build context, so its directory must not be writable by anyone but
# the user who builds (root for a real build): not /var/tmp, where any user can create it first.
WORKDIR="${RAGSPARK_BUILD_DIR:-/var/cache/ragspark-build}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "build: $*" >&2; exit 1; }

PUSH=false
PREPARE_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=true ;;
        --prepare-only) PREPARE_ONLY=true ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) die "unknown argument: $arg" ;;
    esac
done

[[ "$(uname -m)" == "aarch64" ]] || die "needs a native arm64 host (DGX Spark)"
$PREPARE_ONLY || [[ "$EUID" -eq 0 ]] || die "run with sudo: docker access is required"
tools="git python3"
$PREPARE_ONLY || tools="$tools docker"
for tool in $tools; do
    command -v "$tool" >/dev/null || die "$tool not found"
done

# Owned by the user who builds and writable by nobody else, not a symbolic link.
own_dir() {
    [[ ! -L "$1" ]] || die "$1 is a symbolic link"
    [[ "$(stat -c %u "$1")" == "$EUID" ]] || die "$1 belongs to $(stat -c %U "$1"), not to the user who builds"
    (( (0$(stat -c %a "$1") & 022) == 0 )) || die "$1 is writable by group or others (mode $(stat -c %a "$1"))"
}

src="$WORKDIR/ragflow-$RAGFLOW_TAG"
mkdir -p "$(dirname "$WORKDIR")"
[[ -e "$WORKDIR" ]] || mkdir -m 700 "$WORKDIR"
own_dir "$WORKDIR"
if [[ ! -d "$src/.git" ]]; then
    echo "build: cloning RAGFlow $RAGFLOW_TAG"
    git clone --quiet --branch "$RAGFLOW_TAG" --depth 1 "$RAGFLOW_REPO" "$src"
fi
own_dir "$src"
actual="$(git -C "$src" rev-parse HEAD)"
[[ "$actual" == "$RAGFLOW_COMMIT" ]] || die "$RAGFLOW_TAG points to $actual, expected $RAGFLOW_COMMIT"

# Every build starts from exactly the pinned upstream commit: tracked files reset and anything
# else removed, ignored files included - the whole tree goes into the image. Then the RAGSpark
# patches; each refuses to run if the code it expects is not there.
git -C "$src" reset --quiet --hard "$RAGFLOW_COMMIT"
git -C "$src" clean --quiet -fdx
[[ -z "$(git -C "$src" status --porcelain --ignored)" ]] || die "$src is not clean after reset and clean"
python3 "$HERE/patch_dockerfile.py" "$src/Dockerfile"
python3 "$HERE/patch_base_images.py" "$src/Dockerfile" "$BASE_IMAGE" "$DEPS_IMAGE"
python3 "$HERE/patch_search.py" "$src/rag/nlp/search.py"
python3 "$HERE/patch_knn_filter.py" "$src"
python3 "$HERE/patch_agentic.py" "$src"
python3 "$HERE/patch_scans.py" "$src"
python3 "$HERE/patch_docx.py" "$src"
python3 "$HERE/patch_health.py" "$src"
python3 "$HERE/patch_web_default.py" "$src"
if $PREPARE_ONLY; then
    echo "build: $src prepared (upstream $RAGFLOW_COMMIT + RAGSpark patches), nothing built"
    exit 0
fi

echo "build: building $IMAGE:$TAG"
docker build --progress=plain --platform linux/arm64 \
    --label "org.opencontainers.image.source=https://github.com/0ffch41n/ragspark" \
    --label "org.opencontainers.image.title=ragspark-ragflow" \
    --label "org.opencontainers.image.description=RAGFlow $RAGFLOW_TAG for arm64 (NVIDIA DGX Spark), built by RAGSpark" \
    --label "org.opencontainers.image.version=$TAG" \
    --label "org.opencontainers.image.revision=$RAGFLOW_COMMIT" \
    --label "org.opencontainers.image.licenses=Apache-2.0" \
    -f "$src/Dockerfile" -t "$IMAGE:$TAG" "$src"

echo "build: image id $(docker image inspect --format '{{.Id}}' "$IMAGE:$TAG")"
if $PUSH; then
    docker push "$IMAGE:$TAG"
    echo "build: pushed $(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE:$TAG")"
fi

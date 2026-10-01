#!/usr/bin/env bash
# Build the RAGFlow arm64 image used by RAGSpark.
#
#   sudo build/ragflow/build.sh           build the image locally
#   sudo build/ragflow/build.sh --push    build and push to GHCR
#
# The upstream tag is pinned to a commit: if the tag ever moves, the build
# stops instead of silently building different code.
set -euo pipefail

RAGFLOW_REPO="https://github.com/infiniflow/ragflow.git"
RAGFLOW_TAG="v0.27.2"
RAGFLOW_COMMIT="a024bea0cd93f39e6652a42bf84dd20c55bc560b"
REVISION="r1"   # bump whenever the RAGSpark patch set changes
IMAGE="ghcr.io/0ffch41n/ragspark-ragflow"
TAG="${RAGFLOW_TAG#v}-arm64-${REVISION}"
WORKDIR="${RAGSPARK_BUILD_DIR:-/var/tmp/ragspark-build}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "build: $*" >&2; exit 1; }

PUSH=false
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=true ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) die "unknown argument: $arg" ;;
    esac
done

[[ "$(uname -m)" == "aarch64" ]] || die "needs a native arm64 host (DGX Spark)"
[[ "$EUID" -eq 0 ]] || die "run with sudo: docker access is required"
for tool in git docker python3; do
    command -v "$tool" >/dev/null || die "$tool not found"
done

src="$WORKDIR/ragflow-$RAGFLOW_TAG"
mkdir -p "$WORKDIR"
if [[ ! -d "$src/.git" ]]; then
    echo "build: cloning RAGFlow $RAGFLOW_TAG"
    git clone --quiet --branch "$RAGFLOW_TAG" --depth 1 "$RAGFLOW_REPO" "$src"
fi
actual="$(git -C "$src" rev-parse HEAD)"
[[ "$actual" == "$RAGFLOW_COMMIT" ]] || die "$RAGFLOW_TAG points to $actual, expected $RAGFLOW_COMMIT"

# Always start from the pristine upstream Dockerfile, then apply our patch.
git -C "$src" checkout --quiet -- Dockerfile
python3 "$HERE/patch_dockerfile.py" "$src/Dockerfile"

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

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
REVISION="r5"   # bump whenever the RAGSpark patch set changes (r1: Chrome; r2: + reranker input;
                # r3: + kNN filter, agentic pipeline, Naive by default;
                # r4: + research budget, query language, claim shapes;
                # r5: + scans: Cyrillic text layer, vision at temperature 0, whole tables)
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

# Always start from pristine upstream files, then apply the RAGSpark patches.
# Each patch refuses to run if the code it expects is not there.
git -C "$src" checkout --quiet -- Dockerfile rag/nlp/search.py rag/utils/es_conn.py \
    rag/advanced_rag/harness/stats.py rag/advanced_rag/agentic_rag_graph.py rag/advanced_rag/agentic_rag.py \
    rag/advanced_rag/harness/tools/search.py rag/advanced_rag/harness/tools/text_processing.py \
    rag/advanced_rag/harness/orchestrator/sufficient_context.py \
    rag/prompts/action_initialize_state.md rag/prompts/sca_query_rewrite.md \
    deepdoc/parser/pdf_parser.py rag/llm/cv_model.py rag/nlp/__init__.py \
    rag/llm/chat_model.py api/db/services/dialog_service.py web/src/utils/authorization-util.ts
python3 "$HERE/patch_dockerfile.py" "$src/Dockerfile"
python3 "$HERE/patch_search.py" "$src/rag/nlp/search.py"
python3 "$HERE/patch_knn_filter.py" "$src"
python3 "$HERE/patch_agentic.py" "$src"
python3 "$HERE/patch_scans.py" "$src"
python3 "$HERE/patch_web_default.py" "$src"

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

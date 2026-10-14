#!/usr/bin/env bash
# Prepare Fission runtimes and import them into each Ready K3s node.
# All generated files are temporary; this script contains the Dockerfile.
# Requires bash, curl, Docker with buildx, kubectl, and cluster-admin access.
set -Eeuo pipefail

usage() {
    cat <<'HELP'
Usage: bash throngfission/cli/assets/build_image.sh [--all] [options]

  --all                 Prepare python, debian and ubuntu (must be first option).
  --image PRESET|REF     python, debian (default), ubuntu, or a custom NAME:TAG.
  --base-image IMAGE     Override the build base; also provide a new --image tag.
  --platform PLATFORM   Target platform (default linux/arm64).
  --context NAME        kubectl context (default current context).
  --kubeconfig PATH     Explicit kubeconfig for all Kubernetes operations.
  --namespace NAME      Temporary Jobs namespace (default fission-function).
  --nodes NAME,NAME     Restrict imports to these nodes (default all Ready nodes).
  --work-dir DIRECTORY  Existing directory for temporary source/archive files.
  --timeout SECONDS     Network and per-node operation timeout (default 600).
  --cleanup-ttl SECONDS Finished Job retention (default 60).
  --load-only           Import the existing local image without building.
  --build-only          Prepare local images without connecting to Kubernetes.
  --help                Show this help.

Presets: python = official Fission Python 3.13 (Alpine),
         debian = Debian 12 / Python 3.13, ubuntu = Ubuntu 24.04 / Python 3.12.
Existing tags are reused, never rebuilt in place. No image is pushed to a registry.
The Docker daemon stores build cache in its own data directory, not --work-dir.
The runtime includes Python, pip and shell; install project dependencies explicitly.
Imports are pinned against kubelet image GC; remove obsolete images explicitly.
Offline nodes are reported and skipped. Rerun with --load-only when they return.
Interrupted Jobs expire after 3 * --timeout seconds and then their cleanup TTL.

Example:
  bash throngfission/cli/assets/build_image.sh --work-dir /Volumes/CodexHome
  bash throngfission/cli/assets/build_image.sh --all --work-dir /Volumes/CodexHome
  bash throngfission/cli/assets/build_image.sh --all --load-only --nodes pi-4 --work-dir /Volumes/CodexHome

Then run with the automatically selected function:
  THRONGFISSION_IMAGE=debian \
  venv/bin/python -m pytest --throngtest-backend=fission --throngtest-python=python
HELP
}

if [[ "${1:-}" == --all ]]; then
    shift
    for preset in python debian ubuntu; do bash "$0" "$@" --image "$preset"; done
    exit 0
fi

image=debian
base_image=
revision=45815b42c733a396cc37a9ca14ec052b13cf3826
platform=linux/arm64
namespace=fission-function
nodes=
work_dir=.
timeout=600
cleanup_ttl=60
load_only=false
build_only=false
kube=(kubectl)
while (($#)); do
    case "$1" in
        --help) usage; exit 0 ;;
        --load-only) load_only=true; shift ;;
        --build-only) build_only=true; shift ;;
        --image|--base-image|--platform|--context|--kubeconfig|--namespace|--nodes|--work-dir|--timeout|--cleanup-ttl)
            (($# >= 2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            case "$1" in
                --image) image=$2 ;;
                --base-image) base_image=$2 ;;
                --platform) platform=$2 ;;
                --context) kube+=(--context "$2") ;;
                --kubeconfig) kube+=(--kubeconfig "$2") ;;
                --namespace) namespace=$2 ;;
                --nodes) nodes=$2 ;;
                --work-dir) work_dir=$2 ;;
                --timeout) timeout=$2 ;;
                --cleanup-ttl) cleanup_ttl=$2 ;;
            esac
            shift 2 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
selection=$image
pull=false
case "$image" in
    python|debian|ubuntu)
        [[ -z "$base_image" ]] || { printf 'Use a custom --image tag when overriding --base-image.\n' >&2; exit 2; }
        case "$image" in
            python)
                image=ghcr.io/fission/python-env@sha256:f6c8c470c084ce7ecadcaaca76717a7ecaf24b64f5d85a94b1bafa45529b814c
                pull=true ;;
            debian) image=docker.io/library/throngfission-debian:py313-bookworm-45815b42-v1 ;;
            ubuntu)
                image=docker.io/library/throngfission-ubuntu:py312-noble-45815b42-v1
                base_image=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3 ;;
        esac ;;
esac
base_image=${base_image:-python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26}
[[ "$pull" == true || ( "$image" =~ ^[a-zA-Z0-9./_:-]+:[a-zA-Z0-9._-]+$ && "$image" != *:latest ) ]] || {
    printf 'Use an explicit, non-latest image tag.\n' >&2; exit 2;
}
if [[ "$image" != */* ]]; then
    image="docker.io/library/$image"
elif [[ "${image%%/*}" != *.* && "${image%%/*}" != *:* && "${image%%/*}" != localhost ]]; then
    image="docker.io/$image"
elif [[ "$image" == docker.io/* && "${image#docker.io/}" != */* ]]; then
    image="docker.io/library/${image#docker.io/}"
fi
[[ "$platform" == linux/arm64 || "$platform" == linux/amd64 ]] || {
    printf 'Supported platforms: linux/arm64, linux/amd64.\n' >&2; exit 2;
}
[[ "$namespace" =~ ^[a-z0-9-]+$ && "$nodes" =~ ^[a-z0-9.,-]*$ ]] || exit 2
[[ "$timeout" =~ ^[1-9][0-9]*$ && "$cleanup_ttl" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ -d "$work_dir" ]] || { printf 'Work directory does not exist: %s\n' "$work_dir" >&2; exit 2; }
for command in docker curl; do command -v "$command" >/dev/null; done
docker info >/dev/null
kube+=(--request-timeout="${timeout}s" --namespace "$namespace")
if ! "$build_only"; then
    command -v kubectl >/dev/null
    "${kube[@]}" get namespace "$namespace" >/dev/null
fi
work=$(mktemp -d "${work_dir%/}/throngfission-image.XXXXXXXX")
jobs=()
cleanup() {
    local status=$?
    trap - EXIT
    if ((${#jobs[@]})); then
        "${kube[@]}" delete job "${jobs[@]}" --ignore-not-found --wait=false >/dev/null || true
    fi
    rm -rf -- "$work"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Inspect nodes before a potentially expensive build; names and versions come
# from the API, not from local SSH configuration.
if ! "$build_only"; then
    "${kube[@]}" get nodes -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.status.nodeInfo.architecture}{" "}{.status.nodeInfo.kubeletVersion}{" "}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' > "$work/nodes"
    selected=0
    while read -r node arch version ready; do
        [[ -z "$nodes" || ",$nodes," == *",$node,"* ]] || continue
        if [[ "$ready" != True ]]; then printf 'Skipping offline node: %s\n' "$node"; continue; fi
        [[ "linux/$arch" == "$platform" && "$version" == *+k3s* ]] || {
            printf 'Unsupported node %s: %s %s\n' "$node" "$arch" "$version" >&2; exit 1;
        }
        selected=$((selected + 1))
    done < "$work/nodes"
    ((selected)) || { printf 'No Ready matching K3s nodes.\n' >&2; exit 1; }
fi

if docker image inspect "$image" >/dev/null 2>&1; then
    printf 'Reusing existing local image: %s\n' "$image"
elif "$load_only"; then
    printf 'Local image is missing: %s\n' "$image" >&2; exit 1
elif "$pull"; then
    docker pull --platform "$platform" "$image"
else
    docker buildx version >/dev/null
    for file in server.py flask_sockets.py socket_tracker.py requirements.txt; do
        curl --fail --silent --show-error --location --max-time "$timeout" \
            "https://raw.githubusercontent.com/fission/environments/$revision/python/$file" -o "$work/$file"
    done
    curl --fail --silent --show-error --location --max-time "$timeout" \
        "https://raw.githubusercontent.com/fission/environments/$revision/LICENSE" -o "$work/LICENSE.fission"
    cat > "$work/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE=python:3.13-slim-bookworm
FROM ${BASE_IMAGE} AS builder
RUN if ! command -v python3 >/dev/null; then \
        apt-get update && apt-get install -y --no-install-recommends python3-dev python3-venv; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libev-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/fission
COPY requirements.txt /tmp/requirements.txt
RUN /opt/fission/bin/python -m pip install --no-cache-dir -r /tmp/requirements.txt

FROM ${BASE_IMAGE}
ARG FISSION_REVISION
LABEL org.opencontainers.image.source="https://github.com/fission/environments" \
      org.opencontainers.image.revision="${FISSION_REVISION}" \
      org.opencontainers.image.description="Python runtime for Fission and throngfission"
RUN if ! command -v python3 >/dev/null; then \
        apt-get update && apt-get install -y --no-install-recommends python3 python3-venv; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends libev4 ca-certificates bash \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/fission /opt/fission
WORKDIR /app
COPY server.py flask_sockets.py socket_tracker.py requirements.txt LICENSE.fission /app/
ENV PATH="/opt/fission/bin:${PATH}" PYTHONUNBUFFERED=1
ENTRYPOINT ["python"]
CMD ["server.py"]
DOCKERFILE
    docker buildx build --load --provenance=false --platform "$platform" \
        --build-arg "BASE_IMAGE=$base_image" --build-arg "FISSION_REVISION=$revision" \
        --tag "$image" "$work"
fi
actual_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")
[[ "$actual_platform" == "$platform" ]] || { printf 'Image platform mismatch: %s\n' "$actual_platform" >&2; exit 1; }
if "$build_only"; then printf 'Ready locally: %s\n' "$image"; exit 0; fi
printf 'Exporting %s\n' "$image"
docker image save --output "$work/image.tar" "$image"
# Docker 29 can report an index or manifest as Id; CRI reports the config digest.
# The export identifies that configuration on both old and new Docker engines.
config_path=$(LC_ALL=C tar -xOf "$work/image.tar" manifest.json | sed -nE 's/.*"Config"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p')
config_digest=${config_path##*/}
config_digest=${config_digest%.json}
[[ "$config_digest" =~ ^[a-f0-9]{64}$ ]] || { printf 'Cannot identify exported image configuration.\n' >&2; exit 1; }
image_id="sha256:$config_digest"
image_base=${image%@*}
if [[ "$image" != *@* ]]; then image_base=${image_base%:*}; fi

wait_for_pod() {
    local job=$1 start=$SECONDS
    pod=
    until [[ -n "$pod" ]]; do
        pod=$("${kube[@]}" get pods -l "job-name=$job" -o 'jsonpath={.items[*].metadata.name}')
        ((SECONDS - start < timeout)) || { printf 'No pod created for %s\n' "$job" >&2; return 1; }
        [[ -n "$pod" ]] || sleep 1
    done
    "${kube[@]}" wait --for=condition=Ready "pod/$pod" --timeout="${timeout}s"
}

while read -r node arch version ready; do
    [[ -z "$nodes" || ",$nodes," == *",$node,"* ]] || continue
    [[ "$ready" == True ]] || continue
    printf 'Importing into %s...\n' "$node"
    job="throngfission-import-${node}-$(date +%s)-$$"
    jobs+=("$job")
    helper_image="docker.io/rancher/k3s:${version/+/-}"
    "${kube[@]}" create -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $job
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $((timeout * 3))
  ttlSecondsAfterFinished: $cleanup_ttl
  template:
    spec:
      nodeName: $node
      restartPolicy: Never
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 1
      containers:
        - name: importer
          image: $helper_image
          imagePullPolicy: IfNotPresent
          command: ["/bin/sleep", "$((timeout * 3))"]
          securityContext:
            runAsUser: 0
            allowPrivilegeEscalation: false
            # This administrative helper already controls the host runtime.
            # Ubuntu's stacked default profile can block Go's SIGURG here.
            appArmorProfile:
              type: Unconfined
          volumeMounts:
            - name: containerd
              mountPath: /run/k3s/containerd/containerd.sock
      volumes:
        - name: containerd
          hostPath:
            path: /run/k3s/containerd/containerd.sock
            type: Socket
YAML
    wait_for_pod "$job"
    remote_id=$("${kube[@]}" exec "$pod" -- crictl --timeout="${timeout}s" --config /dev/null \
        --runtime-endpoint unix:///run/k3s/containerd/containerd.sock \
        inspecti --output go-template --template '{{.status.id}}' "$image" 2>/dev/null || true)
    if [[ "$remote_id" == "$image_id" ]]; then
        printf 'Matching image already cached on %s.\n' "$node"
    else
        # Preserve canonical references without anonymous archive-index aliases,
        # which can leave stale normalized names in containerd's CRI cache.
        "${kube[@]}" exec -i "$pod" -- ctr --address /run/k3s/containerd/containerd.sock \
            --namespace k8s.io images import --local --base-name "$image_base" \
            --digests --skip-digest-for-named --platform "$platform" - < "$work/image.tar"
    fi
    # The containerd import and its CRI image-cache update are asynchronous.
    start=$SECONDS
    until remote_id=$("${kube[@]}" exec "$pod" -- crictl --timeout="${timeout}s" --config /dev/null \
        --runtime-endpoint unix:///run/k3s/containerd/containerd.sock \
        inspecti --output go-template --template '{{.status.id}}' "$image" 2> "$work/cri-error"); do
        if ((SECONDS - start >= timeout)); then cat "$work/cri-error" >&2; exit 1; fi
        sleep 1
    done
    [[ "$remote_id" == "$image_id" ]] || {
        printf 'Image digest mismatch on %s: %s != %s\n' "$node" "$remote_id" "$image_id" >&2; exit 1;
    }
    "${kube[@]}" exec "$pod" -- ctr --address /run/k3s/containerd/containerd.sock \
        --namespace k8s.io images label "$image" io.cri-containerd.pinned=pinned
    "${kube[@]}" delete job "$job" --wait=true --timeout="${timeout}s"
    printf 'Verified on %s: %s\n' "$node" "$image_id"
done < "$work/nodes"
printf '\nReady on %s nodes. Set THRONGFISSION_IMAGE=%s\n' "$selected" "$selection"

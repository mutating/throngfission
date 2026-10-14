#!/usr/bin/env bash
# Install an opt-in AppArmor profile on Ready K3s nodes. Requires cluster-admin.
# Usage: KUBECONFIG=... bash throngfission/cli/assets/install_apparmor.sh [namespace]
# Does not modify containerd's default profile or enable the admission binding.
set -Eeuo pipefail
namespace=fission-function
nodes=
timeout=180
cleanup_ttl=60
work_dir=.
kube=(kubectl)
if [[ "${1:-}" != --* && -n "${1:-}" ]]; then namespace=$1; shift; fi
while (($#)); do
    (($# >= 2)) || exit 2
    case "$1" in
        --namespace) namespace=$2 ;;
        --nodes) nodes=$2 ;;
        --timeout) timeout=$2 ;;
        --cleanup-ttl) cleanup_ttl=$2 ;;
        --work-dir) work_dir=$2 ;;
        --kubeconfig) kube+=(--kubeconfig "$2") ;;
        --context) kube+=(--context "$2") ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift 2
done
[[ "$nodes" =~ ^[a-z0-9.,-]*$ && "$timeout" =~ ^[1-9][0-9]*$ && "$cleanup_ttl" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$namespace" =~ ^[a-z0-9-]+$ ]] || exit 2
work=$(mktemp -d "${work_dir%/}/throngfission-apparmor.XXXXXXXX")
job_prefix="throngfission-apparmor-$(date +%s)-$$"
jobs=()
cleanup() {
    local status=$?
    trap - EXIT
    if ((${#jobs[@]})); then
        "${kube[@]}" --request-timeout="${timeout}s" -n "$namespace" delete job "${jobs[@]}" --ignore-not-found --wait=false >/dev/null || true
    fi
    rm -rf -- "$work"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cat > "$work/throngfission-stacked-v1" <<'PROFILE'
# Derived from containerd v2.3.2 contrib/apparmor/template.go.
# Copyright The docker Authors, The Moby Authors, The containerd Authors.
# Licensed under the Apache License, Version 2.0:
# https://www.apache.org/licenses/LICENSE-2.0
# Distributed on an AS IS BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# Changes: dedicated name and peer rules for stacked versions of this profile.
abi <abi/3.0>,
#include <tunables/global>
profile throngfission-stacked-v1 flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  network,
  capability,
  file,
  umount,
  signal (receive) peer=unconfined,
  signal (receive) peer=runc,
  signal (receive) peer=crun,
  signal (send,receive) peer=throngfission-stacked-v1,
  signal (send,receive) peer=throngfission-stacked-v1//&**,
  signal (send,receive) peer=**//&throngfission-stacked-v1,
  signal (send,receive) peer=**//&throngfission-stacked-v1//&**,
  deny @{PROC}/* w,
  deny @{PROC}/{[^1-9/],[^1-9/][^0-9/],[^1-9s/][^0-9y/][^0-9s/],[^1-9/][^0-9/][^0-9/][^0-9/]*}/** w,
  deny @{PROC}/sys/[^k]** w,
  deny @{PROC}/sys/kernel/{?,??,[^s][^h][^m]**} w,
  deny @{PROC}/sysrq-trigger rwklx,
  deny @{PROC}/kcore rwklx,
  deny mount,
  deny /sys/[^f]*/** wklx,
  deny /sys/f[^s]*/** wklx,
  deny /sys/fs/[^c]*/** wklx,
  deny /sys/fs/c[^g]*/** wklx,
  deny /sys/fs/cg[^r]*/** wklx,
  deny /sys/firmware/** rwklx,
  deny /sys/devices/virtual/powercap/** rwklx,
  deny /sys/kernel/security/** rwklx,
  ptrace (trace,tracedby,read,readby) peer=throngfission-stacked-v1,
  ptrace (trace,tracedby,read,readby) peer=throngfission-stacked-v1//&**,
  ptrace (trace,tracedby,read,readby) peer=**//&throngfission-stacked-v1,
  ptrace (trace,tracedby,read,readby) peer=**//&throngfission-stacked-v1//&**,
}
PROFILE
profile_content=$(base64 < "$work/throngfission-stacked-v1" | tr -d '\n')
"${kube[@]}" --request-timeout="${timeout}s" get nodes -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.status.nodeInfo.kubeletVersion}{" "}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' > "$work/nodes"
while read -r node version ready; do
    [[ -z "$nodes" || ",$nodes," == *",$node,"* ]] || continue
    if [[ "$ready" != True ]]; then printf 'Skipping offline node: %s\n' "$node"; continue; fi
    [[ "$version" == *+k3s* ]] || { printf 'Not a K3s node: %s\n' "$node" >&2; exit 1; }
    job="$job_prefix-$node"
    jobs+=("$job")
    "${kube[@]}" --request-timeout="${timeout}s" -n "$namespace" create -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $job
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $timeout
  ttlSecondsAfterFinished: $cleanup_ttl
  template:
    spec:
      nodeName: $node
      automountServiceAccountToken: false
      restartPolicy: Never
      terminationGracePeriodSeconds: 1
      containers:
        - name: installer
          image: docker.io/rancher/k3s:${version/+/-}
          imagePullPolicy: IfNotPresent
          command:
            - /bin/sh
            - -ec
            - |
              printf '%s' '$profile_content' | base64 -d > /tmp/throngfission-profile
              target=/host/etc/apparmor.d/throngfission-stacked-v1
              if [ -e "\$target" ] && ! cmp -s /tmp/throngfission-profile "\$target"; then
                echo 'Refusing to replace a different existing AppArmor profile' >&2
                exit 1
              fi
              chroot /host /usr/sbin/apparmor_parser -Kr < /tmp/throngfission-profile
              cp /tmp/throngfission-profile "\$target"
              chmod 644 "\$target"
              echo 'AppArmor profile installed and enforced on $node'
          securityContext:
            runAsUser: 0
            allowPrivilegeEscalation: false
            capabilities:
              add: [MAC_ADMIN]
            appArmorProfile:
              type: Unconfined
            seccompProfile:
              type: RuntimeDefault
          volumeMounts:
            - name: host
              mountPath: /host
              readOnly: true
            - name: profiles
              mountPath: /host/etc/apparmor.d
            - name: security
              mountPath: /host/sys/kernel/security
      volumes:
        - name: host
          hostPath: {path: /, type: Directory}
        - name: profiles
          hostPath: {path: /etc/apparmor.d, type: Directory}
        - name: security
          hostPath: {path: /sys/kernel/security, type: Directory}
YAML
    if ! "${kube[@]}" --request-timeout="${timeout}s" -n "$namespace" wait --for=condition=Complete "job/$job" --timeout="${timeout}s"; then
        "${kube[@]}" --request-timeout="${timeout}s" -n "$namespace" logs "job/$job" >&2 || true
        exit 1
    fi
    "${kube[@]}" --request-timeout="${timeout}s" -n "$namespace" logs "job/$job"
    "${kube[@]}" --request-timeout="${timeout}s" -n "$namespace" delete job "$job" --wait=true
done < "$work/nodes"
printf 'Ready. Select throngfission-stacked-v1 in user_namespace_policy() to use it.\n'

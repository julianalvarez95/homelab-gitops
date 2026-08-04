#!/usr/bin/env bash
# Follows logs of the most recent pod spawned by the morning-digest CronJob
# (namespace: default). Pods are named morning-digest-<epoch>-<suffix> with
# no custom labels, so the latest one is found by name prefix + creation
# timestamp instead of a label selector.
set -uo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NAMESPACE=default
PREFIX="morning-digest-"
last_pod=""

while true; do
  pod=$(kubectl get pods -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp -o name 2>/dev/null \
    | grep "^pod/${PREFIX}" | tail -1 | sed 's#^pod/##')

  if [ -z "$pod" ]; then
    echo "[status-panel] esperando el primer pod de morning-digest..."
    sleep 30
    continue
  fi

  if [ "$pod" != "$last_pod" ]; then
    echo "[status-panel] siguiendo logs de $pod"
    last_pod="$pod"
  fi

  kubectl logs -n "$NAMESPACE" "$pod" -f --tail=200 2>&1
  sleep 15
done

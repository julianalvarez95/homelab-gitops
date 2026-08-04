#!/usr/bin/env bash
# Prints ArgoCD Application sync/health status. Revision is truncated to a
# short SHA so the table stays readable in a narrow tmux pane. Reads
# directly from the Application CRD via kubectl instead of the argocd CLI,
# since argocd-server has no NodePort/Ingress on this cluster (ClusterIP
# only) and the CLI would need a port-forward kept alive just for this panel.
set -uo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

kubectl get application -n argocd \
  -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision \
  | awk 'NR==1{print;next}{$4=substr($4,1,7);print}'

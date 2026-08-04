#!/usr/bin/env bash
# Builds (or attaches to) the tmux "status" session used as an ambient
# status panel on tty1 of the homelab. 3 panes:
#   0 (left, wide)     kubectl get pods -A --watch
#   1 (top right)      ArgoCD Application sync/health, refreshed every 30s
#   2 (bottom right)   live logs of the morning-digest CronJob
# Every pane is wrapped in `while true; do <cmd>; sleep 2; done` so a dropped
# HDMI signal (HPD interrupt storm on HDMI-A-1, see dmesg) or a killed
# kubectl process never leaves a pane dead.
set -uo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$DIR/status-panel.tmux.conf"
SESSION=status
EXPECTED_PANES=3

# has-session alone isn't enough: if a pane's own loop-shell process gets
# killed (not just the command it's running), tmux destroys that pane but
# the session survives with fewer panes. A plain has-session check would
# then just re-attach to the degraded session forever. Rebuild whenever the
# pane count doesn't match what we expect.
session_is_healthy() {
  tmux -f "$CONF" has-session -t "$SESSION" 2>/dev/null || return 1
  local count
  count=$(tmux -f "$CONF" list-panes -t "$SESSION:dashboard" 2>/dev/null | wc -l)
  [ "$count" -eq "$EXPECTED_PANES" ]
}

if ! session_is_healthy; then
  tmux -f "$CONF" kill-session -t "$SESSION" 2>/dev/null

  tmux -f "$CONF" new-session -d -s "$SESSION" -n dashboard \
    'while true; do kubectl get pods -A --watch; sleep 2; done'

  tmux -f "$CONF" split-window -h -t "$SESSION:dashboard.0" \
    "while true; do watch -n 30 -t \"$DIR/argocd-status.sh\"; sleep 2; done"

  tmux -f "$CONF" split-window -v -t "$SESSION:dashboard.1" \
    "while true; do \"$DIR/follow-morning-digest.sh\"; sleep 2; done"

  tmux -f "$CONF" select-layout -t "$SESSION:dashboard" main-vertical

  tmux -f "$CONF" select-pane -t "$SESSION:dashboard.0" -T "kubectl get pods -A"
  tmux -f "$CONF" select-pane -t "$SESSION:dashboard.1" -T "argocd sync status (30s)"
  tmux -f "$CONF" select-pane -t "$SESSION:dashboard.2" -T "morning-digest logs"

  tmux -f "$CONF" select-pane -t "$SESSION:dashboard.0"
fi

exec tmux -f "$CONF" attach-session -t "$SESSION"

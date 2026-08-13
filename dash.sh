#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Dashboard terminal : MONITOR en haut + BOUCLE de codage en bas, une seule
# fenêtre tmux.
#
# Usage :  ./dash.sh
# Navigation tmux :
#   Ctrl+B puis ↑/↓   changer de panneau
#   Ctrl+B z          zoom plein écran du panneau courant
#   Ctrl+B d          détacher (la boucle continue en arrière-plan)
#   tmux attach -t loop   ré-attacher après détachement
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"

command -v tmux >/dev/null 2>&1 || { echo "tmux requis : brew install tmux"; exit 1; }

S="${TMUX_SESSION:-loop}"
MON_ROWS="${MON_ROWS:-14}"     # hauteur (lignes) du panneau monitor en haut
# --fresh est un ALIAS de FRESH=1 : normalisé AVANT le test de session ci-dessous,
# sinon il était silencieusement ignoré quand une session tmux existait déjà (M16).
[ "${1:-}" = "--fresh" ] && FRESH=1
# Fenêtre de contexte de la jauge : le monitor la DÉDUIT du modèle (table interne de
# monitor.mjs). Ne PAS exporter CTX_WINDOW sans demande explicite de l'utilisateur —
# un forçage 1M afficherait un faux 19 % vert à un Sonnet 200k (M7).
# Override : CTX_WINDOW=200000 ./dash.sh

# Une boucle tourne déjà dans cette session (on s'est détaché avec Ctrl+B d) → on s'y RÉ-ATTACHE
# au lieu de la TUER. Reset explicite : ./dash.sh --fresh ou FRESH=1 ./dash.sh (session neuve).
if [ "${FRESH:-0}" != 1 ] && tmux has-session -t "$S" 2>/dev/null; then
  echo "Session '$S' déjà active (boucle en cours ?) → ré-attache. Repartir de zéro : ./dash.sh --fresh"
  exec tmux attach -t "$S"
fi
# --fresh : met le kit + CE projet à jour AVANT de lancer quoi que ce soit (monitor inclus,
# sinon le panneau 0 démarrerait sur l'ancien monitor.mjs). Même mécanique que loop.sh --fresh.
if [ "${1:-}" = "--fresh" ]; then
  KIT_DIR="${KIT_DIR:-$HOME/Documents/GitHub/autonomous-loop-kit}"
  if [ -d "$KIT_DIR" ]; then
    git -C "$KIT_DIR" pull --ff-only 2>/dev/null | tail -1 || true
    [ -x "$KIT_DIR/sync-kit.sh" ] && "$KIT_DIR/sync-kit.sh" "$PWD" | tail -2
    [ -f loop.sh.new ] && mv -f loop.sh.new loop.sh && chmod +x loop.sh
    echo "↻ --fresh : kit $(git -C "$KIT_DIR" log -1 --format=%h 2>/dev/null || echo '?') synchronisé."
  else
    echo "⚠ --fresh : kit introuvable ($KIT_DIR) — définis KIT_DIR=… ; lancement sur la version locale." >&2
  fi
fi

tmux kill-session -t "$S" 2>/dev/null || true
tmux new-session -d -s "$S"

# Panneau 0 (HAUT) = monitor temps réel (jauge auto par modèle ; CTX_WINDOW relayé
# au monitor SEULEMENT si l'utilisateur l'a défini — priorité absolue côté monitor).
MON_ENV=""
[ -n "${CTX_WINDOW:-}" ] && MON_ENV="CTX_WINDOW=${CTX_WINDOW} "
tmux send-keys -t "$S":0.0 "${MON_ENV}node monitor.mjs" C-m

# Split horizontal → panneau 1 (BAS) = boucle de codage
tmux split-window -v -t "$S":0.0
tmux send-keys -t "$S":0.1 './loop.sh' C-m

# Haut compact ; ré-épinglé à chaque resize de la fenêtre.
tmux resize-pane -t "$S":0.0 -y "$MON_ROWS" 2>/dev/null || true
tmux set-hook -t "$S" client-resized "resize-pane -t ${S}:0.0 -y ${MON_ROWS}" 2>/dev/null || true
tmux set-hook -t "$S" window-resized "resize-pane -t ${S}:0.0 -y ${MON_ROWS}" 2>/dev/null || true
tmux select-pane -t "$S":0.1

tmux attach -t "$S"

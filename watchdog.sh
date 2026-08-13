#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# watchdog.sh — RELANCE AUTOMATIQUE d'une boucle TOMBÉE (crash / incident modèle / max_iters).
#
# Complémentaire de monitor-watch.sh : le monitor te RÉVEILLE (gate/arrêt/heartbeat) ; le watchdog,
# lui, REDÉMARRE la boucle tout seul. À lancer par cron toutes les 5 min.
#
#   */5 * * * * /chemin/watchdog.sh /chemin/repo      (ou : ./watchdog.sh --install /chemin/repo)
#
# IDEMPOTENT — ne fait RIEN si :
#   • la boucle TOURNE déjà (verrou VIVANT : pid + identité ancrés au repo — pas un pgrep global
#     qui confondrait avec la boucle d'un AUTRE projet)  • .done existe (fini)
#   • .gate-handoff en attente (gate humain).
# Sinon (tombée, pas finie, pas de gate) → nettoie le VERROU résiduel + relance. Journal : .monitor/watchdog.log
#
# Env : LOOP_SCRIPT=loop.sh · LAUNCH_CMD='./loop.sh' (forks : './run-v7.sh') · DONE_FILE=.done
#       HANDOFF_FILE=.gate-handoff · MONITOR_DIR=.monitor (sinon clé lue du loop.conf du repo, M15)
#       WD_MAX_RESTARTS=5 · WD_WINDOW=3600 (backoff anti-toupie : pause après N relances/fenêtre)
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

# ── Installateur de cron (*/5) ───────────────────────────────────────────────
# Capture les réglages passés en ENV à l'install et les GRAVE dans la ligne cron (cron n'hérite pas de
# ton shell). Une commande pour un fork :
#   LOOP_SCRIPT=run-v7.sh DONE_FILE=.done-v7 LAUNCH_CMD=./run-v7.sh ./watchdog.sh --install <repo>
if [ "${1:-}" = "--install" ]; then
  shift
  REPO="$(cd "${1:-.}" && pwd)" || { echo "repo introuvable" >&2; exit 2; }
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  ENVP=""
  for v in LOOP_SCRIPT LAUNCH_CMD DONE_FILE HANDOFF_FILE MONITOR_DIR WD_MAX_RESTARTS WD_WINDOW; do
    eval "val=\${$v:-}"
    [ -n "$val" ] && ENVP="$ENVP $v=$(printf '%q' "$val")"
  done
  LINE="*/5 * * * *${ENVP} $SELF $REPO # autonomous-loop watchdog $(basename "$REPO")"
  # Dans une ligne crontab, % = passage à la ligne (tout ce qui suit part en stdin de la
  # commande) → un chemin avec % doit être échappé \% sous peine d'une relance jamais exécutée.
  LINE="$(printf '%s' "$LINE" | sed 's/%/\\%/g')"
  ( crontab -l 2>/dev/null | grep -vF "$SELF $REPO"; echo "$LINE" ) | crontab - \
    && echo "✅ cron installé : $LINE" || { echo "échec crontab" >&2; exit 1; }
  [ -n "$ENVP" ] && echo "   env gravé dans la ligne :${ENVP}"
  echo "   (désinstaller : crontab -l | grep -vF '$SELF $REPO' | crontab -)"
  exit 0
fi

REPO="${1:-.}"; cd "$REPO" 2>/dev/null || { echo "watchdog: repo introuvable: $REPO" >&2; exit 2; }
LOOP_SCRIPT="${LOOP_SCRIPT:-loop.sh}"
LAUNCH_CMD="${LAUNCH_CMD:-./$LOOP_SCRIPT}"
DONE_FILE="${DONE_FILE:-.done}"
HANDOFF_FILE="${HANDOFF_FILE:-.gate-handoff}"

# Vitalité du verrou : helper COMMUN lock-alive.sh (pid du verrou + identité ancrée au repo —
# fin du pgrep global multi-projets, M10). Repli si absent (vieux kit non synchronisé) :
# pgrep historique — dégradé (aveugle aux autres projets), jamais dangereux.
_LA="$(cd "$(dirname "$0")" 2>/dev/null && pwd -P)/lock-alive.sh"
if [ -f "$_LA" ]; then . "$_LA"; else
  lock_alive() { pgrep -f "$LOOP_SCRIPT" >/dev/null 2>&1; }
  monitor_dir_of() { printf '.monitor'; }
fi

# MONITOR_DIR : env prioritaire, sinon clé du loop.conf de CE repo (M15), sinon .monitor.
MONITOR_DIR="${MONITOR_DIR:-$(monitor_dir_of .)}"
mkdir -p "$MONITOR_DIR"
LOG="$MONITOR_DIR/watchdog.log"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# ── Idempotence : ne rien faire si terminé / en attente de gate / déjà en cours ─
for d in "$DONE_FILE" "$DONE_FILE"-*; do [ -e "$d" ] && exit 0; done   # .done ou .done-v7 → fini
[ -f "$HANDOFF_FILE" ] && exit 0                                        # gate humain en attente
if lock_alive .; then exit 0; fi             # boucle VIVANTE de CE repo (verrou + pid + identité)

# ── La boucle est TOMBÉE → nettoie le verrou résiduel + relance (détaché) ─────
[ -d "$MONITOR_DIR/.lock" ] && { rm -rf "$MONITOR_DIR/.lock" 2>/dev/null; log "verrou résiduel .lock nettoyé"; }

# Backoff anti-toupie (M15) : une boucle qui TOMBE EN BOUCLE était relancée toutes les 5 min
# pour toujours. Fenêtre glissante : plus de WD_MAX_RESTARTS relances dans les WD_WINDOW
# dernières secondes → pause (le cron réessaiera ; la fenêtre finit par glisser).
WD_MAX_RESTARTS="${WD_MAX_RESTARTS:-5}"
WD_WINDOW="${WD_WINDOW:-3600}"
RESTARTS="$MONITOR_DIR/watchdog.restarts"
_now=$(date +%s); _n=0
if [ -f "$RESTARTS" ]; then
  _tmp="$RESTARTS.tmp.$$"
  while IFS= read -r _t; do
    case "$_t" in ''|*[!0-9]*) continue ;; esac
    [ $((_now - _t)) -lt "$WD_WINDOW" ] && { echo "$_t" >> "$_tmp"; _n=$((_n+1)); }
  done < "$RESTARTS" > /dev/null
  [ -f "$_tmp" ] && mv -f "$_tmp" "$RESTARTS" || : > "$RESTARTS"   # purge des entrées hors fenêtre
fi
if [ "$_n" -ge "$WD_MAX_RESTARTS" ]; then
  log "backoff : $_n relances dans les ${WD_WINDOW}s (max $WD_MAX_RESTARTS) — boucle qui tombe en boucle ? pause, le cron réessaiera."
  exit 0
fi

log "boucle absente (ni .done, ni gate) → relance : $LAUNCH_CMD"
# LAUNCH_CMD est une LIGNE DE COMMANDE interprétée par bash -c (script + args, ex.
# './run-v7.sh --fresh') — expansion shell volontaire, NE PAS la quoter via %q (qui figerait
# la ligne entière en UN SEUL mot et casserait tout appel avec argument). Un chemin à espace
# se quote DANS la valeur : LAUNCH_CMD=\"'/chemin avec espace/run.sh' --fresh\".
# Seul $PWD (un chemin, jamais une ligne de commande) est quoté via %q.
nohup bash -c "cd $(printf '%q' "$PWD") && exec $LAUNCH_CMD" >> "$MONITOR_DIR/watchdog-launch.out" 2>&1 < /dev/null &
echo "$_now" >> "$RESTARTS"
log "relancée (pid $!)"

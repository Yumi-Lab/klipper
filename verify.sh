#!/bin/sh
# Gate mecanique de la boucle (lance apres chaque codeuse). Rouge -> .done refuse.
# Volontairement MINIMAL et local : jamais de deploiement, jamais de mouvement
# reel sur le pad physique depuis ce script.
set -e

echo "== py_compile =="
python3 -m py_compile klippy/kinematics/extruder.py klippy/extras/motion_queuing.py

echo "== gcc -fsyntax-only (kin_extruder.c) =="
gcc -fsyntax-only -I klippy/chelper klippy/chelper/kin_extruder.c

# Harnais Klipper (simulateur) : necessite un .dict MCU compile (Lot 1 de
# PROGRESS.md). Tant qu'il n'existe pas, on ne bloque PAS les autres lots
# dessus -- on le saute avec un message clair plutot que d'echouer a chaque
# tour pour une raison deja connue et suivie.
DICT=$(find . -maxdepth 3 -iname "*.dict" 2>/dev/null | head -1)
if [ -n "$DICT" ]; then
    DICTDIR=$(dirname "$DICT")
    echo "== test_klippy.py (dict trouve: $DICT) =="
    python3 scripts/test_klippy.py -d "$DICTDIR" test/klippy/pressure_advance.test
    python3 scripts/test_klippy.py -d "$DICTDIR" test/klippy/extruders.test
else
    echo "== test_klippy.py SAUTE : aucun .dict MCU trouve (Lot 1 non fait) =="
fi

echo "OK"

#!/bin/sh
# Gate mecanique de la boucle (lance apres chaque codeuse). Rouge -> .done refuse.
# Volontairement MINIMAL et local : jamais de deploiement, jamais de mouvement
# reel sur le pad physique depuis ce script.
set -e

echo "== py_compile =="
python3 -m py_compile klippy/kinematics/extruder.py klippy/extras/motion_queuing.py

echo "== gcc -fsyntax-only (kin_extruder.c) =="
gcc -fsyntax-only -I klippy/chelper klippy/chelper/kin_extruder.c

# Banc de continuite du rattrapage de jeu (Lot 3) : rejoue le vrai
# kin_extruder.c sur la sequence dense d'un changement de couche et exige
# un offset SANS saut (seuil relatif au bras de reference du meme run, cf.
# l'en-tete du banc). Compile en natif : pas de .dict ni docker requis.
echo "== banc continuite backlash (scripts/backlash_overlap_bench.c) =="
mkdir -p .loop/tmp
cc -O2 -w -I klippy/chelper -o .loop/tmp/bl_overlap_bench \
    scripts/backlash_overlap_bench.c -lm
.loop/tmp/bl_overlap_bench

# Harnais Klipper (simulateur) : necessite un .dict MCU compile (Lot 1 de
# PROGRESS.md). Tant qu'il n'existe pas, on ne bloque PAS les autres lots
# dessus -- on le saute avec un message clair plutot que d'echouer a chaque
# tour pour une raison deja connue et suivie.
# Le harnais tourne dans un conteneur Linux : le module C chelper n'est pas
# compilable natif sur macOS (pyhelper.c inclut <sys/prctl.h>, serialqueue.c
# <linux/can.h>) et le build MCU exige une cible ELF (pas de Mach-O).
# Chercher d'abord ./dict (repertoire dedie) : un out/klipper.dict residuel
# d'un build MCU local ne correspond pas au nom attendu par les .test
# (atmega2560.dict) et ferait rater le harnais pour une mauvaise raison.
if [ -d ./dict ]; then
    DICT=$(find ./dict -maxdepth 1 -iname "*.dict" 2>/dev/null | head -1)
else
    DICT=$(find . -maxdepth 3 -iname "*.dict" 2>/dev/null | head -1)
fi
if [ -z "$DICT" ]; then
    echo "== test_klippy.py SAUTE : aucun .dict MCU trouve (Lot 1 non fait) =="
elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "== test_klippy.py SAUTE : docker indisponible (harnais Linux) =="
else
    DICTDIR=$(dirname "$DICT")
    echo "== test_klippy.py via docker (dict: $DICT) =="
    # Image et paquets epingles (revue Lot 4) : le harnais ne doit pas deriver
    # au gre des publications PyPI/Docker Hub. Digest python:3.12 et versions
    # captures le 2026-08-13 (Lot 1) ; a revoir sciemment, jamais par accident.
    PYIMG=python@sha256:dd4fe98ab39f91e936f8e7e7a65a3ce59ecfb11e32f9a125b3132779920ba7f7
    docker run --rm -e HOME=/tmp -v "$PWD:/src" -w /src "$PYIMG" \
        bash -c "pip install -q greenlet==3.5.5 cffi==2.1.1 pyserial==3.5 \
                 jinja2==3.1.6 && \
                 python scripts/test_klippy.py -d '$DICTDIR' \
                     test/klippy/pressure_advance.test test/klippy/extruders.test \
                     test/klippy/backlash_layer_change.test \
                     test/klippy/backlash_live_reconfig.test \
                     test/klippy/backlash_drained_reconfig.test"
fi

echo "OK"

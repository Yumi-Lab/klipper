#!/bin/sh
# Regenere dict/atmega2560.dict — le dictionnaire du MCU simulateur atmega2560
# exige par scripts/test_klippy.py (-d dict/) et donc par verify.sh.
#
# Pourquoi un conteneur : le build MCU exige une cible ELF, impossible en
# natif macOS (Mach-O refuse __section(".compile_time_request")), et il faut
# avr-gcc. Le build se fait dans une COPIE en /tmp du conteneur pour ne pas
# ecraser le .config ni le out/ de l'arbre de travail (qui peuvent appartenir
# a un autre build, ex. carte STM32).
#
# Image epinglee par digest (meme exigence que verify.sh : pas de derive).
# debian:bookworm-slim capture le 2026-08-13.
set -e

IMAGE=debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
CONFIG=${1:-test/configs/atmega2560.config}
NAME=$(basename "$CONFIG" .config)

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "ERREUR : docker indisponible (le build MCU exige Linux + avr-gcc)." >&2
    exit 1
fi

docker run --rm -v "$PWD:/src" "$IMAGE" bash -c '
    set -e
    apt-get update -qq
    apt-get install -y -qq gcc-avr avr-libc make python3 >/dev/null
    mkdir -p /tmp/build
    cd /src
    tar --exclude=.git --exclude=klippy-env --exclude=out --exclude=dict \
        -cf - . | tar -xf - -C /tmp/build
    cd /tmp/build
    cp "'"$CONFIG"'" .config
    make olddefconfig
    make -j"$(nproc)"
    mkdir -p /src/dict
    cp out/klipper.dict "/src/dict/'"$NAME"'.dict"
'

echo "dict/$NAME.dict regenere depuis $CONFIG."

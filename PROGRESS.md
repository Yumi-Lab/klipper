# PROGRESS

## Lots

- [x] **Lot 0 — déjà fait AVANT la boucle (par Fable, en direct avec Nicolas)** :
      diagnostic + fix du crash "Internal error in stepcompress" au moment d'un
      changement de paramètre backlash EN DIRECT (idle, hors impression) —
      `motion_queuing.check_step_generation_scan_windows()` manquant après
      `extruder_set_backlash()`. Commit `e98c567`. **Confirmé résolu en live sur
      le pad physique** pour ce cas précis (deux `SET_pressure_advance`
      successifs avec `BACKLASH_SPEED` différent, imprimante idle : plus de crash).
      Ce lot ne doit PAS être refait ni régressé.

- [x] **Lot 1 — obtenir un harnais de test exécutable.** ✅ FAIT (voir Journal
      2026-08-13) : `dict/atmega2560.dict` construit en conteneur (gcc-avr),
      `verify.sh` exécute `test_klippy.py` dans un conteneur `python:3.12` —
      natif macOS impossible pour les deux étages (Mach-O refuse les sections
      ELF du build MCU ; chelper inclut des en-têtes Linux-only). De plus un
      vrai bug de build du fork a dû être corrigé : `DECL_CONSTANT_STR` avec
      valeur vide (`CONFIG_YUMI_CONFIG`/`CONFIG_YUMI_COMMENT` vides par défaut)
      plantait `buildcommands.py`.

- [ ] **Lot 2 — tracer précisément l'enchaînement qui produit "Invalid sequence".**
      Lire `note_extrude_dir` (extruder.py) + `extruder_backlash_flip` /
      `backlash_lookup` (kin_extruder.c) en détail. Construire (sur le papier ou
      en test) une séquence de mouvements représentative d'un changement de
      couche (retrait, travel XY, éventuel Z, reprise) avec
      `BACKLASH_DEDUCT=0.5 BACKLASH_BLEED=10 BACKLASH_SPEED=40 BACKLASH_ACCEL=15000`
      (config exacte qui a crashé), et dérouler à la main quels jalons
      `backlash_params` existent dans `bl_list` à chaque étape, avec quels
      `print_time`/`ramp`/`target`. Identifier le point exact où l'invariant
      "liste triée par print_time croissant, jamais plus de contenu que ce que
      stepcompress peut relire" est violé. Écrire la conclusion dans le Journal
      ci-dessous AVANT de coder le fix (pas de fix sans cause identifiée).

- [ ] **Lot 3 — implémenter le fix.**
      Doit respecter YUMI_PATCHES.md (jamais élargir `gen_steps_pre/post_active`
      sans `motion_queuing.check_step_generation_scan_windows()`). Commenté en
      français dans le même style que l'existant. `py_compile` + `gcc
      -fsyntax-only` obligatoires avant commit.

- [ ] **Lot 4 — étendre le harnais de test avec un cas qui reproduit le bug**
      (si Lot 1 a abouti), ou documenter précisément le scénario de reproduction
      manuel sinon.

- [ ] **Lot 5 — mettre à jour `YUMI_PATCHES.md`** avec cette 5ᵉ cause, même
      registre d'écriture que les 4 précédentes.

- [ ] **Lot 6 — GATE HUMAIN (matériel réel).** Écrire `.gate-handoff` avec :
      la config backlash exacte à appliquer, le G-code/print à lancer, la durée
      minimale d'observation (au moins 40 min pour couvrir le délai observé la
      dernière fois), et ce qui confirme PASS (aucun "Internal error in
      stepcompress" / "Invalid sequence" sur plusieurs changements de couche).
      **STOP la boucle après avoir écrit ce fichier — ne pas cocher ce lot
      soi-même.** Sera coché seulement après confirmation humaine du test réel.

## Journal
(la codeuse et la contrôleuse ajoutent une ligne horodatée par itération significative)

- **2026-08-13 08:55Z — codeuse, Lot 1 FAIT.** Harnais exécutable, 3 obstacles levés :
  (1) `buildcommands.py` plantait le build MCU sur `DECL_CONSTANT_STR YUMI_CONFIG `
  (valeur vide : `CONFIG_YUMI_CONFIG`/`CONFIG_YUMI_COMMENT` valent `""` par défaut dans
  `src/Kconfig`, la concaténation de littéraux C ne laisse aucun 3ᵉ champ) → tolérance
  chaîne vide ajoutée ; (2) build `.dict` impossible en natif macOS (Mach-O refuse
  `__section(".compile_time_request")`, cible ELF requise) → build en conteneur
  `debian:bookworm-slim` + gcc-avr → `dict/atmega2560.dict` ; (3) chelper non compilable
  natif macOS (`pyhelper.c`: `<sys/prctl.h>`, `serialqueue.c`: `<linux/can.h>`) → harnais
  exécuté dans conteneur `python:3.12` via `verify.sh` (gitignore : `klippy-env/`).

  PROOF :
  cmd exacte : `./verify.sh` — qui lance
  `docker run --rm -e HOME=/tmp -v "$PWD:/src" -w /src python:3.12 bash -c "pip install -q greenlet cffi pyserial jinja2 && python scripts/test_klippy.py -d './dict' test/klippy/pressure_advance.test test/klippy/extruders.test"`
  sortie réelle (dernières lignes) :
  ```
      Starting test/klippy/pressure_advance.test (pressure_advance.cfg)
      Starting test/klippy/extruders.test (extruders.cfg)

      All 2 test cases passed
  OK
  ```
  critère numérique : rc=0, 2/2 tests passés.
  attribution : repo `636ef85` + fix buildcommands du jour ; hôte macOS 15.2 arm64,
  Apple clang 16.0.0 ; conteneur `python@sha256:dd4fe98a…` (Debian bookworm, python 3.12) ;
  dict construit avec avr-gcc bookworm depuis `test/configs/atmega2560.config` ;
  date 2026-08-13T08:54Z.
  VARIED: — (mise en service, premier run vert) / HELD FIXED: code klippy, fichiers de test.
  WHAT THIS DOES NOT SAY: ne reproduit PAS le crash « Invalid sequence » (scénario dense
  de changement de couche = Lot 2/4) ; harnais hôte seul, aucun timing MCU réel ; le cas
  backlash existant (`BOWDEN_LENGTH=800 BACKLASH_COEF=1`) passe mais n'exerce pas
  `BACKLASH_DEDUCT`/`BACKLASH_BLEED` en séquence dense.
  Prochaine étape : Lot 2 — tracer l'enchaînement exact de jalons qui produit le crash.

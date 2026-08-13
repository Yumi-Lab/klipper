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

- [ ] **Lot 1 — obtenir un harnais de test exécutable.**
      `python3 scripts/test_klippy.py -d <dictdir> test/klippy/pressure_advance.test`
      échoue faute de `.dict` MCU compilé (aucun `out/` ni `*.dict` dans le repo).
      Décision : soit produire ce dict (build simulateur/linux, voir
      `docs/Debugging.md` upstream Klipper si présent, ou `make menuconfig` →
      choisir l'architecture "Linux process" → `make`), soit documenter
      précisément pourquoi ce n'est pas praticable dans le temps imparti et
      passer au plan B (revue de code rigoureuse + compilation stricte,
      explicitement noté comme tel dans GOAL.md/DoD point 3). Ne pas s'acharner
      plus d'un lot dessus si ça bloque — basculer et continuer.

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

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

- [x] **Lot 2 — tracer précisément l'enchaînement qui produit "Invalid sequence".**
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

- [x] **Lot 3 — implémenter le fix.**
      Doit respecter YUMI_PATCHES.md (jamais élargir `gen_steps_pre/post_active`
      sans `motion_queuing.check_step_generation_scan_windows()`). Commenté en
      français dans le même style que l'existant. `py_compile` + `gcc
      -fsyntax-only` obligatoires avant commit.

- [x] **Lot 4 — étendre le harnais de test avec un cas qui reproduit le bug**
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

- **2026-08-13 09:25Z — codeuse, Lot 2 FAIT. Cause racine identifiée.**

  **Mécanisme.** `backlash_lookup` (kin_extruder.c:167) modélise chaque
  transition comme « offset tenu à `prev->target` jusqu'à `bp->print_time -
  bp->ramp`, puis rampe en S vers `bp->target` ». Quand deux jalons sont
  espacés de MOINS que la rampe (Δt < ramp), la fenêtre de la 2ᵉ transition
  commence AVANT l'atterrissage de la 1ʳᵉ : au `print_time` du jalon du
  milieu, la paire active devient (milieu, suivant) et l'interpolation est
  évaluée à u = 1 − Δt/ramp > 0 → l'offset SAUTE instantanément de
  `(target_suivant − target_milieu) · f(1 − Δt/ramp)` (f = profil en S).
  Saut de position → itersolve place N>1 pas au même instant → stepcompress
  `check_line` refuse (`interval=0, add=0, count>1`, stepcompress.c:219,
  CHECK_LINES=1 compilé en dur) → « Invalid sequence ». La signature
  observée `o=5 i=0 c=12 a=0` colle : il suffit d'un saut ≥ 2 pas (≈ 5 µm à
  400 pas/mm) ; les sauts mesurés vont de 13 µm à 1,15 mm.

  **Pourquoi un changement de couche, avec DEDUCT/BLEED, après ~37 min.** La
  rampe de la config du crash est LONGUE (58,9 ms à BACKLASH_SPEED=40, vs
  19,6 ms au défaut 120) → fenêtre de recouvrement 3× plus large. Un
  changement de couche sur petite pièce empile : retrait (−jeu) → reprise
  20-55 ms plus tard (−deduct) → paliers de bleed toutes les ~10-30 ms
  (segments courts rapides) → retrait suivant parfois 20 ms après le dernier
  palier (Δtarget = jeu entier). Chaque paire < rampe = un saut. Sans
  DEDUCT/BLEED il n'y a que la paire retrait/reprise (souvent > rampe sauf
  toutes petites pièces) ; avec, 8 paliers × chaque couche multiplient les
  occasions. Il faut une densité suffisante → délai avant le premier saut
  assez gros. Piste (d) écartée par le calcul : accélération de crête du
  profil en S = 6·1,5708/0,0589² ≈ 2717 mm/s², très sous BACKLASH_ACCEL=15000
  (et la crête de vitesse est 40 mm/s, la limite déclarée) — aucune borne
  physique implicite dépassée.

  **Invariants nommés dans la case vérifiés et NON en cause** : (a) le
  nettoyage d'`extruder_backlash_flip` est sûr — il conserve toujours le
  dernier jalon antérieur à `last_flush_time − ramp` (plancher) et la
  génération de pas ne lit jamais en arrière de la tête de flush telle
  qu'elle valait au moment du nettoyage (tête monotone, même thread) ;
  (c) les insertions sont triées — `process_move` appelle `note_extrude_dir`
  dans l'ordre croissant des `print_time` (lead=0, une seule file). L'invariant
  RÉELLEMENT violé est la **continuité de la position commandée**.

  PROOF :
  cmd exacte : `cc -O2 -w -I klippy/chelper -o .loop/tmp/bl_overlap_bench
  .loop/tmp/bl_overlap_bench.c -lm && .loop/tmp/bl_overlap_bench`
  (banc C ad hoc, scratch non commité, qui `#include` le VRAI
  `klippy/chelper/kin_extruder.c` de ce commit et rejoue les séquences de
  jalons ; cibles calculées selon la logique de `note_extrude_dir`, config
  exacte du crash : play=1,5708 mm, ramp=58,9 ms, deduct=0,5, bleed=10)
  sortie réelle (extraits) :
  ```
  A dense (chgt couche, crash)       play=1.5708 ramp=58.9ms jalons=8
    saut max = 1.150457 mm @ t=1.1550 s  = 460.18 pas @ 400 pas/mm
      jalon 6 t=1.155 cible=+0.0000 : -0.000001 -> -1.150457  ecart=-1.150456 mm
      jalon 2 t=1.075 cible=-0.4375 : -0.437501 -> -0.345950  ecart=+0.091551 mm
      jalon 1 t=1.055 cible=-0.5000 : -0.500009 -> -0.454225  ecart=+0.045784 mm
  B controle (jalons > rampe)        play=1.5708 ramp=58.9ms jalons=8
    saut max = 0.002000 mm @ t=2.3706 s  = 0.80 pas @ 400 pas/mm
      (tous les ecarts de jalons ≈ 0 : ±0,000014 mm max)
  C dense, rampe defaut 19.6ms       play=1.5708 ramp=19.6ms jalons=8
      (aucun ecart de jalon > 0,0002 mm : le « 2,40 pas » du saut max est la
       pente LÉGITIME de la rampe à 120 mm/s échantillonnée à 50 µs, pas une
       discontinuité)
  ```
  critère numérique : discontinuité au `print_time` d'un jalon du milieu =
  1,150 mm (≈ 460 pas à 400 pas/mm) ≫ seuil de refus stepcompress (> 1 pas à
  intervalle 0) ; bras de référence MÊME RUN (mêmes cibles, jalons espacés >
  rampe) : écart max 0,8 pas → continu — le code est innocent hors
  recouvrement, c'est bien le recouvrement qui tue.
  attribution : repo HEAD `1f1a3c4` (code sous test = kin_extruder.c de ce
  commit, inclus littéralement par le banc) ; hôte macOS 15.2 arm64, Apple
  clang 16.0.0 via `cc`, -O2 ; banc `.loop/tmp/bl_overlap_bench.c` (scratch
  non commité, rejouable tel quel) ; échantillonnage 50 µs ; date
  2026-08-13.
  VARIED: espacement des jalons (20-55 ms vs 200 ms) et rampe (58,9 ms vs
  19,6 ms) / HELD FIXED: code sous test, play=1,5708 mm, séquence de cibles
  issue de la config du crash.
  WHAT THIS DOES NOT SAY: la séquence exacte de flips de l'impression réelle
  est inconnue — le banc démontre le MÉCANISME, pas le replay du print ; le
  chemin complet itersolve→stepcompress n'est pas exécuté ici (repro
  bout-en-bout via le harnais klippy = Lot 4) ; 400 pas/mm est une hypothèse
  — les sauts de 45-92 µm crashent dès ~45 pas/mm, le plus petit (13 µm) dès
  ~154 pas/mm, donc sous toute résolution d'extrudeur réaliste ; aucun fix
  n'est validé ici.
  Conclusion : la cause est le **recouvrement des fenêtres de rampe** dans
  `backlash_lookup` quand Δt < ramp — discontinuité de la position commandée.
  Le fix `b38e1ef` (8 paliers fixes) traitait le TAUX de jalons, pas le
  recouvrement : 8 paliers restent plus denses que la rampe sur segments
  rapides, et la paire retrait/reprise peut l'être aussi.
  Prochaine étape : Lot 3 — conception du fix (pistes : ancrer la base de
  chaque rampe à la trajectoire réelle au début de sa fenêtre, ou fusionner
  les jalons plus proches que la rampe à l'insertion ; choix à trancher avec
  le banc ci-dessus comme critère avant/après).

- **2026-08-13 10:05Z — codeuse, Lot 3 FAIT. Fix : l'invariant de
  non-recouvrement est imposé À L'INSERTION du jalon** (`63e3694`,
  `klippy/chelper/kin_extruder.c`, fonction `extruder_backlash_flip`).

  **Choix de conception** (parmi les deux pistes du Lot 2). L'ancrage de la
  base de chaque rampe à la trajectoire réelle a été écarté : la récursion
  qu'il exige dans `backlash_lookup` lit des jalons en arrière de la
  rétention du nettoyage (`last_flush_time − ramp`) sur les longues chaînes
  de paliers — il aurait fallu retoucher la rétention, au risque de rouvrir
  la classe de bug n°4. La **fusion à l'insertion** garde `backlash_lookup`
  STRICTEMENT inchangé : on enforce l'invariant qu'il supposait déjà. Règle :
  si la fenêtre du nouveau jalon (`print_time − ramp`) démarre avant
  l'atterrissage du précédent, (1) le précédent est SUPPLANTÉ quand sa
  fenêtre est entièrement dans le futur (`last_start ≥ last_flush_time` —
  rien d'émis ne la suivait, sa cible ne serait jamais atteinte) ; (2) sinon
  — génération déjà entrée dans sa fenêtre — la NOUVELLE rampe est
  raccourcie pour démarrer pile à son atterrissage (à cet instant f(0)=0 et
  la base vaut `last->target` : continu des deux côtés ; la pointe de vitesse
  monte, un à-coup possible mais jamais un saut — la continuité prime).
  Conséquence physique assumée : quand les flips sont plus denses que la
  rampe, la marche du jeu n'était de toute façon pas physiquement
  réalisable (1,57 mm aller-retour en 20 ms à 40 mm/s) ; l'offset suit alors
  une rampe unique vers la cible la plus récente. Les rampes ne font que
  RÉTRÉCIR → la fenêtre de scan déclarée (`backlash_ramp`) reste valide,
  aucune resync nécessaire (YUMI_PATCHES respecté). Côté Python : rien —
  les cibles de `note_extrude_dir` sont absolues, sans dérive.
  Non-régression des 4 fixes précédents vérifiée par lecture : paliers
  fixes (b38e1ef) inchangés, rampe par jalon (0bf594f) inchangée,
  `_apply_backlash` (95e3217) passe par le même flip désormais continu,
  resync de fenêtre (e98c567) non impactée.

  **Le banc est désormais commité** (`scripts/backlash_overlap_bench.c`,
  avis du reviewer) et intégré à `verify.sh`, avec critère de gate relatif
  au bras de référence du MÊME RUN (max(1 pas, 4× contrôle) ; origine du
  seuil en en-tête du fichier). Un 4ᵉ cas (D) exerce le chemin de
  raccourcissement (flush simulé DANS la fenêtre précédente).

  PROOF 1 — le banc détecte le bug sur le code AVANT fix (HEAD `c24dd73`,
  banc commité, fix pas encore appliqué) :
  cmd exacte : `cc -O2 -w -I klippy/chelper -o .loop/tmp/bl_overlap_bench
  scripts/backlash_overlap_bench.c -lm && .loop/tmp/bl_overlap_bench`
  sortie réelle (extraits) :
  ```
  A dense (chgt couche, fusion)  saut max = 1.150457 mm = 460.18 pas
      jalon 6 t=1.155 cible=+0.0000 : -0.000001 -> -1.150457  ecart=-1.150456 mm
  D dense, flush mi-fenetre      saut max = 1.150457 mm = 460.18 pas
  B controle = 0.16 pas ; C = 0.48 pas
  seuil (meme run) = 1.00 pas
  ECHEC cas A ; ECHEC cas D ; rc=1
  ```
  critère numérique : rc=1, cas denses 460 pas > seuil 1 pas — le gate est
  bien ROUGE sans le fix (donc il mesure quelque chose).

  PROOF 2 — le banc passe APRÈS fix (HEAD `63e3694`) :
  même cmd, sortie réelle (extraits) :
  ```
  A dense (chgt couche, fusion)  saut max = 0.000400 mm = 0.16 pas
  D dense, flush mi-fenetre      saut max = 0.000542 mm = 0.22 pas
  B controle = 0.16 pas ; C = 0.48 pas   (INCHANGÉS : pas de régression)
  CONTINUITE OK : tous les cas sous le seuil. rc=0
  ```
  critère numérique : 460,18 pas → 0,16 pas (fusion) / 0,22 pas
  (raccourcissement), sous le seuil de 1 pas ; bras de contrôle identiques
  avant/après → le fix n'a pas altéré les cas qui marchaient.
  attribution : hôte macOS 15.2 arm64, Apple clang 16.0.0 via `cc`, -O2 ;
  échantillonnage 10 µs (sous la pente légitime la plus raide, 120 mm/s =
  0,48 pas/échantillon) ; date 2026-08-13.
  VARIED: code sous test (avant/après fix) / HELD FIXED: séquences de
  cibles, play=1,5708 mm, rampes 58,9/19,6 ms, modèle de flush.
  WHAT THIS DOES NOT SAY: la séquence exacte de l'impression réelle reste
  inconnue — le banc démontre la continuité sur une séquence dense
  représentative, pas le replay du print ; le chemin complet
  itersolve→stepcompress n'est pas exécuté ici (= Lot 4) ; 400 pas/mm est
  une hypothèse (le saut d'origine, 1,15 mm, crashe dès ~2 pas/mm, donc
  sous toute résolution réaliste) ; la validation matérielle reste le
  gate humain du Lot 6.

  PROOF 3 — `./verify.sh` complet vert APRÈS fix (HEAD `63e3694`) :
  sortie réelle (dernières lignes) :
  ```
  CONTINUITE OK : tous les cas sous le seuil.
  == test_klippy.py via docker (dict: ./dict/atmega2560.dict) ==
      Starting test/klippy/pressure_advance.test (pressure_advance.cfg)
      Starting test/klippy/extruders.test (extruders.cfg)
      All 2 test cases passed
  OK
  ```
  critère : rc=0 ; py_compile + gcc -fsyntax-only + banc + 2/2 tests
  klippy docker. Attribution identique au Lot 1 (même dict, même image
  python:3.12). WHAT THIS DOES NOT SAY: le harnais klippy n'exerce pas
  encore le scénario dense DEDUCT/BLEED (c'est le Lot 4).
  Prochaine étape : Lot 4 — étendre `test/klippy/pressure_advance.test`
  avec le scénario dense de changement de couche.

- **2026-08-13 11:40Z — codeuse, Lot 4 FAIT. Repro bout-en-bout dans le harnais
  klippy : ROUGE sans le fix, VERT avec.** Nouveau test dédié
  `test/klippy/backlash_layer_change.test` (+ son `.cfg`), intégré à
  `verify.sh` (3 tests klippy désormais). Traite aussi les deux avis de la
  revue du Lot 3 (commit `2070b5d`) : garde-fou rampe > 0 et cas E du banc.

  **Scénario** (config exacte du crash : play=1,571 mm, rampe=58,9 ms,
  DEDUCT=0,5 BLEED=10, extrudeur bowden démultiplié 95,5 pas/mm) : retrait,
  travel court, reprise (+30 ms), 8 paliers de bleed (+5-18 ms), retrait
  suivant (+11 ms après le dernier palier) — deux cycles. Espacements mesurés
  dans le simulateur via instrumentation temporaire (retirée après usage) :
  tous ≪ 58,9 ms, dont la paire retrait/reprise à 30 ms comme sur la machine.

  **Deux pièges de harnais découverts en route** (à connaître pour tout futur
  test de ce genre) : (1) `G1 E…` est ABSOLU par défaut — les « paliers » à
  E constant étaient des no-op ou des rétractions ; il faut `M83` ;
  (2) en mode batch, `process_move` (donc `note_extrude_dir`) est DÉFÉRÉ aux
  flushs du lookahead : sans `M400` avant le `SET_PRESSURE_ADVANCE
  BACKLASH_COEF=0` final, tout le scénario était planifié avec coef déjà à 0
  → zéro flip C. Le `M400` rend la planification déterministe (coef=1).

  PROOF 1 — ROUGE avec le code AVANT fix (`git show 63e3694~1:klippy/chelper/kin_extruder.c`
  restauré en place, soit l'état `0bf594f`, toutes les autres sources à HEAD) :
  cmd exacte : `docker run --rm -e HOME=/tmp -v "$PWD:/src" -w /src python:3.12
  bash -c "pip install -q greenlet cffi pyserial jinja2 && python
  scripts/test_klippy.py -d './dict' test/klippy/backlash_layer_change.test"`
  sortie réelle (dernières lignes) :
  ```
  Test case test/klippy/backlash_layer_change.test FAILED (Error during test)!
  b'stepcompress o=9 i=0 c=50 a=0: Invalid sequence'
  ```
  critère numérique : rc=255, message EXACT du crash de production
  (`o=9 i=0 c=50 a=0` : 50 pas au même instant — le saut retrait→reprise,
  ~0,5 mm × 95,5 pas/mm).

  PROOF 2 — VERT avec le fix (HEAD `2070b5d`) :
  même cmd, sortie réelle :
  ```
      Starting test/klippy/backlash_layer_change.test (backlash_layer_change.cfg)
      All 1 test cases passed
  ```
  critère : rc=0.

  PROOF 3 — `./verify.sh` complet vert à HEAD `2070b5d` :
  sortie réelle (dernières lignes) :
  ```
  CONTINUITE OK : tous les cas sous le seuil.   (banc, cas E inclus)
      Starting test/klippy/pressure_advance.test (pressure_advance.cfg)
      Starting test/klippy/extruders.test (extruders.cfg)
      Starting test/klippy/backlash_layer_change.test (backlash_layer_change.cfg)
      All 3 test cases passed
  OK
  ```
  critère : rc=0 ; py_compile + gcc -fsyntax-only + banc (5 cas) + 3/3 klippy.

  attribution : repo HEAD `2070b5d` ; bras ROUGE = kin_extruder.c à
  `63e3694~1` (identique à `0bf594f` pour ce fichier), tout le reste à HEAD ;
  hôte macOS 15.2 arm64 ; conteneur `python:3.12` (Debian bookworm, gcc 13.3,
  même image que Lots 1-3) ; dict `dict/atmega2560.dict` du Lot 1 ; chelper
  recompilé à chaque échange de source (clé mtime, vérifié) ; date
  2026-08-13.
  VARIED: kin_extruder.c pré/post fix / HELD FIXED: scénario, cfg, dict,
  code Python, harnais.
  WHAT THIS DOES NOT SAY: ce n'est pas le replay de l'impression réelle — le
  harnais simule le MCU atmega2560, pas la machine ; en batch les flips sont
  insérés avec le flush loin derrière (chemin de fusion surtout) — l'entrelace
  streaming exact de la production n'est pas reproduit ; les espacements
  paliers (5-18 ms) sont plus denses que le réel (~20 ms) ; la validation
  matérielle reste le gate humain du Lot 6.
  Prochaine étape : Lot 5 — documenter cette 5ᵉ cause dans YUMI_PATCHES.md.

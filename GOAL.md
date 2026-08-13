# GOAL — corriger le crash « Invalid sequence » du bowden backlash take-up en impression réelle

## Contexte du dépôt

Fork Klipper (`Yumi-Lab/klipper`), branche **`yumi-charge-essai`** (= « essai en charge »,
travail expérimental EN COURS, jamais mergé sur `master`, jamais poussé sur `origin`).
Cette branche développe une fonctionnalité de **rattrapage de jeu bowden** ("backlash
take-up" / "charge layer") : dans un tube bowden courbé, une inversion de sens du
filament coûte une course morte avant que pression/lissage/lead ne redeviennent
efficaces. La couche parcourt ce jeu EN AVANCE pour qu'il soit refermé pile quand
la vraie extrusion commence.

**LIRE EN PREMIER, à chaque itération** : `YUMI_PATCHES.md` à la racine du repo.
C'est la doc d'architecture du fork, elle documente les pièges déjà rencontrés et
la règle d'or : **ne jamais élargir `gen_steps_pre/post_active` sans resynchroniser**
(cf. section lead_time). C'est exactement la classe de bug qu'on chasse ici.

## Fichiers concernés

- `klippy/kinematics/extruder.py` — classe `ExtruderStepper`. Fonctions clés :
  `note_extrude_dir` (appelée par le planner à CHAQUE mouvement, gère le flip
  retrait/reprise + la résorption "bleed"/"deduct"), `_backlash_play`,
  `_backlash_ramp`, `_apply_backlash` (traite les changements de paramètres live).
- `klippy/chelper/kin_extruder.c` — le C : `backlash_lookup` (interpole l'offset),
  `extruder_calc_position` (applique l'offset à la position), `extruder_update_scan_window`
  (écrit `gen_steps_pre/post_active`), `extruder_set_backlash`, `extruder_backlash_reset`,
  `extruder_backlash_flip` (empile un jalon de retournement, avec nettoyage des vieux jalons).
- `klippy/chelper/__init__.py` — déclarations cffi de ces fonctions C (signatures à
  garder synchro avec le `.c`, sinon crash silencieux ou erreur d'appel).
- `klippy/extras/motion_queuing.py` — `check_step_generation_scan_windows()` : relit
  `gen_steps_pre/post_active` de TOUS les steppers synchronisés et recalcule
  `kin_flush_delay`, la fenêtre d'historique que retient le toolhead.
- `test/klippy/pressure_advance.test` — contient DÉJÀ un cas de test backlash (ligne
  ~40, `SET_PRESSURE_ADVANCE ADVANCE=0.05 BOWDEN_LENGTH=800 BACKLASH_COEF=1`), à
  compléter avec un scénario qui reproduit le bug ci-dessous.
- `scripts/test_klippy.py` — le harnais de test. Invocation : nécessite `-d DICTDIR`
  (répertoire contenant le `.dict` compilé du MCU simulateur/linux). **Aucun `.dict`
  n'existe encore dans ce repo** — Lot 0 : obtenir un build simulateur qui produit ce
  dictionnaire (`make menuconfig` → target simulateur/linux → `make`, voir la doc
  Klipper upstream `docs/Debugging.md` si besoin), pour pouvoir enfin exécuter
  `python3 scripts/test_klippy.py -d <dictdir> test/klippy/pressure_advance.test`.
  Si ce chemin s'avère disproportionné (plusieurs heures), documenter précisément le
  blocage dans PROGRESS.md et retomber sur une vérification par lecture de code
  rigoureuse + `gcc -fsyntax-only` + `py_compile`, sans jamais prétendre "testé" si
  ça ne l'est pas.

## Historique déjà résolu sur cette branche (NE PAS refaire, PARTIR DE LÀ)

Trois classes de crash déjà rencontrées et corrigées, dans l'ordre :

1. **Faux jalons qui se chevauchent** (`b38e1ef`) — la résorption posait jusqu'à
   450 jalons/seconde, ils se chevauchaient, l'offset tremblait. Fixé par un nombre
   FIXE de 8 paliers (`BACKLASH_BLEED_STEPS`).
2. **Chaque jalon doit porter SA PROPRE rampe** (`0bf594f`) — interpoler un vieux
   jalon avec la rampe COURANTE (si on a changé vitesse/accel entre-temps) fait
   sauter l'offset. Fixé : `struct backlash_params` porte désormais `ramp`.
3. **Offset non nul au moment d'un changement de paramètre** (`95e3217`,
   « six arrêts machine ») — retoucher un paramètre alors que l'offset vaut autre
   chose que zéro fait sauter la position. Fixé : `_apply_backlash` ramène l'offset
   à zéro PAR SA RAMPE avant tout changement, si `_backlash_target` est non nul.
4. **Fenêtre de scan pas resynchronisée** (`e98c567`, MOI, aujourd'hui) —
   `extruder_set_backlash` réécrit `gen_steps_pre/post_active` (même champ C que le
   `smooth_time` de la pressure advance), mais contrairement à `_set_pressure_advance`
   n'appelait jamais `motion_queuing.check_step_generation_scan_windows()` après.
   `kin_flush_delay` restait figé sur l'ancienne fenêtre, plus étroite → l'historique
   trapq était purgé trop tôt → stepcompress relisait du mouvement déjà libéré.
   Reproduit et **confirmé résolu** pour le cas "changement de BACKLASH_SPEED en
   direct, console idle, aucune impression en cours" (testé sur pad physique).

## LE BUG ENCORE OUVERT (objectif de cette boucle)

**Signature exacte** (log Klipper, pad de test) :
```
stepcompress o=5 i=0 c=12 a=0: Invalid sequence
Transition to shutdown state: Internal error in stepcompress
```
(Note : "Invalid sequence" est la sous-cause précise ; "Internal error in
stepcompress" est le message générique qui l'englobe — les deux logs se réfèrent
au MÊME crash.)

**Circonstances exactes** (log klippy horodaté, reconstitué) :
1. Config appliquée en direct (console, impression EN COURS) :
   `BOWDEN_LENGTH=800 BACKLASH_COEF=1.0 BACKLASH_SPEED=40 BACKLASH_ACCEL=15000
   BACKLASH_DEDUCT=0.5 BACKLASH_BLEED=10 SMOOTH_TIME=0 ADVANCE=0 LEAD_TIME=0`
   → `play=1.571mm`, `ramp=58.9ms` (déduits, affichés en console). **Cette commande
   a réussi normalement** (réponse échoée, aucune erreur) — donc ce n'est PAS le
   bug n°4 ci-dessus (déjà couvert par mon fix du jour), ni le bug n°3 (l'offset
   était visiblement à zéro, sinon `_apply_backlash` l'aurait géré).
2. **L'impression a continué normalement pendant ~37 minutes** après cette commande
   (aucune autre commande live entre les deux).
3. Le crash survient à ce moment-là, **sans commande de reconfiguration en cours**
   — donc dans le fonctionnement RÉGULIER de la couche backlash pendant
   l'impression : uniquement `note_extrude_dir` (appelé à chaque mouvement par le
   planner) → `extruder_backlash_flip` (C) → et la lecture par
   `extruder_calc_position`/`backlash_lookup`.
4. L'opérateur (Nicolas, qui pilote la machine) pense que ça a planté **au moment
   d'un changement de couche** — donc probablement une séquence dense
   retrait → déplacement à vide (+ eventuel Z-hop) → reprise, plusieurs inversions
   de sens rapprochées dans le temps.

**Ce que ça exclut / oriente** :
- Ce n'est PAS un problème de la fenêtre de scan mal resynchronisée au moment du
  RÉGLAGE (déjà couvert). C'est un problème de la logique de FONCTIONNEMENT
  CONTINU du flip pendant une impression réelle, avec `BACKLASH_DEDUCT=0.5` et
  `BACKLASH_BLEED=10` actifs (donc le chemin `note_extrude_dir` avec résorption
  proportionnelle ET la branche "retour, on repousse tout sauf la déduction" sont
  TOUS LES DEUX exercés — c'est le chemin le plus complexe du fichier, lignes
  ~161-216 de `extruder.py`).
- Pistes concrètes à investiguer en premier (par ordre de suspicion, à vérifier
  par lecture de code rigoureuse, pas par supposition) :
  a. **`extruder_backlash_flip` (kin_extruder.c)** : le nettoyage des vieux jalons
     utilise `cleanup = sk->last_flush_time - es->backlash_ramp`. Un changement de
     couche fait typiquement PLUSIEURS flips rapprochés (retrait, puis reprise,
     parfois un mouvement Z entre les deux). Si deux flips arrivent avec un
     `print_time` plus proche que `ramp`, le nettoyage peut-il supprimer un jalon
     dont `backlash_lookup` a encore besoin pour l'interpolation en cours ? Tracer
     précisément `list_is_last`/`list_del` sur ce cas.
  b. **`note_extrude_dir` (extruder.py, ~161-216)** : la branche résorption
     (`_bleed_left`) calcule une cible par PALIERS (`BACKLASH_BLEED_STEPS=8`) —
     est-ce qu'un changement de couche peut interrompre une résorption EN COURS
     (bleed_left > 0) avec un NOUVEAU retrait avant qu'elle soit finie ? Que
     devient `_bleed_left` dans ce cas — remis à zéro proprement (ligne ~175,
     `self._bleed_left = 0.` sur `play>0 and direction<0`) mais est-ce que le
     jalon C déjà empilé pour l'ancienne cible de résorption reste cohérent avec
     le nouveau jalon de traction posé juste après, si les deux tombent dans la
     fenêtre de la MÊME rampe ?
  c. **Le Z-hop d'un changement de couche** : est-ce qu'un mouvement SANS
     extrusion (Z pur, ou XY travel) entre deux flips peut désynchroniser
     `print_time` de l'ordre attendu par `backlash_lookup` (qui suppose `bl_list`
     trié par `print_time` croissant, rempli uniquement via `list_add_tail`) ? Si
     le planner peut réordonner/fusionner des moves d'une façon qui fait que
     `note_extrude_dir` est appelé avec un `print_time` PLUS TÔT qu'un jalon déjà
     empilé, la liste n'est plus triée → `backlash_lookup` (qui suppose l'ordre)
     peut mal interpoler ou déclencher un état incohérent côté stepcompress.
  d. Vérifier aussi si `BACKLASH_ACCEL=15000` (très élevé, valeur de test) combiné
     à `ramp=58.9ms` produit une accélération de rampe qui dépasse une limite
     physique implicite du planner (le profil en S `f(u)=u²(3-2u)` a une accel de
     crête `6·D/T²` — calculer si ça reste cohérent avec les limites machine).

**Ne PAS supposer un coupable avant d'avoir tracé le VRAI enchaînement d'appels**
(quels jalons existent dans `bl_list` juste avant le crash, avec quels
`print_time`/`ramp`/`target`). Si possible, instrumenter temporairement (logging)
dans une branche de travail pour comprendre, PUIS retirer l'instrumentation avant
de committer le fix.

## Règles absolues

- **Rester sur la branche `yumi-charge-essai`.** Ne jamais toucher à `master`, ne
  jamais rebaser sans qu'on le demande explicitement.
- **Ceci est du code de contrôle moteur pour une VRAIE imprimante 3D physique**
  (pad de test `192.168.100.106`, un banc réel, pas un simulateur). **Aucun
  déploiement automatique sur ce pad depuis la boucle** (`deploy_cmd` est
  volontairement VIDE). Toute validation matérielle passe par un gate-handoff
  humain — écrire un protocole de test PRÉCIS (commandes G-code exactes à
  rejouer, ce qu'il faut observer) plutôt que de prétendre avoir validé quoi
  que ce soit sur la machine.
- **Jamais de mention d'IA dans les commits** (pas de `Co-Authored-By`, pas de
  "Generated with", rien). Commits en français, même style que l'historique
  existant (message court, la ligne d'après explique le POURQUOI si non trivial).
- Chaque fix doit s'accompagner d'un commentaire dans le code expliquant le piège
  évité, dans le même style que l'existant (voir les commentaires `# YUMI:` et
  les blocs au-dessus de `_apply_backlash`/`backlash_lookup`).
- Si un lot casse un comportement DÉJÀ validé (les 4 fixes ci-dessus), c'est un
  échec du lot — ne jamais régresser un cas déjà réglé.
- Compilation obligatoire avant tout commit :
  `python3 -m py_compile klippy/kinematics/extruder.py klippy/extras/motion_queuing.py`
  et `gcc -fsyntax-only -I klippy/chelper klippy/chelper/kin_extruder.c`.

## Definition of Done

Cocher TOUT ce qui suit, dans l'ordre, avant de créer `.done` :
1. Cause racine du crash "Invalid sequence" en cours d'impression identifiée avec
   un raisonnement précis (quel enchaînement exact de jalons/flips la produit),
   pas une supposition.
2. Fix implémenté et commenté, cohérent avec l'architecture existante
   (YUMI_PATCHES.md respecté : jamais de fenêtre élargie sans resync).
3. Harnais de test (`test/klippy/pressure_advance.test` ou nouveau fichier dédié)
   étendu avec un scénario qui REPRODUIT le bug (séquence dense de flips avec
   BACKLASH_DEDUCT/BLEED actifs, simulant un changement de couche) — vert avec le
   fix, documenté rouge sans lui si on peut le vérifier facilement (sinon noter
   pourquoi ce n'était pas possible).
4. `YUMI_PATCHES.md` mis à jour : ajouter cette 5ᵉ cause dans la liste des
   « arrêts » déjà documentés (section rattrapage de jeu), même style d'écriture.
5. Un protocole de test LIVE écrit noir sur blanc (dans `.gate-handoff` au moment
   voulu) : quelle config backlash appliquer, combien de temps imprimer, quoi
   observer, comment confirmer que le bug ne revient plus — puis **gate-handoff,
   STOP**. Le `.done` final n'est créé qu'APRÈS qu'un humain ait confirmé le test
   live PASS (relance de la boucle après suppression de `.gate-handoff` avec
   verdict positif).

Quand TOUT est coché ET (vérifications logicielles vertes OU blocage documenté
précisément) ET le gate humain final confirmé → créer `.done`.

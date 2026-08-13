# Yumi-Lab/klipper — patches Yumi

Ce dépôt est un **fork du Klipper officiel** (`Klipper3d/klipper`). Notre version =
Klipper officiel + un petit nombre de commits Yumi, gardés **au sommet** et
**rebasés** sur l'upstream à chaque montée de version. C'est ça la « source de
vérité » : les pads clonent ce dépôt (`master`), il n'y a plus aucune copie de
fichier `.py` faite ailleurs (avant, yumi-config recopiait le patch — supprimé).

Les patches sont **côté host** — **aucun flash MCU**, jamais. Sur le pad :
`git pull` + `systemctl restart klipper`, c'est tout.

⚠️ **Depuis le rattrapage de jeu, `kin_extruder.c` n'est plus stock.** Ce fichier est du
C **hôte** (`klippy/chelper/`, compilé dans `c_helper.so`), pas du firmware MCU.
Klipper le **recompile tout seul** au démarrage quand la source est plus récente
que la bibliothèque : le premier `systemctl restart klipper` après un `git pull`
prend donc quelques secondes de plus, une seule fois. Rien à flasher, rien à
compiler à la main, et surtout **aucune GitHub Action** — la compilation dépend de
l'architecture du pad, elle ne peut se faire que sur le pad.

Conséquence pour les sync upstream : le rebase peut désormais entrer en conflit
sur `kin_extruder.c`. Les blocs Yumi y sont marqués `// YUMI` comme ailleurs.

---

## 1. Retrouver le patch à chaque sync (3 façons)

### a) Voir notre delta complet vs l'officiel
```bash
git fetch upstream
git diff upstream/master..master
```
→ affiche **exactement** ce que Yumi a ajouté/changé par rapport au Klipper officiel.
Si ça montre autre chose que nos commits attendus, quelque chose a dérivé.

### b) Lister nos commits Yumi (ceux au-dessus de l'upstream)
```bash
git log --oneline upstream/master..master
```
Doit ressembler à :
```
xxxxxxx extruder: add lead_time (anticipation/coast) at planner level
xxxxxxx Add YUMI_CONFIG build-time constant
```

### c) Repérer les blocs dans le code
Chaque ligne ajoutée par Yumi est marquée `# YUMI:` :
```bash
grep -rn "# YUMI:" klippy/      # patches Python
grep -rn "// YUMI" klippy/chelper/  # rattrapage de jeu (C hôte)
```

---

## 2. Les patches actuels

| Patch | Fichiers | Type |
|---|---|---|
| **extruder lead_time** (anticipation / coast) | `klippy/kinematics/extruder.py`, `klippy/extras/motion_queuing.py` | core patché, pur-Python |
| **rattrapage de jeu bowden** | `klippy/chelper/kin_extruder.c`, `klippy/chelper/__init__.py`, `klippy/kinematics/extruder.py` | core patché, **C hôte** |
| **YUMI_CONFIG** (constante gravée au build, lue host-side via mcu_constants) | firmware build | constante |

### Ce que fait le rattrapage de jeu
Dans un bowden courbé, le filament comprimé s'appuie sur la paroi extérieure et,
en traction, sur l'intérieure. Toute inversion coûte donc une **course morte** :

    jeu = (diamètre intérieur − diamètre filament) × courbure totale

Tant qu'elle n'est pas parcourue, ADVANCE, SMOOTH_TIME et LEAD_TIME poussent dans
le vide : leur consigne est avalée par le jeu et n'atteint jamais la colonne.
C'est un **activateur**, pas un compensateur — seul il ne dépose rien.

La couche parcourt ce jeu **en avance**, de sorte qu'il soit refermé exactement
quand la vraie extrusion commence. C'est un lead qui ne décale que le **bord** de
la pente, pas la pente entière, et de la distance du jeu et non d'un temps.

- L'opérateur déclare une **géométrie**, jamais un jeu : `bowden_length` (0 =
  couche éteinte, c'est le défaut) et `bowden_id`. Le reste se déduit.
- `backlash_coef` est le bouton d'expérimentation : il multiplie le jeu calculé
  (2 le double, 0,5 le divise), se règle **en direct** comme le flow.
- `bowden_turns` (défaut 1) est l'hypothèse de routage, déclarée et non cachée.
  Le nombre de coudes d'un cheminement **ne croît pas avec la longueur** — c'est
  pourquoi la longueur ne fait que plafonner θ, jamais le fixer.
- La rampe dure `jeu / backlash_speed`, soit ~13 ms : très en deçà des 100 ms que
  le lissage réclame couramment. Une rampe plus longue est **refusée avec un
  message**, jamais rognée en silence.

**Le sens est horodaté par le planner**, pas deviné par la cinématique : une
fonction de position n'a pas de mémoire, et après une pause elle ne peut plus
savoir d'où vient le filament sans lire des moves peut-être déjà libérés — la
faute exacte qui produit « Invalid sequence ». `process_move` voit les mouvements
dans l'ordre, donc il sait ; il empile un jalon comme le fait `pa_list`.

Conformité vérifiée par un banc C autonome (14 contrôles) : neutralité à jeu nul,
offset nul loin de l'inversion, rampe à mi-course, **valeur atteinte pile à
l'instant de l'inversion**, maintien pendant la traction, et fenêtre réclamée.

**Ni le jeu ni τ ne sont mesurés sur une machine à ce jour** : `backlash_coef` se
cale à l'impression, c'est sa raison d'être.

### Les arrêts machine rencontrés (et ce qui les a fixés)

Cinq classes de crash « Internal error in stepcompress » / « Invalid sequence »
rencontrées sur la couche de rattrapage, dans l'ordre. La règle d'or transversale :
la position commandée doit rester **continue**, et toute fenêtre élargie exige une
resync — deux propriétés à **imposer**, jamais à supposer.

1. **Faux jalons qui se chevauchent** (`b38e1ef`) — la résorption proportionnelle
   posait jusqu'à ~450 jalons/seconde ; ils se chevauchaient, l'offset tremblait.
   Fix : un nombre FIXE de 8 paliers (`BACKLASH_BLEED_STEPS`).
2. **Chaque jalon porte sa propre rampe** (`0bf594f`) — interpoler un vieux jalon
   avec la rampe COURANTE (vitesse/accel changées entre-temps) faisait sauter
   l'offset. Fix : `struct backlash_params` embarque son `ramp`.
3. **Offset non nul au changement de paramètre** (`95e3217`) — retoucher un
   paramètre alors que l'offset n'est pas à zéro faisait sauter la position.
   Fix : `_apply_backlash` ramène l'offset à zéro PAR SA RAMPE avant tout
   changement, si la cible est non nulle.
4. **Fenêtre de scan non resynchronisée** (`e98c567`) — `extruder_set_backlash`
   réécrit `gen_steps_pre/post_active` (même champ C que le `smooth_time` du PA)
   sans appeler `motion_queuing.check_step_generation_scan_windows()` :
   `kin_flush_delay` restait figé sur la fenêtre précédente, plus étroite, et
   l'historique trapq était purgé sous les pas. **Ne jamais** élargir
   `gen_steps_pre/post_active` sans resync — le même piège que `lead_time`.
5. **Recouvrement des fenêtres de rampe** (`63e3694`, `2070b5d` ; repro bout-en-bout
   `500857b`) — `backlash_lookup` suppose que la rampe vers un jalon a **atterri**
   avant que celle du suivant ne démarre. Sur un changement de couche dense
   (retrait, reprise à −deduct, paliers de bleed), deux jalons tombent à moins
   d'une rampe l'un de l'autre : au `print_time` du jalon du milieu, la paire
   active bascule et l'offset SAUTE (1,15 mm mesurés au banc, 460 pas) →
   stepcompress refuse la discontinuité. Fix : l'invariant est imposé **à
   l'insertion** — si la fenêtre du nouveau jalon recouvre l'atterrissage du
   précédent, celui-ci est supplanté quand sa fenêtre est entièrement dans le
   futur (fusion) ; sinon la nouvelle rampe est raccourcie pour démarrer pile à
   son atterrissage. Les rampes ne font que rétrécir : la fenêtre de scan
   déclarée reste valide, aucune resync requise. Couvert par
   `scripts/backlash_overlap_bench.c` (banc de continuité, seuil relatif au bras
   de référence du même run) et `test/klippy/backlash_layer_change.test`
   (scénario dense de changement de couche — rouge sans le fix avec la signature
   exacte de production, vert avec).

### Ce que fait lead_time
- `lead_time` (config `[extruder]` ou `SET_PRESSURE_ADVANCE EXTRUDER=... LEAD_TIME=`)
  décale dans le temps le contenu du trapq extrudeur au niveau **planner**
  (`process_move` append à `print_time - lead`). Pur décalage temporel → total
  d'extrusion conservé, débit constant (pas de surépaisseur d'angle comme le PA).
- `motion_queuing.py` élargit la fenêtre de scan (`kin_flush_delay`) de `lead` pour
  rester sync-safe sur les feeders synchronisés (YMS). **Ne jamais** élargir
  `gen_steps_pre/post_active` → crash « Invalid sequence ».
- `smooth_time` découplé du PA : actif si `pressure_advance > 0` **OU** `lead > 0`,
  donc le smooth lisse les rampes à PA=0 sans fake PA.

Config validée en réel : `pressure_advance: 0` + `pressure_advance_smooth_time: 0.04`
+ `lead_time: 0.03`.

---

## 3. Mettre à jour depuis l'upstream (le rebase)

```bash
git fetch upstream
git checkout master
git rebase upstream/master
```

- **Aucun conflit** (upstream n'a pas touché `extruder.py` / `motion_queuing.py`)
  → terminé, nos commits sont remis au sommet automatiquement.
- **Conflit** (upstream a touché les mêmes lignes que nos blocs `# YUMI:`) →
  git montre exactement où. On résout en gardant la logique YUMI sur la **nouvelle**
  version upstream, puis :
  ```bash
  git add <fichier>
  git rebase --continue
  ```

Puis on garde la branche jumelle `yumi` alignée et on pousse :
```bash
git branch -f yumi master
git push --force-with-lease origin master yumi
```

> `--force-with-lease` parce que le rebase réécrit l'historique au-dessus de
> l'upstream. C'est attendu et sûr tant qu'on est les seuls à pousser.

---

## 4. Vérifier après rebase

```bash
python3 -m py_compile klippy/kinematics/extruder.py klippy/extras/motion_queuing.py
gcc -fsyntax-only -I klippy/chelper klippy/chelper/kin_extruder.c
git diff upstream/master..master --stat   # doit rester nos fichiers + le delta YUMI
```
Puis test réel : une impression + 0 erreur sync watchdog.

Vérifier aussi que le rattrapage est bien **neutre à zéro** — c'est ce qui
permet de la laisser en place sans risque :
```bash
python3 scripts/test_klippy.py test/klippy/pressure_advance.test
```
Le cas de test se termine par `BACKLASH_COEF=0` suivi d'un mouvement : si le chemin
d'origine n'est pas restauré exactement, ça se voit là.

---

## 5. Proposer le patch à l'upstream (optionnel)

La branche `yumi` sert à produire un PR propre vers `Klipper3d/klipper` :
```bash
git format-patch upstream/master..yumi
```
Ne pas en dépendre : l'upstream est très sélectif sur la cinématique extrudeur.
Le fork reste notre source de vérité quoi qu'il arrive.

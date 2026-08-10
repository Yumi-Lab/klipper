# Yumi-Lab/klipper — patches Yumi

Ce dépôt est un **fork du Klipper officiel** (`Klipper3d/klipper`). Notre version =
Klipper officiel + un petit nombre de commits Yumi, gardés **au sommet** et
**rebasés** sur l'upstream à chaque montée de version. C'est ça la « source de
vérité » : les pads clonent ce dépôt (`master`), il n'y a plus aucune copie de
fichier `.py` faite ailleurs (avant, yumi-config recopiait le patch — supprimé).

Les patches sont **côté host** — **aucun flash MCU**, jamais. Sur le pad :
`git pull` + `systemctl restart klipper`, c'est tout.

⚠️ **Depuis la couche CHARGE, `kin_extruder.c` n'est plus stock.** Ce fichier est du
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
grep -rn "// YUMI" klippy/chelper/  # couche CHARGE (C hôte)
```

---

## 2. Les patches actuels

| Patch | Fichiers | Type |
|---|---|---|
| **extruder lead_time** (anticipation / coast) | `klippy/kinematics/extruder.py`, `klippy/extras/motion_queuing.py` | core patché, pur-Python |
| **couche CHARGE bowden** | `klippy/chelper/kin_extruder.c`, `klippy/chelper/__init__.py`, `klippy/kinematics/extruder.py` | core patché, **C hôte** |
| **YUMI_CONFIG** (constante gravée au build, lue host-side via mcu_constants) | firmware build | constante |

### Ce que fait la couche CHARGE
Sur un bowden, la colonne de filament est élastique : elle emmagasine de la matière
à l'aller et la rend au retour. Le pressure advance compense ça par un terme
**instantané** (`pa × vitesse`), qui exige une vitesse moteur d'autant plus grande
que le tube — donc la constante de temps — est long.

La charge est la même compensation **étalée** sur une fenêtre `T_c` :

    charge(t) = coef × ( position_nominale(t + T_c) − position_nominale(t) )

- Couche **strictement additive** : `CHARGE_COEF=0` (ou une fenêtre nulle) rend le
  chemin d'origine **bit pour bit**. On peut donc la laisser configurée et éteinte.
- En croisière elle délivre `coef × v × T_c`. `T_c → 0` dégénère en pressure advance
  de valeur `coef × T_c` ; `T_c = τ` reproduit `lead_time`. Ce ne sont pas trois
  méthodes rivales mais une seule famille.
- Pointe moteur bornée à `débit × (1 + coef)` au lieu de croître sans limite.
- Elle lit la trajectoire **nominale**, pas celle du pressure advance : les deux
  couches compensent des physiques différentes et ne doivent pas se nourrir l'une
  l'autre.
- Réglée par une **distance** (mm) et une **vitesse** (mm/s), jamais par un temps :
  `T_c = CHARGE_DIST / CHARGE_SPEED`. C'est ce qu'un opérateur sait mesurer sur une
  machine. Les trois paramètres se changent **en direct** par
  `SET_PRESSURE_ADVANCE`, pour corriger l'amplitude en cours d'essai.

Modèle validé dans `docs/lead-time-simulator.html` (dépôt YumiOS-Klipper-V2), et
conformité du firmware au modèle vérifiée par un banc C autonome (25 contrôles).
**τ n'est mesuré sur aucune machine à ce jour** : le coefficient se cale à l'essai.

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

Vérifier aussi que la couche CHARGE est bien **neutre à zéro** — c'est ce qui
permet de la laisser en place sans risque :
```bash
python3 scripts/test_klippy.py test/klippy/pressure_advance.test
```
Le cas de test se termine par `CHARGE_COEF=0` suivi d'un mouvement : si le chemin
d'origine n'est pas restauré exactement, ça se voit là.

---

## 5. Proposer le patch à l'upstream (optionnel)

La branche `yumi` sert à produire un PR propre vers `Klipper3d/klipper` :
```bash
git format-patch upstream/master..yumi
```
Ne pas en dépendre : l'upstream est très sélectif sur la cinématique extrudeur.
Le fork reste notre source de vérité quoi qu'il arrive.

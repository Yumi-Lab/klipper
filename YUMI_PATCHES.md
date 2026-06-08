# Yumi-Lab/klipper — patches Yumi

Ce dépôt est un **fork du Klipper officiel** (`Klipper3d/klipper`). Notre version =
Klipper officiel + un petit nombre de commits Yumi, gardés **au sommet** et
**rebasés** sur l'upstream à chaque montée de version. C'est ça la « source de
vérité » : les pads clonent ce dépôt (`master`), il n'y a plus aucune copie de
fichier `.py` faite ailleurs (avant, yumi-config recopiait le patch — supprimé).

Les patches sont **100 % Python, côté host**. `kin_extruder.c` reste **STOCK** →
**aucune compilation, aucun flash MCU**. Sur le pad : `git pull` + `systemctl
restart klipper`, c'est tout.

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
grep -rn "# YUMI:" klippy/
```

---

## 2. Les patches actuels

| Patch | Fichiers | Type |
|---|---|---|
| **extruder lead_time** (anticipation / coast) | `klippy/kinematics/extruder.py`, `klippy/extras/motion_queuing.py` | core patché, pur-Python |
| **YUMI_CONFIG** (constante gravée au build, lue host-side via mcu_constants) | firmware build | constante |

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
git diff upstream/master..master --stat   # doit rester nos 2 fichiers + le delta YUMI
```
Puis test réel : une impression + 0 erreur sync watchdog.

`kin_extruder.c` doit rester STOCK (le patch est pur-Python — si tu dois toucher
du C, c'est que quelque chose a dérivé).

---

## 5. Proposer le patch à l'upstream (optionnel)

La branche `yumi` sert à produire un PR propre vers `Klipper3d/klipper` :
```bash
git format-patch upstream/master..yumi
```
Ne pas en dépendre : l'upstream est très sélectif sur la cinématique extrudeur.
Le fork reste notre source de vérité quoi qu'il arrive.

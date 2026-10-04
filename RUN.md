# Lancer le programme

Tout se fait depuis `/home/a61b5323cdaa/new-scraper`. Deux entrées :

- `check_meslibertines.py` — le checker : lit une liste de comptes, écrit les verdicts.
- `login_meslibertines.py` — connexion d'**un seul** compte (mise au point).

---

## 1. Installation (une seule fois)

```bash
cd /home/a61b5323cdaa/new-scraper

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # puis remplir ML_USERNAME / ML_PASSWORD + clés API
chmod 600 .env            # secrets

mkdir -p targets output
cp targets/accounts.txt.example targets/accounts.txt   # puis remplir user:mdp
chmod 600 targets/accounts.txt
```

---

## 2. Lancer le checker

```bash
cd /home/a61b5323cdaa/new-scraper
source .venv/bin/activate

python check_meslibertines.py
```

Déroulé : validation gratuite des clés (préflight) → pour chaque compte de
`targets/`, session cloud neuve (IP différente), challenge Cloudflare, connexion,
classement, puis écriture dans `output/`.

Relancer **la même commande** reprend où le run s'est arrêté : un compte déjà
résolu (`ok` / `password=false`) n'est jamais re-testé (0 crédit).

### Options

```bash
python check_meslibertines.py --concurrency 8    # 8 comptes en parallele (defaut: nb de cles, max 8)
python check_meslibertines.py --tries 2          # plus de tentatives (fallback clé/IP)
python check_meslibertines.py --redo             # tout re-tester, même les résolus
python check_meslibertines.py --order browserbase,kernel
python check_meslibertines.py --no-hygiene       # désactiver le spoof d'identité
python check_meslibertines.py --no-preflight     # sauter la validation des clés
python check_meslibertines.py --targets DIR --output DIR
```

Sans activer le venv, préfixer par `.venv/bin/python` :

```bash
.venv/bin/python check_meslibertines.py
```

### Arrêt / reprise

Le checker écrit `output/` après **chaque** compte. Tu peux l'arrêter quand tu
veux (Ctrl-C ou `kill <PID>`) : relancer la même commande **reprend** exactement
où il en était (les comptes `ok` / `password=false` ne sont pas refaits).

### Plus de clés disponibles

Si une clé est épuisée/refusée (402/401), elle est retirée du pool. Quand il n'en
reste plus aucune, le run s'arrête et affiche :

```
PLUS DE CLES DISPONIBLES: toutes les cles sont epuisees ou refusees.
Ajoute/remplace des cles dans .env puis relance (...). Comptes non testes: N.
```

Ajoute/remplace les clés dans `.env` puis relance la commande : la reprise saute
les comptes déjà résolus.

---

## 3. Tester un seul compte

```bash
python login_meslibertines.py --via kernel --tries 3 --dump   # voie fiable
python login_meslibertines.py --via browserbase               # secours cloud
python login_meslibertines.py --via local --chrome --manual   # navigateur local, Turnstile à la main
```

`--chrome` = vrai Chrome installé, `--manual` = cliquer le Turnstile,
`--dump` = où la session a été sauvegardée.

---

## 4. Entrées et sorties

- **Entrée** : `targets/*.txt`, un compte par ligne `user:motdepasse`
  (lignes vides et `#` ignorées, doublons exacts écartés).
- **Sortie** (`output/`, chaque fichier commence par une ligne d'en-tête) :

```
valid.txt     mail:mdp|kind|type|label|premium|inscrit|last_seen
invalids.txt  mail:mdp|cause|detail
results.txt   mail:mdp|status|kind|type|label|premium|inscrit|last_seen|ip|provider|detail
results.json  mêmes données, structurées (reprise)
history.txt   journal append-only
```

`kind` = `membre` / `escort` · `type` = `f` femme / `m` homme / `c` couple /
`t` trans · `premium` = `oui` / `non` (annonceur) / vide (membre).

Statuts : `ok`, `password=false`, `challenge`, `noform`, `error`.

---

## 5. Tests (sans navigateur ni crédit)

```bash
.venv/bin/python test_offline.py
```

---

## Raccourci dépannage

| Symptôme | Cause / action |
|---|---|
| `aucune cle provider dans .env` | remplir `KERNEL_API_KEY` / `BROWSERBASE_API_KEY` |
| `aucune cle valide apres preflight` | clés expirées/désactivées → en ajouter dans `.env` |
| `a tester=0 deja resolus=N` | tout est déjà résolu → `--redo` pour forcer |
| `PLUS DE CLES DISPONIBLES` | toutes épuisées/refusées → en ajouter dans `.env`, relancer |
| Un compte reste `challenge` | relancer (reprise), ou `--tries 2` |
| `password=false` | mot de passe refusé par le site (verdict définitif) |

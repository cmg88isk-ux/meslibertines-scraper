# MesLibertines Checker

Vérificateur de comptes pour `meslibertines.com`.

Lit une liste de comptes, ouvre un navigateur cloud neuf pour chacun, franchit le
challenge Cloudflare/Turnstile, se connecte, puis classe le compte : nature
(`membre` client, `escort` annonceur ou `multi` gestion), type (`femme` / `homme`
/ `couple` / `trans`), abonnement `premium` payé ou non, date d'inscription et
dernière connexion. Un mot de passe refusé est écrit `password=false`, preuve du
site à l'appui. Les navigateurs vivent chez Kernel et Browserbase, pilotés en
CDP : aucun navigateur local n'est requis.

---

## Sommaire

- [Ce que fait le programme](#ce-que-fait-le-programme)
- [Architecture](#architecture)
- [Installation](#installation)
- [Utilisation](#utilisation)
- [Format des résultats](#format-des-résultats)
- [Types de comptes](#types-de-comptes)
- [Modèle anti-détection](#modèle-anti-détection)
- [Résilience](#résilience)
- [Tests](#tests)
- [Sécurité](#sécurité)

---

## Ce que fait le programme

Pour chaque compte de `targets/` :

1. ouvre une session cloud neuve (donc une **nouvelle IP**) ;
2. applique une identité propre à la session (User-Agent calé sur le vrai build
   Chrome, Client Hints cohérents, locale/timezone, assets bloqués) ;
3. charge `meslibertines.com/users/login/` et attend que le challenge Cloudflare
   passe ;
4. masque l'overlay d'avertissement majeur (`#windiv-confirm`) ;
5. remplit `#user` / `#passwd` et soumet ;
6. si le site répond *« Nom d'utilisateur ou mot de passe invalide! »*, écrit
   `password=false` ; sinon récupère le dash et le profil public et classe le
   compte (nature, type, premium, dates) ;
7. écrit une ligne dans `output/valid.txt` (ou `invalids.txt`) et la table
   complète dans `output/results.txt` / `output/results.json`.

## Architecture

Chaque module a une responsabilité unique.

| Module | Rôle |
|---|---|
| `config.py` | Lit `.env`, rassemble le pool de clés d'un fournisseur |
| `transports.py` | Abstraction fournisseur : une clé → un Chrome joignable en CDP |
| `hygiene.py` | Identité de session (UA + hints + timezone), blocage des assets |
| `login_meslibertines.py` | Connexion d'un compte (cloud ou navigateur local) |
| `meslibertines_profile.py` | Texte de profil → nature + type + premium + dates (fonctions pures) |
| `check_meslibertines.py` | Orchestrateur : `targets/` → `output/`, rotation IP/provider |
| `test_offline.py` | Tests, sans navigateur ni crédit |

Point clé de `transports.py` : **Kernel et Browserbase sont interchangeables**.
Les deux exposent une URL CDP, donc le même code sert pour les deux. Aucun SDK
propriétaire n'est requis à l'exécution.

```
targets/*.txt
      │
      ▼
  config.py ──── pool de clés
      │
      ▼
  transports.py ──── Kernel / Browserbase  ──►  Chrome en CDP
      │                                              │
      │                                    hygiene.py (identité)
      ▼                                              │
  login_meslibertines.py  ◄──────────────────────────┘
      │
      ▼
  meslibertines_profile.py  ──►  check_meslibertines.py  ──►  output/
```

## Installation

```bash
cd new-scraper

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # puis remplir les clés
chmod 600 .env            # les clés sont des secrets

mkdir -p targets output
cp targets/accounts.txt.example targets/accounts.txt   # puis remplir user:motdepasse
chmod 600 targets/accounts.txt
```

`.env` :

| Variable | Rôle |
|---|---|
| `ML_USERNAME` / `ML_PASSWORD` | compte unique utilisé par `login_meslibertines.py` |
| `KERNEL_API_KEY` | Chrome furtif Kernel (voie fiable). Pool : `KERNEL_API_KEY_2`, `_3`, ... |
| `BROWSERBASE_API_KEY` | Chrome cloud Browserbase (secours, fingerprint aléatoire). Pool : `_2`, `_3`, ... |
| `WEBSHARE_API_TOKEN` | optionnel, proxy résidentiel pour le mode local |

Plusieurs clés peuvent coexister pour chaque fournisseur (`..._API_KEY`,
`..._API_KEY_2`, `_3`, ...) : elles forment un pool. Au démarrage, chaque clé est
validée gratuitement (`GET /browsers` côté Kernel, `GET /v1/projects` côté
Browserbase) ; une clé désactivée (401) est écartée, une clé qui répond est
conservée. Les comptes sont répartis sur le pool et un échec transitoire bascule
sur la clé suivante, puis sur l'autre fournisseur. Toutes les clés valides
restent dans la chaîne de fallback.

`targets/*.txt` : un compte par ligne, `user:motdepasse`. Les lignes vides et
les lignes commençant par `#` sont ignorées, les doublons exacts aussi.
`targets/` et `output/` sont ignorés par git.

## Utilisation

```bash
# Vérifier tous les comptes de targets/  -> output/  (reprise automatique)
python check_meslibertines.py

# Plus de tentatives par compte (nouvelle IP à chaque essai)
python check_meslibertines.py --tries 2

# Plusieurs comptes en parallele (N workers = N cles; Kernel plafonne a 5 sessions)
python check_meslibertines.py --concurrency 4

# Tout re-tester, même les comptes déjà résolus
python check_meslibertines.py --redo

# Replier sur Browserbase en premier (fingerprint aléatoire)
python check_meslibertines.py --order browserbase,kernel

# Désactiver le spoof d'identité (debug)
python check_meslibertines.py --no-hygiene

# Sauter le préflight des clés (déconseillé)
python check_meslibertines.py --no-preflight
```

**Reprise = économie de crédits.** Un compte déjà résolu (`ok` ou
`password=false`) n'est **jamais** re-testé : relancer le checker ne paie que ce
qui reste. La sortie est écrite après **chaque** compte (sous verrou), donc un
Ctrl-C ou un `kill` conserve tout ce qui est déjà traité ; il suffit de relancer.
Par défaut `--tries 2` : la seconde session n'est ouverte que pour un blocage
transitoire (un mot de passe refusé s'arrête au premier essai, sans fallback
inutile). Les échecs transitoires (`challenge`, `error`) ne sont pas enregistrés
comme définitifs et reviennent au prochain run, sans repayer les comptes réussis.

**Parallélisme.** `--concurrency N` fait tourner N comptes en même temps, chacun
sur une clé/IP distincte. Kernel plafonne à **5 sessions simultanées par org**,
donc le défaut est `min(nb de clés, 4)`. Au-delà, l'API renvoie 429 : le checker
met alors en pause et réessaie (il ne marque pas le compte en échec).

**Sessions orphelines.** Un arrêt dur (kill) n'exécute pas le code de fermeture
et laisse des sessions Kernel ouvertes, qui saturent le plafond de 5 et font
échouer toutes les nouvelles en 429. Au démarrage, le préflight **libère ces
sessions**. Un verrou `output/.lock` empêche aussi deux runs simultanés de se
marcher dessus.

**Plus de clés.** Si une clé répond 402/401 (crédits épuisés ou refusée), elle
est retirée du pool en cours de route. Quand il n'en reste aucune, le run
s'arrête et affiche `PLUS DE CLES DISPONIBLES: ...` avec le nombre de comptes non
testés ; ajoute/remplace des clés dans `.env` et relance (la reprise ne refait
pas les comptes déjà résolus).

**Préflight.** Avant tout lancement, chaque clé du pool est validée par une
lecture gratuite (`GET /browsers` côté Kernel). Une clé désactivée est écartée
immédiatement ; aucune session n'est ouverte tant que le pool n'est pas sain.

Connexion d'un seul compte (outil de mise au point) :

```bash
python login_meslibertines.py --via kernel --tries 3 --dump   # voie fiable
python login_meslibertines.py --via browserbase               # secours
python login_meslibertines.py --via local --chrome --manual   # navigateur local, Turnstile manuel
```

`--chrome` pilote le vrai Chrome installé, `--manual` laisse cliquer le
Turnstile à la main (utile sur IP résidentielle), `--dump` indique où la session
a été sauvegardée.

## Format des résultats

Cinq fichiers dans `output/`, réécrits après chaque compte. Chaque fichier
commence par une **ligne d'en-tête** indiquant les colonnes :

| Fichier | Contenu |
|---|---|
| `valid.txt` | comptes OK : `mail:mdp\|kind\|type\|label\|premium\|jours_vip\|inscrit\|last_seen` |
| `premium.txt` | annonceurs premium actifs : `mail:mdp\|kind\|type\|label\|premium\|jours_vip` |
| `invalids.txt` | comptes non OK : `mail:mdp\|cause\|detail` |
| `results.txt` | table complète : `mail:mdp\|status\|kind\|type\|label\|premium\|jours_vip\|inscrit\|last_seen\|ip\|provider\|detail` |
| `results.json` | mêmes lignes, structurées (sert aussi à la reprise) |
| `history.txt` | journal append-only, jamais tronqué (`date\|mail:mdp\|status\|kind\|type\|premium\|jours_vip\|ip\|provider\|detail`) |

`kind` = `membre` ou `escort`. `premium` = `oui` / `non` pour un compte
annonceur (bloc `Abonnement` du dash : `.free-package` → `non`, paquet actif →
`oui`), vide pour un membre (pas d'abonnement). `jours_vip` = nombre de jours
VIP restants pour un annonceur premium (compte à rebours du site, sinon date de
fin convertie en jours), vide sinon. `premium.txt` ne garde que les annonceurs à
paquet actif (`premium=oui`) avec leurs jours VIP (`0` = paquet épuisé).

`invalids.txt` distingue la cause : `password=false` (refus franc du site,
preuve à l'appui), `challenge` (Cloudflare non franchi, rejouable), `noform`,
`error`. Seuls `ok` et `password=false` sont définitifs et donc sautés à la
reprise.

Exemple **fictif** :

```
# valid.txt
mail:mdp|kind|type|label|premium|inscrit|last_seen
EudesFre:********|membre|m|homme||11/01/2023|04/10/2026 16:38
simerius:********|escort|f|femme|non|

# invalids.txt
mail:mdp|cause|detail
quelquun@example.com:********|password=false|identifiants invalides (site)
autre@example.com:********|challenge|
```

Dans `results.txt`, les champs sont : couple testé, statut, nature (`kind`),
code type, libellé, premium, inscription, dernière connexion, IP de sortie,
provider + 6 derniers caractères de la clé, détail. `password=false` n'est écrit
que si le formulaire s'est bien rendu et que le site a répondu par son message
d'échec : un challenge non résolu ne peut donc pas être confondu avec un mauvais
mot de passe.

## Types de comptes

Le parser reconnaît tout ce que le site expose. La **nature** se lit à
l'atterrissage après connexion (et sur l'URL du profil) :

| Nature (`kind`) | Dashboard | Profil | Type |
|---|---|---|---|
| `membre` (client) | `/member_dashes/` | `/member/<slug>/` | genre du compte |
| `escort` (annonceur) | `/profiles/dash/` | `/escort/<slug>-<id>/` | genre de l'annonce + premium |
| `multi` (gestion multi-escortes) | `/multi_dashes/` | — | pas de genre unique |

Le sous-type vient du genre déclaré (`data[gender]` du formulaire d'édition pour
un membre, panneau `Sexe:` du profil public sinon), avec les **mêmes codes que le
site** :

| Code | Libellé |
|---|---|
| `f` | femme |
| `m` | homme |
| `c` | couple |
| `t` | trans (Transexuelle) |

Le **premium payé** ne concerne que les annonceurs : il est lu dans le bloc
`Abonnement` du dash (`non` si `.free-package` « vous ne disposez pas d'un
paquet », `oui` si un paquet est actif, vide pour un membre). Quand un paquet est
actif, le **nombre de jours VIP restants** (`jours_vip`) est lu au même endroit :
compte à rebours « X jours restants » si présent, sinon date de fin
(« Expire le JJ/MM/AAAA ») convertie en jours depuis aujourd'hui.

La meta description est ignorée volontairement : pour un profil trans elle
indique tout de même « femme », seul le panneau `Sexe:` fait foi.

## Modèle anti-détection

**IP qui change.** Chaque tentative ouvre une session neuve. **Kernel** sort
d'une IP unique à chaque session (mesuré : 5 sessions = 5 IP distinctes,
`verify_rotation.py`). **Browserbase** (plan gratuit) partage un pool datacenter :
ses IP peuvent se répéter d'une session à l'autre — la rotation n'y est pas
garantie, d'où Kernel en provider principal. Le checker ne réutilise jamais la
clé/IP qui vient d'échouer (`KeyPool.ordered`).

**Fingerprint.** Le User-Agent n'est **jamais** forcé sur une version plus
ancienne que le navigateur réel : ce désaccord faisait boucler le challenge
Turnstile. `hygiene.apply_identity` injecte à la place un UA épinglé au vrai
build Chrome, avec des Client Hints cohérents, une locale/timezone française et
le blocage des analytics/polices/médias (`Network.setBlockedURLs`). La session
est tirée **une seule fois** (plateforme, locale, timezone, viewport) côté
provider, puis transmise à `apply_identity` : les deux couches ne peuvent donc
pas se contredire. `verify_rotation.py` mesure le résultat (5 sessions Kernel :
5 IP et 5 fingerprints distincts, `navigator.webdriver` absent).

**Rotation des providers.** Le checker alterne Kernel et Browserbase, et les
clés entre elles : une nouvelle tentative ne réutilise jamais l'IP qui vient
d'échouer.

**Note :** ce sont des mesures d'hygiène, pas une garantie. Rien ne garantit un
blocage zéro.

## Résilience

- **Challenge Cloudflare non résolu** : la tentative repart sur une session
  neuve (autre IP) jusqu'à `--tries`, puis `challenge` est écrit.
- **Mauvais mot de passe** : verdict définitif `password=false`, pas de retry
  inutile — confirmé par le message du site.
- **Provider en erreur** (429 Browserbase, session perdue) : la tentative
  repart, l'échec d'un compte n'interrompt pas le run.
- **Clé épuisée** : retirée du pool dès le 402/401 ; quand toutes le sont, le run
  s'arrête avec `PLUS DE CLES DISPONIBLES` et le nombre de comptes restants.
- **Reprise** : les fichiers de `output/` sont réécrits après chaque compte, et
  `history.txt` est append-only. Un Ctrl-C/`kill` ne perd rien et un relaunch
  saute les comptes déjà résolus (`ok` / `password=false`), donc ne repaie pas
  les crédits déjà dépensés.
- **Refus franc** : dès que le site imprime son message d'invalidité, la
  tentative s'arrête sans attendre la fin du délai (économie de minutes).

Sur cette machine (IP datacenter), **Kernel est la voie fiable** : 4/4 sessions
atteignent le formulaire en 6-9 s. Browserbase est intermittant (2/4, puis 429)
et sert de secours.

## Tests

```bash
python test_offline.py
```

Sans navigateur ni crédit :

- classement rejouable/non rejouable des erreurs HTTP des transports ;
- reconnaissance des types de comptes (`membre`/`escort`, `f`/`m`/`c`/`t`) ;
- détection du premium payé (`oui`/`non`/vide) ;
- jours VIP restants (compte à rebours ou date de fin convertie en jours) ;
- la meta description ne l'emporte jamais sur le panneau `Sexe:` ;
- extraction de la date d'inscription et de la dernière connexion ;
- dédup des cibles, reprise par couple `(user, password)`, et sorties qui ne
  perdent pas les verdicts précédents (`history.txt` append-only).

## Sécurité

- `.env`, `targets/`, `output/`, `meslibertines-profile/` et
  `meslibertines-state.json` sont ignorés par git. **Vérifiez `git status` avant
  le premier commit.**
- `chmod 600` sur `.env` et `targets/accounts.txt`.
- Aucun secret n'est écrit dans le dépôt ; les clés n'apparaissent jamais en
  clair dans les logs (6 derniers caractères seulement).
- Les résultats contiennent de vrais identifiants et de vraies IP : gardez le
  dépôt privé.

## Licence

Usage privé. Ne pas distribuer.

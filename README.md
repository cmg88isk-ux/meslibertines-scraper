# Yomoni Scraper

Scraper multi-comptes pour l'espace client `my.yomoni.fr`.

Lit une liste de comptes, se connecte à chacun, parcourt l'espace membre et
produit **une ligne de résultat par compte**. Les navigateurs vivent chez
Browserbase et Kernel, pilotés en CDP : le code ne dépend d'aucun navigateur
local.

État actuel : **22 comptes traités** (23 lignes : une adresse porte deux mots de
passe différents, tous deux essayés), 8 réussis, 16 rejetés, 0 en attente.

---

## Sommaire

- [Ce que fait le programme](#ce-que-fait-le-programme)
- [Architecture](#architecture)
- [Installation](#installation)
- [Utilisation](#utilisation)
- [Format des résultats](#format-des-résultats)
- [Définitif contre transitoire](#définitif-contre-transitoire)
- [Modèle anti-détection](#modèle-anti-détection)
- [Résilience](#résilience)
- [Vérifier avant de lancer](#vérifier-avant-de-lancer)
- [Tests](#tests)
- [Sécurité](#sécurité)

---

## Ce que fait le programme

Pour chaque compte de `credentials.txt` :

1. ouvre un navigateur cloud neuf (flush complet des cookies) ;
2. applique une identité propre à la session (IP, User-Agent, langues) ;
3. se connecte sur `my.yomoni.fr/sign-in` ;
4. parcourt `/home`, `/profile`, `/notifications`, `/news` et l'assistant de
   souscription s'il y en a un ;
5. extrait produit, montant, adresse, téléphone et detalle ;
6. écrit **une ligne** dans `result/yomoni_results.txt` et un dump JSON complet
   dans `result/dumps/`.

Un compte qui n'aboutit pas est enregistré avec un marqueur explicite
(`mot de passe changé`, `2FA requise`) : on sait toujours *pourquoi* il est vide.

## Architecture

Chaque module a une responsabilité unique, ce qui permet de remplacer une pièce
sans toucher aux autres.

| Module | Rôle |
|---|---|
| `config.py` | Lit `.env`, rassemble le pool de clés d'un fournisseur |
| `transports.py` | Abstraction fournisseur : une clé → un Chrome joignable en CDP |
| `hygiene.py` | Flush, identité de session, blocage des assets |
| `yomoni.py` | Connexion et parcours du site, indépendant du fournisseur |
| `extract.py` | Texte capturé → une ligne de résultat (fonctions pures) |
| `scrape_multi.py` | Orchestrateur : concurrence, rotation, persistance |
| `probe_reach.py` | Teste quelles clés atteignent vraiment le site |
| `check_credits.py` | État des crédits de chaque fournisseur |
| `test_offline.py` | Tests, sans navigateur ni crédit |

Le point clé de `transports.py` : **Browserbase et Kernel sont interchangeables**.
Les deux expose une URL CDP, donc le même code de connexion sert pour les deux.
Aucun SDK propriétaire n'est requis à l'exécution.

```
credentials.txt
      │
      ▼
  config.py ──── pool de clés
      │
      ▼
  transports.py ──── Browserbase / Kernel  ──►  Chrome en CDP
      │                                              │
      │                                    hygiene.py (flush + identité)
      ▼                                              │
  yomoni.py  (connexion + parcours)  ◄────────────────┘
      │
      ▼
  extract.py  ──►  scrape_multi.py  ──►  result/
```

## Installation

```bash
git clone https://github.com/cmg88isk-ux/yomoni-scraper
cd yomoni-scraper

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # puis remplir les clés
chmod 600 .env            # les clés sont des secrets

cp credentials.txt.example credentials.txt   # puis remplir email:motdepasse
chmod 600 credentials.txt
```

`credentials.txt` : un compte par ligne, `email:motdepasse`, sans espace autour
du `:`. Les doublons exacts et les lignes de commentaire sont ignorés. Si une
adresse apparaît avec deux mots de passe différents, **les deux sont essayés** :
c'est la façon de tester un mot de passe corrigé sans perdre l'historique.

## Utilisation

```bash
# Lancer le traitement (reprend où il s'était arrêté)
python scrape_multi.py

# Voir le plan sans dépenser le moindre crédit
python scrape_multi.py --dry-run

# Revérifier un compte précis, en ignorant le verdict mémorisé
python scrape_multi.py --account someone@example.com

# Tout revérifier
python scrape_multi.py --all

# Retester aussi les comptes bloqués par 2FA
python scrape_multi.py --retry-failed

# Limiter à N comptes, pour valider la chaîne de bout en bout
python scrape_multi.py --limit 2
```

La reprise est automatique : relancer le programme ne reprocesse jamais un
compte déjà résolu. Le `--dry-run` est le bon réflexe avant un run long.

## Format des résultats

`result/yomoni_results.txt`, une ligne par compte, 6 champs séparés par `|`,
dans cet ordre :

```
email | mot de passe | 4 derniers chiffres | adresse | produit | détail
```

Exemple **fictif**, avec la forme exacte d'une vraie ligne :

```
prenom.nom@example.com|********|||oui|type: assurance-vie (Épargne - Assurance-vie); montant 0,00 €; versement +0,00 €; souscription 1/5 Projet; maj 10/09/2025 à 17h00
autre.compte@example.com|********|6241|12 rue Exemple, 75000, PARIS|non|type: aucun produit (ni assurance-vie, ni epargne immobiliere); solde 0,00 €; versement +0,00 €
```

| Champ | Contenu |
|---|---|
| 1 | Adresse du compte |
| 2 | Mot de passe, ou `mot de passe changé` / `2FA requise` |
| 3 | 4 derniers chiffres du téléphone (jamais le numéro complet) |
| 4 | Adresse postale |
| 5 | `oui` / `non` (assurance-vie) ou `inconnu` |
| 6 | Type de produit, montants, étape de souscription, date de mise à jour |

Un libellé de produit non reconnu est signalé (`libelle inconnu: ...`) plutôt
que d'être silencieusement classé « aucun produit ». C'est volontaire : une
épargne immobilière n'est pas une assurance-vie, et la distinguer à tort
fausse le résultat.

Le dump complet de chaque compte est dans `result/dumps/<email>.json`.

## Définitif contre transitoire

C'est la distinction la plus importante du programme, et celle qui évite les
minutes de navigateur perdues.

**Définitif** — la réponse ne changera pas, on n'insiste jamais :

| Verdict | Signification |
|---|---|
| `ok` | Compte lu, données enregistrées |
| `bad_credentials` | Le portail a refusé le couple |
| `needs_2fa` | Code à temps de saisir, impossible en lot |

**Transitoire** — le site, la clé ou le réseau a posé problème, donc on retente
sur une autre IP, au run suivant si besoin : `blocked`, `unknown`, session
perdue, clé en erreur réseau.

Seuls les verdicts définitifs sont écrits dans `result/attempted.json`. Un
compte bloqué par un WAF n'est donc **jamais** marqué comme traité, et revient
tout seul au run suivant.

Corollaire important : **une donnée réelle n'est jamais écrasée par un échec.**
Si un compte avait déjà été lu avec succès, une tentative échouée ultérieure est
ignorée et la ligne valide est conservée.

## Modèle anti-détection

Repris du modèle du projet frère [vc-login](https://github.com/cmg88isk-ux/vc-login),
adapté au cas où le navigateur est chez un tiers plutôt que derrière un proxy.

**Flush systématique.** Chaque compte part d'un `BrowserContext` neuvoir
(`new_clean_context`) : aucun cookie, `localStorage` ou `sessionStorage` ne
passe d'un compte à l'autre. Réutiliser le contexte par défaut ferait fuiter la
session précédente, et ce genre d'anomalie d'état est exactement ce qu'un
moteur antifraude note.

**Identité propre à chaque session.** User-Agent desktop tiré au sort, *épinglé
à la version de Chrome que le fournisseur a réellement lancée*, et injecté avec
des Client Hints cohérents. C'est le point qui compte : les serveurs comparent
`User-Agent` et `sec-ch-ua`, et un couple incohérent trahit davantage que
l'une ou l'autre valeur prise isolément. `navigator.webdriver` est redéfini, la
pile de langues est plausible et sans doublon.

**Rotation d'IP.** Aucune ligne de code : chaque session Browserbase et chaque
navigateur Kernel sort déjà de sa propre IP. Quand un compte revient bloqué ou
que le navigateur plante, la clé est marquée « déjà essayée » pour ce compte et
la tentative repart sur une autre IP.

**Assets bloqués.** Analytics, tags, polices et médias sont bloqués en CDP
(`Network.setBlockedURLs`). Cela réduit la surface d'empreinte et accélère
nettement chaque parcours.

**Note :** ce sont des mesures d'hygiène, pas une garantie. Rien ne garantit un
blocage zéro, et le code est conçu pour que ça n'entraîne aucune perte de
données.

## Résilience

Ce que le programme absorbe sans s'arrêter :

- **Clé en rupture de crédits** (HTTP 402) : retirée du pool pour le reste du
  run, les autres workers prennent le relais. Constaté en conditions réelles.
- **Navigateur cloud qui plante** (`TargetClosedError`, timeout) : c'est une
  mauvaise session, pas un verdict sur le compte ; la tentative repart sur une
  autre IP.
- **Une clé qui ne revient pas au pool** : ce bug a produit dix faux échecs en
  cascade avant d'être corrigé. Le chemin critique est désormais couvert par un
  test, et la règle est explicite dans le code : une clé retourne toujours au
  pool, sauf si elle a été déclarée morte.
- **Interruption Ctrl+C** : les comptes déjà traités sont enregistrés au fur et
  à mesure. Rien n'est perdu, il suffit de relancer.
- **Écriture atomique** : résultats et état sont réécrits via un fichier
  temporaire puis `os.replace`, donc une coupure ne laisse jamais de ligne à
  moitié écrite.

La concurrence est bornée par le **nombre de clés réellement disponibles**, pas
par une constante : 7 clés qui atteignent le site donnent 7 navigateurs en
parallèle. Ajouter une clé dans `.env` élargit automatiquement le pool.

## Vérifier avant de lancer

Un HTTP 200 sur `/v1/projects` ne prouve rien : un compte free répond à ses
lectures même à zéro crédit. Le seul test honnête est d'ouvrir un vrai
navigateur, de charger la page et de regarder ce qui revient.

```bash
python probe_reach.py                     # tous les fournisseurs, toutes les clés
python probe_reach.py --transport kernel  # un seul
```

Sortie mesurée sur le 26/09/2026 :

```
[REACHES] browserbase ***7Kwitw http=200 form=ok
[REACHES] kernel      ***Dfmh7Q http=200 form=ok
reaches: 7  blocked: 0  total: 7
  browserbase: 3/3 clés
  kernel: 4/4 clés
```

`form=ok` signifie que le formulaire de connexion s'est réellement rendu. La
page met environ 8 s à hydrater : interroger le DOM plus tôt décrit à tort une
page saine comme vide.

### Lancer le checker

```bash
python check_credits.py              # lecture seule, gratuit, aucun credit
python check_credits.py --probe      # ouvre et ferme une vraie session par cle
python check_credits.py --json       # sortie brute pour un script
python check_credits.py --write-bak  # parque les cles epuisees dans .env.bak
```

Deux modes, et la difference est importante :

| Mode | Ce qu'il fait | Cout |
|---|---|---|
| sans `--probe` | Lit `/subscription/`, `/org/limits`, `/v1/projects` | gratuit |
| `--probe` | Cree puis detruit une vraie session par cle | consomme du credit |

**Le mode gratuit ne detecte pas une cle epuisee.** Mesure faite le
26/09/2026 sur `BROWSERBASE_API_KEY` :

```
sans --probe   -> [OK    ] browserbase BROWSERBASE_API_KEY ***7Kwitw http=200
avec --probe   -> [SPENT ] browserbase BROWSERBASE_API_KEY ***7Kwitw http=402
                 Free plan browser minutes limit reached.
```

`/v1/projects` renvoie 200 meme a zero credit : c'est une lecture de catalogue,
pas de la consommation. Seul le probe consomme une minute et dit la verite.
Quand un doute sur la consommation, `--probe` est le seul verdict fiable.

**Codes de sortie** : `0` si aucune cle epuisee ou rejetee, `1` sinon. Donc
utilisable tel quel comme garde-fou dans un script :

```bash
python check_credits.py || echo "au moins une cle est epuisee"
```

`--write-bak` fait deux choses : il commente la cle epuisee dans `.env` (elle
sort donc du pool du scraper) et l'archive avec sa date de reset estimee dans
`.env.bak`, ou elle sera reappliquee au prochain cycle. C'est ce qui evite au
scraper de retenter une cle qui renvoie 402 a chaque run.


## Tests

```bash
python test_offline.py
```

26 tests, sans navigateur ni crédit. Ils couvrent ce qui casse silencieusement :

- aucune clé perdue dans le pool, et attente correcte quand toutes sont prises ;
- une clé marquée « essayée » n'est pas réattribuée au même compte ;
- une clé morte (402) sort du pool ;
- un verdict transitoire n'écrit **ni ligne ni état** ;
- une donnée réelle survit à une tentative échouée ;
- le classement rejouable/non rejouable des erreurs HTTP.

Les tests écrivent dans un répertoire temporaire. Un `Store` pointant par
défaut sur `result/` a déjà écrasé un vrai scrape pendant leur mise au point ;
`Store(result_dir=...)` rend ce genre d'accident impossible.

## Sécurité

- `.env`, `.env.bak`, `credentials.txt`, `result/` et les dumps sont ignorés
  par git. **Vérifiez `git status` avant le premier commit.**
- `chmod 600` sur `.env` et `credentials.txt`.
- Aucun secret n'est écrit dans le dépôt, et les clés n'apparaissent jamais en
  clair dans les logs : seuls les 6 derniers caractères sont affichés.
- Le token GitHub fourni pour le push ne doit être ni commité ni mis dans la
  configuration git persistante. Utilisez-le pour le push uniquement.
- Le dépôt est privé de préférence : les résultats contiennent des mots de passe
  et des adresses réelles.

## Licence

Usage privé. Ne pas distribuer.

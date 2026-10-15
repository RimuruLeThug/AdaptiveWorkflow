# AdaptiveWorkflow

> **Version 2.0.0.** Testé sous Windows uniquement. La logique est couverte par des tests unitaires qui n'ont besoin ni de réseau ni de modèle réel. L'usage avec des modèles réels se limite à un run documenté ([docs/auto_projet.md](docs/auto_projet.md)). Lisez la [Feuille de route](#feuille-de-route) avant de vous y fier.

**Un workflow multi-agents pour le terminal, sans coût obligatoire.** Des modèles locaux (Qwen via Ollama) et l'API Gemini gratuite travaillent ensemble, sous un code Python simple qui prend les décisions. Chaque utilisateur utilise sa propre clé Gemini et ses propres modèles locaux.

Le cœur fournit la mécanique (file d'attente et budgets, organisateur et workers, contrôles des sorties, revue et vote, escalade, masquage des données personnelles, journaux). Chaque cas d'usage est un *profil* dans `profiles/`. L'interface et les prompts sont en français ; la version anglaise de cette page est [README.md](README.md).

**Sommaire :** [Périmètre](#périmètre) · [Démarrage rapide](#démarrage-rapide) · [Configuration testée](#configuration-testée) · [Installation détaillée](#installation-détaillée) · [Utilisation](#utilisation) · [Mode langage libre](#mode-langage-libre) · [Commandes](#commandes) · [Agents interchangeables](#agents-interchangeables) · [Profils](#profils) · [Quotas Gemini](#quotas-gemini) · [Mode Claude (facultatif)](#mode-claude-facultatif) · [Escalade du workflow](#escalade-du-workflow) · [Données personnelles et RGPD](#données-personnelles-et-rgpd) · [Calibrage](#calibrage) · [Architecture](#architecture) · [Tests](#tests) · [Limites connues](#limites-connues) · [Feuille de route](#feuille-de-route) · [Note légale sur les profils](#note-légale-sur-les-profils) · [Licence](#licence)

## Périmètre

Le projet prend une description de projet (`/fiche`), demande à un organisateur de répartir une tâche entre des workers (`/tache`), regroupe les idées en double (`/dedup`), fait noter les idées par trois relecteurs (`/revue`) et enregistre vos décisions (`/valider`, `/refuser`). Pour une idée que vous avez validée et que les relecteurs ont approuvée, un grand modèle peut l'étudier (`/escalader`), après un contrat figé et votre confirmation.

Principes :

- **L'organisateur propose, le code décide.** Liste fermée de workers, budget de délégations, profondeur maximale, plan invalide refusé en entier, chaque sortie vérifiée.
- **Pas de pensée de groupe.** Chaque worker et chaque relecteur travaille sans voir les réponses des autres.
- **Le code calcule** les votes, les scores et les shortlists. Les modèles ne font que proposer.
- **Aucune demande en texte libre ne part vers un modèle externe sans votre confirmation.** Seule exception : le rôle de reformulation, qui demande un accord unique par session (voir [Mode langage libre](#mode-langage-libre)).
- **Une seule file limitée en débit** pour tous les appels Gemini.

Ce que le projet ne fait **pas** :

- Il n'exécute pas de code, ne modifie pas de fichiers et n'agit sur aucun compte. Les modèles produisent du texte ; Claude est appelé sans outils.
- Il n'a pas d'interface web ni graphique. C'est une application de terminal.
- Il n'est pas prévu pour des données personnelles en V1 : le niveau gratuit de Gemini et un abonnement Claude sont exclus pour les données de clients (voir [Données personnelles et RGPD](#données-personnelles-et-rgpd)).
- Ses seuils ne sont pas calibrés. Les valeurs par défaut sont provisoires.

## Démarrage rapide

Windows uniquement, seule plateforme validée :

1. Téléchargez l'archive `AdaptiveWorkflow-vX.Y.Z.zip` depuis la page **Releases** du dépôt GitHub, puis décompressez-la dans un dossier vide.
2. Double-cliquez sur **`install.bat`**. Il vérifie Python 3.11 ou plus récent, installe `tzdata`, vérifie Ollama et propose de télécharger les deux modèles `qwen3.5:4b-q8_0` et `qwen2.5:3b-instruct-q4_K_M`. Il crée `.env` à partir de `.env.example`, sans aucune clé.
3. Ouvrez `.env` et renseignez `GEMINI_API_KEY=` avec votre clé. Une clé gratuite est disponible dans [Google AI Studio](https://aistudio.google.com) (Clés API).
4. Double-cliquez sur **`start.bat`** pour ouvrir le terminal. Tapez `/aide` pour la liste des commandes.

Facultatif : `tests.bat` lance la suite de tests ; le résultat est écrit dans `logs\tests.txt`.

## Configuration testée

| | |
|---|---|
| Système | Windows, Python 3.12 |
| Processeur | AMD Ryzen 7 7800X3D |
| Carte graphique | AMD Radeon RX 7800 XT, 16 Go (Ollama à 100 % GPU) |
| Mémoire | 32 Go |
| Modèles locaux | `qwen3.5:4b-q8_0` (5,2 Go), `qwen2.5:3b-instruct-q4_K_M` (1,9 Go) |
| API | Gemini `gemini-3.5-flash-lite`, niveau gratuit |

Le cœur n'utilise que la bibliothèque standard de Python (3.11+). `tzdata` est recommandé. D'autres plateformes peuvent fonctionner, mais seul Windows a été testé. Le coffre chiffré du masquage utilise DPAPI de Windows ; ailleurs, il faut le paquet facultatif `cryptography`.

## Installation détaillée

Alternative manuelle à `install.bat` :

1. **Python 3.11 ou plus récent** (testé avec 3.12). Puis `pip install -r requirements.txt` (installe `tzdata`).
2. **Ollama** ([ollama.com](https://ollama.com)), puis :
   ```
   ollama pull qwen3.5:4b-q8_0
   ollama pull qwen2.5:3b-instruct-q4_K_M
   ```
   Après un premier appel, `ollama ps` doit afficher `100% GPU` dans la colonne PROCESSOR.
3. **Clé Gemini.** Copiez `.env.example` vers `.env` (nom exact, à la racine du projet) et renseignez `GEMINI_API_KEY`. Le programme refuse de démarrer sans elle.
4. **Vérifications :**
   ```
   python -m gemini_client      # une vraie requête Gemini (comptée dans le quota) : répond « OK »
   python -m local_llm          # les deux modèles locaux répondent
   python -m privacy selftest   # coffre chiffré du masquage
   python -m unittest discover -s tests -t .
   ```
5. **Lancer :** `python -m qwen_ui` (ou `start.bat`).

Facultatif, pour une meilleure détection des noms en français : `pip install spacy` puis `python -m spacy download fr_core_news_md`.

### À propos de `.env` et des secrets

Votre clé API se trouve **uniquement** dans votre fichier `.env` local. Il est listé dans `.gitignore` et ne doit jamais être versionné. Le dépôt ne contient que `.env.example`, avec une clé vide. L'état local (`state/` : compteurs de quota, coffre de masquage, décisions) et les journaux (`logs/`) sont aussi ignorés par git. Aucune clé n'est écrite dans le code, et la clé est envoyée dans un en-tête de requête, jamais dans une URL.

## Utilisation

Lancez le terminal (`python -m qwen_ui` ou `start.bat`). Le programme refuse de démarrer si la clé Gemini manque ou si le modèle d'interface Qwen 3.5 4B n'est pas disponible dans Ollama. À la sortie, les modèles du projet sont déchargés de la VRAM.

Session type : décrivez le projet avec `/fiche`, validez-le, lancez `/tache <consigne>`, puis `/dedup`, `/revue`, `/valider` ou `/refuser`. Pour une idée validée, `/escalader <n>` est l'étape facultative suivante.

### Mode langage libre

Sans `/` en début de ligne, vous pouvez parler librement à Qwen.

1. **Reformulation.** Un agent `reformulation` transforme votre message en une demande claire pour Qwen. Par défaut c'est Gemini ; `AGENT_REFORMULATION=qwen_interface` garde tout en local. Gemini demande **une fois par session** l'autorisation. Refuser, ou `/accord non`, garde la reformulation en local. Chaque envoi externe est masqué et contrôlé contre les fuites avant de partir.
2. **Routage.** Qwen propose une action parmi une liste blanche (`tache`, `fiche`, `dedup`, `revue`, `retour`, `detail`, `resume`, `organisateur`, `quota`, `workers`, `aide`, `discussion`). Le code la valide. Les actions coûteuses ou qui changent l'état (`tache`, `revue`, `fiche`, `organisateur`) vous sont présentées et demandent votre `o`.
3. **Profil implicite.** Vous pouvez nommer un profil (« passe en auto_projet »), ou Qwen en propose un parmi ceux qui existent. Le code vérifie le nom. Un profil « données personnelles » est chargé tout de suite, avant tout envoi.
4. **Données personnelles.** Le code (et non le modèle) détecte les identifiants comme les adresses e-mail, les IBAN, les numéros de sécurité sociale français, les numéros de téléphone et les dates de naissance. S'il en trouve, il bascule sur le profil restrictif (`PROFIL_DONNEES_PERSO`, par défaut `bureautique_fictive`), révoque l'accord de reformulation et bloque les envois externes pour la session. Seul un `/profil <nom>` explicite lève ce verrou.
5. **Retours.** Vous pouvez répondre aux résultats en langage courant (« garde la 2, la 1 est trop générique »). Qwen interprète, le code vérifie les numéros, et vous confirmez. Une raison est demandée une fois, facultative. Les décisions passent par le même mécanisme que `/valider` et `/refuser`. Chaque décision est enregistrée dans `state/retours.jsonl` (masqué, local). Les derniers retours (`FEEDBACK_EXEMPLES`, 5 par défaut) sont donnés à Qwen comme exemples, jamais dans un profil « données personnelles ». Un retour ne porte que sur la dernière revue.

Non validé avec des modèles réels ; couvert par des tests unitaires avec des doublures.

### Commandes

| Commande | Rôle | Requêtes Gemini |
|---|---|---|
| `/aide` | affiche l'aide | 0 |
| `/fiche <description>` | Qwen rédige la fiche projet, vous la validez (`/fiche` seul l'affiche, `/fiche effacer` la supprime) | 0 |
| `/tache <consigne>` | l'organisateur découpe la tâche et délègue aux workers ; au plus 9 requêtes par défaut (2 plans, 6 délégations, 1 synthèse), plus les retries | jusqu'à 9 |
| `/detail <n>` | sortie complète d'un worker du dernier run | 0 |
| `/dedup` | regroupe les idées en double (doublons exacts, puis propositions de Qwen 2.5 3B vérifiées par le code) | 0 |
| `/revue` | 3 relecteurs notent les idées (une requête chacun) ; 2 sur 3 doivent approuver ; shortlist | 3 |
| `/valider <n> [...]` | valide des idées de la dernière revue | 0 |
| `/refuser <n> [...] [raison]` | refuse des idées ; le refus est transmis à l'organisateur au prochain `/tache` (sauf s'il contient une donnée personnelle) | 0 |
| `/escalader <n> [force]` | un grand modèle étudie une idée validée et approuvée, après contrat figé et confirmation ; `force` passe outre le seuil de complexité | 1 requête Gemini, ou 1 appel Claude |
| `/organisateur gemini\|claude` | choisit l'organisateur (`claude` est vérifié et demande une confirmation) | 0 |
| `/grand gemini\|claude` | choisit le grand modèle | 0 |
| `/profil [nom]` | affiche ou force le profil (facultatif, le code le choisit) | 0 |
| `/accord [oui\|non]` | affiche, accorde ou révoque l'accord de session pour la reformulation externe | 0 |
| `/masquer <texte>` | aperçu local du masquage et contrôle de fuite ; rien n'est envoyé | 0 |
| `/donnees` | données fictives du profil, minimisées et masquées | 0 |
| `/resume` | résumé local (Qwen) du dernier résultat | 0 |
| `/workers` | liste la liste fermée des workers | 0 |
| `/quota` | requêtes Gemini utilisées aujourd'hui et restantes | 0 |
| `/quitter` | quitte | 0 |

Alias : `/task`, `/tâche`, `/help`, `/org`, `/review`, `/doublons`, `/détail`, `/résumé`, `/q`, `/exit`, `/quit`.

Sans `/`, les commandes ci-dessus peuvent aussi être demandées en langage libre (voir plus haut). `/escalader`, `/grand`, `/masquer`, `/profil`, `/valider`, `/refuser`, `/accord`, `/donnees` et `/quitter` ne sont accessibles qu'en commande explicite (les décisions peuvent quand même être données en langage naturel après une revue).

## Agents interchangeables

Chaque rôle du workflow peut être confié à un autre agent, par `.env` ou par un profil, sans modifier le code (`agents.py`). Les rôles sont `organisateur`, `worker`, `relecteur`, `grand_modele`, `interface`, `cadrage`, `routage`, `promesse`, `format`, `dedup`, `reparation`, `reformulation`.

```
AGENT_REFORMULATION=qwen_interface        # la reformulation reste locale (aucun envoi externe)
AGENT_DEDUP=qwen_format,qwen_interface    # agent principal, puis repli
```

Agents : `qwen_interface`, `qwen_format` (locaux), `gemini`, `gemini_test` (son modèle vient uniquement de `GEMINI_TEST_MODEL` ; vide = indisponible), `claude`, et les agents déclarés dans `AGENT_DECLARATIONS` ou dans un profil (fournisseurs `ollama`, `local_openai` sur une adresse de bouclage, `gemini`, `claude`). Tout rôle confié à Gemini ou à Claude est un envoi externe : il est masqué, contrôlé contre les fuites et soumis à votre confirmation, sauf le rôle de reformulation sous accord de session. Un profil « données personnelles » refuse tout agent externe, replis compris. Chaque appel Gemini, quel que soit l'agent, passe par la même file et le même quota.

Vérifié par tests unitaires et doublures uniquement. Aucune comparaison d'agents avec des modèles réels n'a été faite. Détails : [docs/agents.md](docs/agents.md) et [docs/interchangeabilite.md](docs/interchangeabilite.md).

## Profils

Un profil est un dossier `profiles/<nom>/` contenant `profil.json` (workers, consignes, schémas de sortie, seuils de revue, champs) et, facultativement, `donnees.json` (données d'exemple **fictives**). Le code valide le profil au chargement. Un nouveau cas d'usage s'ajoute sans modifier le cœur.

| Profil | Usage | Données personnelles | Workers |
|---|---|---|---|
| `brainstorm` (par défaut) | idées et analyses | non | `generateur`, `analyste` |
| `bureautique_fictive` | exemple de back-office (relances, courriers) sur données **fictives** ; montre la minimisation, le masquage et le refus des fournisseurs externes | oui | `redacteur` |
| `auto_projet` | le projet utilisé sur son propre code et sa documentation publics | non | `idees_projet`, `relecteur_projet` |

`/profil` affiche la liste, `/profil <nom>` force un profil. Le profil de démarrage se règle avec `PROFILE`.

## Quotas Gemini

Le limiteur applique des valeurs prudentes par défaut, modifiables dans `.env` :

| Règle | Valeur |
|---|---|
| Espacement entre deux requêtes | 5 s (12 par minute) |
| Alerte | 1 200 requêtes par jour |
| Arrêt dur | 1 400 requêtes par jour (plus aucun appel Gemini ce jour-là) |
| Relances sur HTTP 429 | 2, chacune comptée ; le délai demandé par Google est respecté |
| Remise à zéro | minuit, heure du Pacifique |

Toutes les requêtes Gemini (organisateur, workers, relecteurs, reformulation, escalade) passent par **une seule file** et partagent ce budget. Le compteur est écrit sur disque avant chaque appel et survit aux redémarrages : une requête partie est comptée même si le programme plante. `/quota` affiche le compteur.

**Les limites exactes varient selon le modèle et dans le temps** : vérifiez-les pour votre clé dans AI Studio ([page des limites de débit](https://aistudio.google.com/rate-limit)).

## Mode Claude (facultatif)

Claude est une option pour l'organisateur et pour le grand modèle. Il passe par **Claude Code** (`claude -p`), connecté avec **votre abonnement claude.ai**. Il **consomme le quota de votre abonnement** ; ce n'est jamais gratuit, et le programme ne le présente jamais comme tel.

- Installez Claude Code et connectez-vous : `claude auth login` (compte claude.ai).
- Le programme vérifie la connexion (`claude auth status`). Une connexion par clé API est **refusée**, car elle facturerait l'API, sauf si vous l'autorisez explicitement avec `CLAUDE_ALLOW_API_BILLING=1`. La variable `ANTHROPIC_API_KEY` est retirée de l'environnement de l'appel.
- Claude est appelé sans outils (aucun fichier, aucune commande), depuis un dossier temporaire vide. Les appels sont comptés par jour dans `state/claude_calls.json`.

## Escalade du workflow

L'escalade est une fonctionnalité du produit : elle envoie une idée ambitieuse à un grand modèle. Elle n'a aucun rapport avec le développement du projet.

Une idée est candidate si vous l'avez validée, si 2 relecteurs sur 3 l'ont approuvée, et si son score de complexité dépasse `COMPLEXITY_THRESHOLD` (5 par défaut). Qwen estime les composantes (fichiers touchés, dépendances nouvelles, risque de régression) ; le code calcule le score. Ensuite :

1. le code construit un **contrat** (promesse, périmètre, hors périmètre, contraintes non négociables) que vous validez, puis qui est figé ;
2. le coût et la durée sont affichés ; **rien n'est appelé sans votre confirmation** ;
3. le grand modèle peut réduire le périmètre, changer d'approche ou découper le travail, mais ne peut pas modifier la promesse, lever une contrainte ni ajouter d'élément hors périmètre ;
4. le code contrôle la réponse (JSON, schéma, contraintes, périmètre), puis Qwen répond à une question fermée (« la promesse est-elle identique ? »). Au moindre doute, la version est rejetée, et une nouvelle tentative repart du contrat initial.

Le rapport et le résultat sont écrits dans `logs/escalation-<id>.json` et `state/escalations/<id>.json`.

## Données personnelles et RGPD

Le module `privacy/` masque les données avant tout envoi vers l'extérieur : minimisation des champs, détection déterministe (adresses e-mail, numéros de téléphone, IBAN, numéros de sécurité sociale français, SIRET/SIREN, adresses postales, codes postaux, dates de naissance, noms), indices indirects proposés par Qwen et retenus seulement s'ils sont vérifiables dans le texte, aperçu obligatoire, **contrôle de fuite bloquant**, coffre local chiffré (DPAPI sous Windows) avec des jetons stables, et réinjection locale des vraies valeurs par le code.

Le détecteur est **volontairement laxiste** en V2 : seuls les e-mails, les IBAN, les numéros de sécurité sociale français, les numéros de téléphone et les dates de naissance font basculer une session sur le profil restrictif. Les noms, adresses et codes postaux sont tout de même masqués avant tout envoi externe, mais ne font pas basculer le profil. Ce point est à resserrer en V3.

**Limites à connaître :**

- Des données pseudonymisées restent des **données personnelles** (RGPD, considérant 26). Le masquage réduit le risque ; il ne vous sort pas du RGPD.
- **Le niveau gratuit de Gemini et un abonnement Claude sont exclus pour les données de clients** (pas de contrat de traitement de données). Un profil « données personnelles » refuse donc tout fournisseur externe en V1 ; seul le masquage local est disponible. Un usage réel exige une offre payante avec contrat de traitement, et probablement une analyse d'impact (AIPD).
- Le masquage automatique n'est pas parfait : une combinaison de détails peut rester identifiante, et un modèle peut recopier ou inventer une valeur. Sans spaCy (`fr_core_news_md`), un nom sans civilité ni majuscules n'est détecté que s'il figure dans les champs déclarés. Pour des données très sensibles, seul un traitement entièrement local est sûr.
- **Testez uniquement avec des données fictives.**

## Calibrage

Tous les seuils sont centralisés dans `config.py` et peuvent être modifiés dans `.env`. Leurs valeurs actuelles (6,5/10 pour l'approbation, 2 votes sur 3, shortlist de 5 à 8, seuil de complexité 5) sont provisoires : elles n'ont **pas** été calibrées sur des sessions réelles.

`calibrate.py` lit vos propres journaux et décisions et propose des valeurs, avec la taille des échantillons et des avertissements. Il est en lecture seule et n'écrit jamais dans `.env` :

```
python calibrate.py                    # dossiers logs/ et state/ configurés
python calibrate.py --votes 2          # votes nécessaires (défaut de la configuration)
python calibrate.py --inclure-pilote   # compte aussi les décisions scriptées de pilot.py
```

En dessous de 20 observations, le rapport qualifie le résultat d'anecdotique. Méthode et détails : [docs/calibration.md](docs/calibration.md).

## Architecture

```
config.py            configuration : quotas, modèles, fournisseurs (valeurs prudentes par défaut)
rate_limiter.py      file unique, espacement, relances, compteur quotidien persistant
gemini_client.py     API REST Gemini (bibliothèque standard), toujours via le limiteur
local_llm.py         modèles locaux derrière une interface unique (Ollama en V2)
claude_cli.py        Claude via Claude Code, sur abonnement
agents.py            agents interchangeables : rôles, registre, routage, replis, contrôles de confidentialité
orchestrator.py      bus : plan validé, délégations isolées, vérification, synthèse, journal
organizers/          organisateurs Gemini et Claude
workers/             contrat de worker, déduplication, workers par défaut
review.py            relecteurs, vote 2 sur 3, shortlist
escalation.py        contrat, grand modèle, contrôles
verify/              schéma JSON, réparation, contrôle de promesse ; haiku.py (vérification par Claude Haiku, désactivée)
privacy/             détection, masquage, contrôle de fuite, coffre chiffré, politique des fournisseurs
profiles/            un dossier par cas d'usage ; loader.py les valide
qwen_ui/             terminal : app.py, router.py, langage_libre.py, fiche.py, presenter.py, bootstrap.py
pilot.py             rejoue un scénario sans saisie et mesure
calibrate.py         suggestions de seuils à partir de vos runs (lecture seule)
tests/               19 modules de tests, aucun réseau ni modèle réel requis
docs/                documentation détaillée (en anglais)
README.md, README.fr.md, CHANGELOG.md, GUIDE_PROMPTS_QWEN_V1.md (comment rédiger fiches et consignes), LICENSE
requirements.txt     dépendances facultatives (tzdata ; spaCy et cryptography sont facultatifs)
.env.example         modèle de .env (sans clé)
*.bat                install.bat, start.bat, tests.bat, pilot.bat, pilot_dedup.bat, pilot_escalade.bat, privacy_selftest.bat, liberer_vram.bat
pilot_dedup.txt, pilot_escalade.txt   scénarios d'exemple pour pilot.py
state/, logs/        créés à l'exécution, ignorés par git
```

Le parcours d'une demande, les journaux et les quotas sont décrits dans [docs/architecture.md](docs/architecture.md). Les problèmes courants et les messages associés sont dans [docs/troubleshooting.md](docs/troubleshooting.md). Les variables sont listées dans [docs/configuration.md](docs/configuration.md).

## Tests

```
python -m unittest discover -s tests -t .
```

Ou `tests.bat`, qui écrit le résultat dans `logs\tests.txt`. La suite compte environ 470 fonctions de test (`def test_`) réparties dans 19 modules. Elle n'a besoin ni de réseau ni de modèle réel : Ollama, Gemini et Claude sont remplacés par des doublures. Des tests qui passent montrent la logique avec des doublures ; ils ne mesurent pas le comportement des modèles réels.

## Limites connues

- Seul Windows est validé.
- Les agents interchangeables et le mode langage libre sont validés par tests unitaires avec doublures. Le seul run avec des modèles réels est celui documenté dans [docs/auto_projet.md](docs/auto_projet.md) ; il est anecdotique, ce n'est pas un benchmark.
- Les seuils ne sont pas calibrés.
- Le détecteur de données personnelles est volontairement laxiste (voir plus haut) ; les noms sont masqués mais ne font pas basculer le profil restrictif.
- Le niveau gratuit de Gemini et Claude sont exclus pour les données personnelles ; en pratique, seul le traitement local est possible pour ce type de données.
- Le limiteur Gemini suppose un seul processus utilisant le fichier d'état à la fois. La concurrence des modèles locaux (VRAM, parallélisme d'Ollama) n'est pas gérée par le projet.
- Ollama est le seul backend local utilisé. Le backend `local_openai` existe mais n'est testé qu'avec des doublures.
- La vérification facultative par Claude Haiku (`verify/haiku.py`) est désactivée.

## Feuille de route

La V1 était volontairement limitée. État de la V2 et prochaines étapes :

- **V2**
  - [x] Agents interchangeables (`agents.py` : rôles, registre, routage avec repli, entrée `gemini_test`). Validé par tests unitaires et doublures uniquement.
  - [x] Interface simplifiée pilotée par Qwen : le mode langage libre. Validé par tests unitaires et doublures uniquement.
  - [x] Le projet utilisé sur lui-même : profil `auto_projet`, lancé une fois avec des modèles réels ; voir [docs/auto_projet.md](docs/auto_projet.md).
  - [~] Calibrage des seuils à partir de runs réels : les valeurs sont centralisées et `calibrate.py` propose des valeurs à partir de vos runs (voir [docs/calibration.md](docs/calibration.md)). Le calibrage réel n'a pas été fait ; il demande beaucoup de runs.
  - [~] Backends autres qu'Ollama : une abstraction et un backend local compatible OpenAI existent, testés avec doublures uniquement. Plateformes autres que Windows : le code n'a pas d'appel propre à Windows, mais seul Windows est validé.
- **V3 (prévu) :** resserrer le détecteur de données personnelles.

## Note légale sur les profils

Analyser un binaire n'est autorisé que si vous en êtes propriétaire ou si vous avez le droit de l'étudier. Aucun profil n'est fourni pour décompiler un logiciel commercial ou contourner une protection (DRM, anti-triche).

## Licence

MIT, voir [LICENSE](LICENSE).

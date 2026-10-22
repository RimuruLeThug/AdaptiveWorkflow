# AdaptiveWorkflow

> **Version 3.0.0.** Testé sous Windows uniquement (V2). La logique est couverte par des tests unitaires qui n'ont besoin ni de réseau ni de modèle réel. La V3 (rôles sur Claude Haiku, un seul Qwen local) n'a **pas encore** été lancée avec des modèles réels. Lisez la [Feuille de route](#feuille-de-route) avant de vous y fier.

**Un workflow multi-agents pour le terminal.** Des agents IA (Claude Haiku, Gemini, un Qwen local via Ollama) travaillent ensemble, sous un code Python simple qui prend les décisions. **Depuis la V3, chaque rôle est confié par défaut à Claude Haiku** (Claude Code connecté à votre abonnement claude.ai : cela consomme le quota de l'abonnement, ce n'est pas gratuit), avec **un seul modèle Qwen local** en repli. Chaque rôle reste réaffectable (`AGENT_<ROLE>`, ou la clé `agents` d'un profil) à Gemini gratuit ou au Qwen local pour revenir à un coût nul.

Le cœur fournit la mécanique (file d'attente et budgets, organisateur et workers, contrôles des sorties, revue et vote, escalade, masquage des données personnelles, journaux). Chaque cas d'usage est un *profil* dans `profiles/`. L'interface et les prompts sont en français ; la version anglaise de cette page est [README.md](README.md).

**Sommaire :** [Périmètre](#périmètre) · [Démarrage rapide](#démarrage-rapide) · [Configuration testée](#configuration-testée) · [Installation détaillée](#installation-détaillée) · [Utilisation](#utilisation) · [Mode langage libre](#mode-langage-libre) · [Commandes](#commandes) · [Agents interchangeables](#agents-interchangeables) · [Profils](#profils) · [Quotas Gemini](#quotas-gemini) · [Mode Claude (facultatif)](#mode-claude-facultatif) · [Escalade du workflow](#escalade-du-workflow) · [Données personnelles et RGPD](#données-personnelles-et-rgpd) · [Calibrage](#calibrage) · [Architecture](#architecture) · [Tests](#tests) · [Limites connues](#limites-connues) · [Feuille de route](#feuille-de-route) · [Note légale sur les profils](#note-légale-sur-les-profils) · [Licence](#licence)

## Périmètre

Le projet prend une description de projet (`/fiche`), demande à un organisateur de répartir une tâche entre des workers (`/tache`), regroupe les idées en double (`/dedup`), fait noter les idées par trois relecteurs (`/revue`) et enregistre vos décisions (`/valider`, `/refuser`). Pour une idée que vous avez validée et que les relecteurs ont approuvée, un grand modèle peut l'étudier (`/escalader`), après un contrat figé et votre confirmation.

Principes :

- **L'organisateur propose, le code décide.** Liste fermée de workers, budget de délégations, profondeur maximale, plan invalide refusé en entier, chaque sortie vérifiée.
- **Pas de pensée de groupe.** Chaque worker et chaque relecteur travaille sans voir les réponses des autres.
- **Le code calcule** les votes, les scores et les shortlists. Les modèles ne font que proposer.
- **Aucune demande en texte libre ne part vers un modèle externe sans votre accord** : confirmation à chaque envoi, ou accord unique de session pour un fournisseur (voir [Mode langage libre](#mode-langage-libre)). Tout envoi externe est masqué et contrôlé contre les fuites.
- **Données personnelles : tout en local.** Un profil « données personnelles » (ou une donnée personnelle détectée) écarte tous les agents externes ; chaque rôle est alors traité par le Qwen local.
- **Une seule file limitée en débit** pour tous les appels Gemini.

Ce que le projet ne fait **pas** :

- Il n'exécute pas de code, ne modifie pas de fichiers et n'agit sur aucun compte. Les modèles produisent du texte ; Claude est appelé sans outils.
- Il n'a pas d'interface web ni graphique. C'est une application de terminal.
- Il n'envoie jamais de données personnelles à un fournisseur externe : le niveau gratuit de Gemini et un abonnement Claude sont exclus pour les données de clients ; ces données ne sont traitées que par le Qwen local (voir [Données personnelles et RGPD](#données-personnelles-et-rgpd)).
- Ses seuils ne sont pas calibrés. Les valeurs par défaut sont provisoires.

## Démarrage rapide

Windows uniquement, seule plateforme validée :

1. Téléchargez l'archive `AdaptiveWorkflow-vX.Y.Z.zip` depuis la page **Releases** du dépôt GitHub, puis décompressez-la dans un dossier vide.
2. Double-cliquez sur **`install.bat`**. Il vérifie Python 3.11 ou plus récent, installe `tzdata`, vérifie Claude Code et Ollama et propose de télécharger le modèle local `qwen3.5:9b-q4_K_M` (environ 6,6 Go). Il crée `.env` à partir de `.env.example`, sans aucune clé.
3. Installez **Claude Code** et connectez-vous avec votre compte claude.ai : `claude auth login`. C'est l'agent par défaut de tous les rôles.
4. Facultatif : renseignez `GEMINI_API_KEY=` dans `.env` si vous confiez un rôle à Gemini (clé gratuite dans [Google AI Studio](https://aistudio.google.com)). Sans clé, les agents Gemini sont simplement indisponibles.
5. Double-cliquez sur **`start.bat`** pour ouvrir le terminal. Tapez `/aide` pour la liste des commandes.

Facultatif : `tests.bat` lance la suite de tests ; le résultat est écrit dans `logs\tests.txt`.

## Configuration testée

| | |
|---|---|
| Système | Windows, Python 3.12 |
| Processeur | AMD Ryzen 7 7800X3D |
| Carte graphique | AMD Radeon RX 7800 XT, 16 Go (Ollama à 100 % GPU) |
| Mémoire | 32 Go |
| Modèles locaux | V2 : `qwen3.5:4b-q8_0` (5,2 Go), `qwen2.5:3b-instruct-q4_K_M` (1,9 Go). V3 : un seul modèle, `qwen3.5:9b-q4_K_M` (6,6 Go), pas encore mesuré sur cette machine |
| API | Gemini `gemini-3.5-flash-lite`, niveau gratuit ; Claude Code (abonnement claude.ai) |

Le cœur n'utilise que la bibliothèque standard de Python (3.11+). `tzdata` est recommandé. D'autres plateformes peuvent fonctionner, mais seul Windows a été testé. Le coffre chiffré du masquage utilise DPAPI de Windows ; ailleurs, il faut le paquet facultatif `cryptography`.

## Installation détaillée

Alternative manuelle à `install.bat` :

1. **Python 3.11 ou plus récent** (testé avec 3.12). Puis `pip install -r requirements.txt` (installe `tzdata`).
2. **Claude Code**, connecté à votre abonnement : `claude auth login` (compte claude.ai). Agent par défaut de tous les rôles.
3. **Ollama** ([ollama.com](https://ollama.com)), puis le modèle local unique (repli, profils « données personnelles », masquage) :
   ```
   ollama pull qwen3.5:9b-q4_K_M
   ```
   Après un premier appel, `ollama ps` doit afficher `100% GPU` dans la colonne PROCESSOR. Autre modèle : `QWEN_MODEL` dans `.env`.
4. **Clé Gemini (facultative).** Copiez `.env.example` vers `.env` (nom exact, à la racine du projet) et renseignez `GEMINI_API_KEY` si un rôle est confié à Gemini.
5. **Vérifications :**
   ```
   python -m gemini_client      # une vraie requête Gemini (comptée dans le quota) : répond « OK »
   python -m local_llm          # le modèle local répond
   python -m privacy selftest   # coffre chiffré du masquage
   python -m unittest discover -s tests -t .
   ```
6. **Lancer :** `python -m qwen_ui` (ou `start.bat`).

Facultatif, pour une meilleure détection des noms en français : `pip install spacy` puis `python -m spacy download fr_core_news_md`.

### À propos de `.env` et des secrets

Votre clé API se trouve **uniquement** dans votre fichier `.env` local. Il est listé dans `.gitignore` et ne doit jamais être versionné. Le dépôt ne contient que `.env.example`, avec une clé vide. L'état local (`state/` : compteurs de quota, coffre de masquage, décisions) et les journaux (`logs/`) sont aussi ignorés par git. Aucune clé n'est écrite dans le code, et la clé est envoyée dans un en-tête de requête, jamais dans une URL.

## Utilisation

Lancez le terminal (`python -m qwen_ui` ou `start.bat`). Au démarrage, le programme vérifie Claude Code, Ollama et la clé Gemini, et **avertit** seulement s'il en manque un : un agent indisponible est sauté au profit de son repli (Claude absent : tout passe au Qwen local ; Qwen absent : pas de repli local ni de profil « données personnelles »). À la sortie, le modèle local est déchargé de la VRAM.

Par défaut, **on parle à l'assistant normalement, sans aucune commande** (mode conversation, ci-dessous). Les commandes `/…` restent disponibles pour qui les connaît.

### Mode conversation (par défaut)

Vous écrivez comme à une personne ; l'assistant comprend l'intention, se souvient des derniers échanges et vous demande votre accord avant tout travail qui coûte quelque chose. Exemple de session :

```
> salut, j'ai un projet d'appli Android gratuite pour réduire le gaspillage à la maison, hors ligne, je suis seul et j'ai 3 mois
Voici ce que je retiens : (fiche projet)
C'est bien ça ? (oui/non) oui
> vas-y, trouve-moi 12 idées dont 2 un peu folles
Je confie ça aux agents (claude_haiku …) : « Propose 12 idées … » On y va ? (oui/non) plutôt 15
Je confie ça aux agents (claude_haiku …) : « Propose 15 idées … » On y va ? (oui/non) vas-y
> regroupe les doublons
> fais-les noter
> je garde la 2 et la 5, la 1 est trop compliquée
> approfondis la 2
```

- **Un seul appel par message.** L'agent du rôle `routage` (Claude Haiku par défaut, Qwen local en repli) reçoit le message, l'historique récent (12 échanges, en mémoire seulement, jamais sur disque) et un état du travail résumé par le code (fiche, idées, notes, décisions). Il propose **une** action d'une liste blanche (répondre ou poser une question, décrire le projet, demander des idées, regrouper, faire noter, donner son avis, afficher, résumer, approfondir, quota, confidentialité) ; le code la vérifie (numéros existants, arguments).
- **Confirmations en langage naturel.** « oui », « ok », « vas-y », « non », « laisse tomber »… Toute autre réponse est prise comme une correction (« plutôt 15 idées ») : l'étape est abandonnée et la réponse traitée comme un nouveau message, qui voit la proposition précédente. Deux corrections enchaînées au plus. La consigne exacte envoyée aux agents est toujours affichée avant le « On y va ? ».
- **Accord de session.** Si l'agent de conversation est externe (Claude Haiku par défaut) et disponible, la question est posée **une fois** au premier message. « non » : l'assistant vous comprend avec le Qwen local, sans question à chaque message ; « garde tout en local » le dit en cours de route, « tu peux utiliser Claude » le redemande. Claude Code absent : rien n'est demandé, Qwen répond.
- **Données personnelles.** Même règle que ci-dessous : détection par le code, profil restrictif, tout en local. Quitter ce profil (`/profil <nom>`) vide l'historique de conversation.
- **Plus de commandes affichées.** Les messages qui citaient une commande sont reformulés en phrases à dire (« dis « fais-les noter » »). Les commandes tapées (`/aide`, `/quota`…) fonctionnent et s'affichent comme avant.
- `aide` et `au revoir` (et variantes) sont reconnus par le code, sans appel de modèle. `MODE_CONVERSATION=0` revient au mode langage libre ci-dessous.

Validé par tests unitaires avec doublures (scénario complet sans aucune commande compris) ; pas encore avec des modèles réels : lancez `pilot.bat`, dont le scénario par défaut est désormais une conversation (`pilot.bat --commandes` pour l'ancien parcours).

### Mode langage libre (`MODE_CONVERSATION=0`)

Ancien mode, conservé : sans `/` en début de ligne, le message est reformulé puis une commande vous est proposée.

1. **Reformulation et accord de session.** Un agent `reformulation` (Claude Haiku par défaut ; `AGENT_REFORMULATION=qwen` garde tout en local) transforme votre message en une demande claire. Au premier message, le programme demande **une seule fois** un accord de session pour ce fournisseur : il couvre la reformulation **et les autres rôles en texte libre** qui lui sont confiés (routage, cadrage, résumés, dédoublonnage, réparation, contrôle de promesse), sans confirmation à chaque envoi. Refuser, ou `/accord non`, garde la reformulation en local et fait confirmer (o/n, avec aperçu masqué) chaque envoi externe des autres rôles. Chaque envoi externe est masqué et contrôlé contre les fuites avant de partir, accord ou pas.
2. **Routage.** L'agent de routage propose une action parmi une liste blanche (`tache`, `fiche`, `dedup`, `revue`, `retour`, `detail`, `resume`, `organisateur`, `quota`, `workers`, `aide`, `discussion`). Le code la valide. Les actions coûteuses ou qui changent l'état (`tache`, `revue`, `fiche`, `organisateur`) vous sont présentées et demandent votre `o`.
3. **Profil implicite.** Vous pouvez nommer un profil (« passe en auto_projet »), ou Qwen en propose un parmi ceux qui existent. Le code vérifie le nom. Un profil « données personnelles » est chargé tout de suite, avant tout envoi.
4. **Données personnelles.** Le code (et non le modèle) détecte les adresses e-mail, IBAN, numéros de sécurité sociale français, téléphones, dates de naissance, SIRET (et SIREN annoncés), adresses postales, codes postaux suivis d'une ville, valeurs déclarées par le profil et noms de personnes (civilité + nom, « Prénom NOM »). S'il en trouve, il bascule sur le profil restrictif (`PROFIL_DONNEES_PERSO`, par défaut `bureautique_fictive`), révoque l'accord de session et traite tout par le Qwen local pour la session. Seul un `/profil <nom>` explicite lève ce verrou.
5. **Retours.** Vous pouvez répondre aux résultats en langage courant (« garde la 2, la 1 est trop générique »). Qwen interprète, le code vérifie les numéros, et vous confirmez. Une raison est demandée une fois, facultative. Les décisions passent par le même mécanisme que `/valider` et `/refuser`. Chaque décision est enregistrée dans `state/retours.jsonl` (masqué, local). Les derniers retours (`FEEDBACK_EXEMPLES`, 5 par défaut) sont donnés à Qwen comme exemples, jamais dans un profil « données personnelles ». Un retour ne porte que sur la dernière revue.

Non validé avec des modèles réels ; couvert par des tests unitaires avec des doublures.

### Commandes

Appels avec les défauts V3 (chaque rôle sur Claude Haiku) ; un rôle réaffecté à Gemini consomme des requêtes Gemini à la place, un rôle local (ou un profil « données personnelles ») n'appelle rien d'externe.

| Commande | Rôle | Appels externes (défaut V3) |
|---|---|---|
| `/aide` | affiche l'aide | 0 |
| `/fiche <description>` | l'agent de cadrage rédige la fiche projet, vous la validez (`/fiche` seul l'affiche, `/fiche effacer` la supprime) | 1 |
| `/tache <consigne>` | l'organisateur découpe la tâche et délègue aux workers ; au plus 9 appels par défaut (2 plans, 6 délégations, 1 synthèse) | jusqu'à 9 |
| `/detail <n>` | sortie complète d'un worker du dernier run | 0 |
| `/dedup` | regroupe les idées en double (doublons exacts, puis propositions de l'agent de dédoublonnage vérifiées par le code) | 1 |
| `/revue` | 3 relecteurs notent les idées (un appel chacun) ; 2 sur 3 doivent approuver ; shortlist | 3 |
| `/valider <n> [...]` | valide des idées de la dernière revue | 0 |
| `/refuser <n> [...] [raison]` | refuse des idées ; le refus est transmis à l'organisateur au prochain `/tache` (sauf s'il contient une donnée personnelle) | 0 |
| `/escalader <n> [force]` | un grand modèle étudie une idée validée et approuvée, après contrat figé et confirmation ; `force` passe outre le seuil de complexité | 1, plus 1 estimation de complexité et 1 contrôle de promesse |
| `/organisateur agents\|gemini\|claude` | choisit l'organisateur : `agents` (défaut, rôle `organisateur`), ou un client direct (`claude` est vérifié et demande une confirmation) | 0 |
| `/grand agents\|gemini\|claude` | choisit le grand modèle (même logique) | 0 |
| `/profil [nom]` | affiche ou force le profil (facultatif, le code le choisit) | 0 |
| `/accord [oui\|non]` | affiche, accorde ou révoque l'accord de session (reformulation et rôles en texte libre, un fournisseur) | 0 |
| `/masquer <texte>` | aperçu local du masquage et contrôle de fuite ; rien n'est envoyé | 0 |
| `/donnees` | données fictives du profil, minimisées et masquées | 0 |
| `/resume` | résumé du dernier résultat par l'agent du rôle `interface` | 1 |
| `/workers` | liste la liste fermée des workers | 0 |
| `/quota` | requêtes Gemini utilisées aujourd'hui et restantes, appels Claude du jour | 0 |
| `/quitter` | quitte | 0 |

Alias : `/task`, `/tâche`, `/help`, `/org`, `/review`, `/doublons`, `/détail`, `/résumé`, `/q`, `/exit`, `/quit`.

En mode conversation, tout le parcours se fait sans commande, `escalader` compris (« approfondis la 2 », avec ses deux confirmations). Restent réservés aux commandes : `/organisateur`, `/grand`, `/masquer`, `/donnees`, `/fiche effacer` et l'accord de session donné sans question (`/accord oui`). En mode langage libre (`MODE_CONVERSATION=0`), `/escalader`, `/grand`, `/masquer`, `/profil`, `/valider`, `/refuser`, `/accord`, `/donnees` et `/quitter` ne sont accessibles qu'en commande explicite (les décisions peuvent quand même être données en langage naturel après une revue).

## Agents interchangeables

Chaque rôle du workflow est confié à un agent, par défaut ou par `.env` ou par un profil, sans modifier le code (`agents.py`). Les rôles sont `organisateur`, `worker`, `relecteur`, `grand_modele`, `interface`, `cadrage`, `routage`, `promesse`, `format`, `dedup`, `reparation`, `reformulation`.

**Défauts V3 :** chaque rôle → `claude_haiku` (Claude Code, modèle `CLAUDE_HAIKU_MODEL`, alias `haiku` par défaut), puis repli sur `qwen` (le modèle local unique). Pour changer :

```
AGENT_REFORMULATION=qwen                  # la reformulation reste locale (aucun envoi externe)
AGENT_WORKER=gemini,qwen                  # workers sur Gemini gratuit, repli local
AGENT_DEDUP=qwen                          # dédoublonnage local
BIG_MODEL_PROVIDER=claude                 # escalade sur l'agent « claude » (CLAUDE_MODEL, sonnet par défaut)
```

Agents : `claude_haiku`, `qwen` (local ; `qwen_interface` et `qwen_format`, noms de la V2, sont des alias du même modèle), `gemini`, `gemini_test` (son modèle vient uniquement de `GEMINI_TEST_MODEL` ; vide = indisponible), `claude`, et les agents déclarés dans `AGENT_DECLARATIONS` ou dans un profil (fournisseurs `ollama`, `local_openai` sur une adresse de bouclage, `gemini`, `claude`). Tout rôle confié à Gemini ou à Claude est un envoi externe : il est masqué et contrôlé contre les fuites ; pour un rôle en texte libre, il faut en plus votre confirmation ou l'accord de session. Un agent indisponible (Claude Code absent ou non connecté, clé Gemini absente) est sauté sans rien envoyer. En profil « données personnelles », les agents externes sont écartés, replis compris, et le rôle est traité par le Qwen local. Chaque appel Gemini, quel que soit l'agent, passe par la même file et le même quota.

Vérifié par tests unitaires et doublures uniquement. Aucune comparaison d'agents avec des modèles réels n'a été faite. Détails : [docs/agents.md](docs/agents.md) et [docs/interchangeabilite.md](docs/interchangeabilite.md).

## Profils

Un profil est un dossier `profiles/<nom>/` contenant `profil.json` (workers, consignes, schémas de sortie, seuils de revue, champs) et, facultativement, `donnees.json` (données d'exemple **fictives**). Le code valide le profil au chargement. Un nouveau cas d'usage s'ajoute sans modifier le cœur.

| Profil | Usage | Données personnelles | Workers |
|---|---|---|---|
| `brainstorm` (par défaut) | idées et analyses | non | `generateur`, `analyste` |
| `bureautique_fictive` | exemple de back-office (relances, courriers) sur données **fictives** ; montre la minimisation, le masquage et le traitement entièrement local (Qwen) | oui | `redacteur` |
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

Toutes les requêtes Gemini (quel que soit le rôle confié à Gemini) passent par **une seule file** et partagent ce budget. Le compteur est écrit sur disque avant chaque appel et survit aux redémarrages : une requête partie est comptée même si le programme plante. `/quota` affiche le compteur.

**Les limites exactes varient selon le modèle et dans le temps** : vérifiez-les pour votre clé dans AI Studio ([page des limites de débit](https://aistudio.google.com/rate-limit)).

## Mode Claude (facultatif)

Depuis la V3, Claude Haiku (agent `claude_haiku`) est l'agent par défaut de tous les rôles ; l'agent `claude` (`CLAUDE_MODEL`, `sonnet` par défaut) reste disponible pour l'organisateur ou le grand modèle. Les deux passent par **Claude Code** (`claude -p`), connecté avec **votre abonnement claude.ai**. Ils **consomment le quota de votre abonnement** ; ce n'est jamais gratuit, et le programme ne le présente jamais comme tel. Un run `/tache` + `/dedup` + `/revue` représente une quinzaine d'appels.

- Installez Claude Code et connectez-vous : `claude auth login` (compte claude.ai).
- Le programme vérifie la connexion (`claude auth status`). Une connexion par clé API est **refusée**, car elle facturerait l'API, sauf si vous l'autorisez explicitement avec `CLAUDE_ALLOW_API_BILLING=1`. La variable `ANTHROPIC_API_KEY` est retirée de l'environnement de l'appel.
- Claude est appelé sans outils (aucun fichier, aucune commande), depuis un dossier temporaire vide. Les appels sont comptés par jour dans `state/claude_calls.json` (`/quota`).
- Claude Code absent ou non connecté : un avertissement s'affiche au démarrage et les rôles passent directement à leur repli (Qwen local), sans demande de confirmation inutile.

## Escalade du workflow

L'escalade est une fonctionnalité du produit : elle envoie une idée ambitieuse à un grand modèle. Elle n'a aucun rapport avec le développement du projet.

Une idée est candidate si vous l'avez validée, si 2 relecteurs sur 3 l'ont approuvée, et si son score de complexité dépasse `COMPLEXITY_THRESHOLD` (5 par défaut). L'agent du rôle `interface` estime les composantes (fichiers touchés, dépendances nouvelles, risque de régression) ; le code calcule le score. Ensuite :

1. le code construit un **contrat** (promesse, périmètre, hors périmètre, contraintes non négociables) que vous validez, puis qui est figé ;
2. le coût et la durée sont affichés ; **rien n'est appelé sans votre confirmation** ;
3. le grand modèle peut réduire le périmètre, changer d'approche ou découper le travail, mais ne peut pas modifier la promesse, lever une contrainte ni ajouter d'élément hors périmètre ;
4. le code contrôle la réponse (JSON, schéma, contraintes, périmètre), puis l'agent du rôle `promesse` répond à une question fermée (« la promesse est-elle identique ? »). Au moindre doute, la version est rejetée, et une nouvelle tentative repart du contrat initial.

Le rapport et le résultat sont écrits dans `logs/escalation-<id>.json` et `state/escalations/<id>.json`.

## Données personnelles et RGPD

Le module `privacy/` masque les données avant tout envoi vers l'extérieur : minimisation des champs, détection déterministe (adresses e-mail, numéros de téléphone, IBAN, numéros de sécurité sociale français, SIRET/SIREN, adresses postales, codes postaux, dates de naissance, noms), indices indirects proposés par Qwen et retenus seulement s'ils sont vérifiables dans le texte, aperçu obligatoire, **contrôle de fuite bloquant**, coffre local chiffré (DPAPI sous Windows) avec des jetons stables, et réinjection locale des vraies valeurs par le code.

Le détecteur qui fait basculer une session sur le profil restrictif est **resserré en V3** : en plus des e-mails, IBAN, numéros de sécurité sociale, téléphones et dates de naissance, les SIRET (et SIREN précédés de « SIREN », « SIRET » ou « RCS »), les adresses postales (« 12 rue … »), les codes postaux suivis d'une ville (« 75011 Paris »), les valeurs déclarées par le profil et les noms de personnes (civilité + nom, « Prénom NOM ») le déclenchent. Deux filtres limitent les faux positifs : un code postal seul (« budget 15000 euros ») ne compte pas, et un sigle courant en majuscules (« Android TV », « API REST », « CRM ») n'est pas pris pour un nom. Un nom sans civilité ni majuscules (« relance dupont ») ne déclenche que s'il est déclaré par le profil. Tout ce qui est détecté reste de toute façon masqué avant un envoi externe.

**Limites à connaître :**

- Des données pseudonymisées restent des **données personnelles** (RGPD, considérant 26). Le masquage réduit le risque ; il ne vous sort pas du RGPD.
- **Le niveau gratuit de Gemini et un abonnement Claude sont exclus pour les données de clients** (pas de contrat de traitement de données). Un profil « données personnelles » écarte donc tout fournisseur externe : chaque rôle est traité par le Qwen local (V3). Un usage réel exige une offre payante avec contrat de traitement, et probablement une analyse d'impact (AIPD).
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
local_llm.py         modèle local derrière une interface unique (Ollama ; un seul modèle Qwen en V3)
claude_cli.py        Claude via Claude Code, sur abonnement
agents.py            agents interchangeables : rôles (défaut V3 : claude_haiku, repli qwen), registre, routage, replis, confidentialité
orchestrator.py      bus : plan validé, délégations isolées, vérification, synthèse, journal
organizers/          organisateurs Gemini et Claude
workers/             contrat de worker, déduplication, workers par défaut
review.py            relecteurs, vote 2 sur 3, shortlist
escalation.py        contrat, grand modèle, contrôles
verify/              schéma JSON, réparation, contrôle de promesse ; haiku.py (vérification par Claude Haiku, désactivée)
privacy/             détection, masquage, contrôle de fuite, coffre chiffré, politique des fournisseurs
profiles/            un dossier par cas d'usage ; loader.py les valide
qwen_ui/             terminal : app.py, conversation.py, router.py, langage_libre.py, fiche.py, presenter.py, bootstrap.py
pilot.py             rejoue un scénario sans saisie et mesure
calibrate.py         suggestions de seuils à partir de vos runs (lecture seule)
tests/               20 modules de tests, aucun réseau ni modèle réel requis (garde-fou dans tests/__init__.py)
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

Ou `tests.bat`, qui écrit le résultat dans `logs\tests.txt`. La suite compte environ 525 fonctions de test (`def test_`) réparties dans 20 modules. Elle n'a besoin ni de réseau ni de modèle réel : Ollama, Gemini et Claude sont remplacés par des doublures, et `tests/__init__.py` empêche tout appel réel à Claude Code ou à Ollama même si un test oubliait un double. Des tests qui passent montrent la logique avec des doublures ; ils ne mesurent pas le comportement des modèles réels.

## Limites connues

- Seul Windows est validé.
- Les agents interchangeables et le mode langage libre sont validés par tests unitaires avec doublures. Le seul run avec des modèles réels est celui de la V2 documenté dans [docs/auto_projet.md](docs/auto_projet.md) ; il est anecdotique, ce n'est pas un benchmark. Les défauts V3 (Claude Haiku partout, `qwen3.5:9b-q4_K_M`) n'ont pas encore tourné en réel : qualité des réponses de Haiku sur les schémas JSON, durée par appel de `claude -p` et consommation du quota sont à mesurer.
- Le mode conversation n'a pas encore tourné avec des modèles réels : la qualité de compréhension (surtout par le Qwen local 9B quand l'accord est refusé) et la pertinence des consignes construites à partir de l'historique sont à observer.
- Les seuils ne sont pas calibrés.
- Le détecteur de données personnelles est resserré mais heuristique (voir plus haut) : faux positifs possibles (un nom de marque en « Mot MAJUSCULES » fait basculer en local), faux négatifs possibles (nom en minuscules non déclaré).
- Chaque envoi externe déclenche d'abord, en local, le repérage des indices indirects par Qwen (masquage) ; sans Ollama, ce repérage est sauté avec un avertissement.
- Le niveau gratuit de Gemini et Claude sont exclus pour les données personnelles ; en pratique, seul le traitement local est possible pour ce type de données.
- Le limiteur Gemini suppose un seul processus utilisant le fichier d'état à la fois. La concurrence des modèles locaux (VRAM, parallélisme d'Ollama) n'est pas gérée par le projet.
- Ollama est le seul backend local utilisé. Le backend `local_openai` existe mais n'est testé qu'avec des doublures.
- La vérification facultative par Claude Haiku (`verify/haiku.py`) est désactivée.

## Feuille de route

La V1 était volontairement limitée. État des versions suivantes :

- **V2**
  - [x] Agents interchangeables (`agents.py` : rôles, registre, routage avec repli, entrée `gemini_test`). Validé par tests unitaires et doublures uniquement.
  - [x] Interface simplifiée pilotée par Qwen : le mode langage libre. Validé par tests unitaires et doublures uniquement.
  - [x] Le projet utilisé sur lui-même : profil `auto_projet`, lancé une fois avec des modèles réels ; voir [docs/auto_projet.md](docs/auto_projet.md).
  - [~] Calibrage des seuils à partir de runs réels : les valeurs sont centralisées et `calibrate.py` propose des valeurs à partir de vos runs (voir [docs/calibration.md](docs/calibration.md)). Le calibrage réel n'a pas été fait ; il demande beaucoup de runs.
  - [~] Backends autres qu'Ollama : une abstraction et un backend local compatible OpenAI existent, testés avec doublures uniquement. Plateformes autres que Windows : le code n'a pas d'appel propre à Windows, mais seul Windows est validé.
- **V3**
  - [x] Tous les rôles sur Claude Haiku par défaut (agent `claude_haiku`), repli sur le Qwen local, chaque rôle toujours réaffectable. Tests unitaires et doublures uniquement.
  - [x] Un seul modèle Qwen local (`QWEN_MODEL`, `qwen3.5:9b-q4_K_M` par défaut).
  - [x] Profils « données personnelles » : repli automatique sur le Qwen local au lieu d'un refus.
  - [x] Détecteur de données personnelles resserré.
  - [x] Clé Gemini et Ollama facultatifs au démarrage.
  - [ ] Run réel de la V3 et mesure (durée, quota Claude, validité JSON).
  - [x] Mode conversation : parler à l'assistant sans aucune commande (3.1.0). Tests unitaires et doublures uniquement.

## Note légale sur les profils

Analyser un binaire n'est autorisé que si vous en êtes propriétaire ou si vous avez le droit de l'étudier. Aucun profil n'est fourni pour décompiler un logiciel commercial ou contourner une protection (DRM, anti-triche).

## Licence

MIT, voir [LICENSE](LICENSE).

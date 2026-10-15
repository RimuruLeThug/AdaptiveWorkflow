# AdaptiveWorkflow (V1) — version française

> English documentation: see [README.md](README.md).

Squelette d'orchestration multi-agents **à coût zéro** : des modèles locaux (Qwen via Ollama) et l'API Gemini gratuite travaillent ensemble, sous le contrôle d'un code qui décide. Chaque utilisateur branche sa propre clé Gemini et ses propres modèles locaux.

Le squelette fournit la mécanique — file d'attente et budgets, organisateur et workers, vérification, revue et vote, escalade, masquage des données, journaux — et **aucune logique métier** : chaque cas d'usage est un profil (`profiles/`).

> Statut : V1, testée sur la configuration ci-dessous. Licence MIT.

## Principe

```
Vous ──> Qwen 3.5 4B (local) : interface, cadrage, routage, contrôle de promesse
              │  commande confirmée par vous
              ▼
         Bus d'orchestration (code Python) : décide, vérifie, journalise
              │  file unique, 12 requêtes/min, arrêt à 1400/jour
              ▼
         Gemini Flash-Lite (gratuit) : organisateur, workers, 3 reviewers
              │
         Qwen 2.5 3B (local) : déduplication, réparation des JSON invalides
```

- **L'organisateur propose, le code décide** : liste fermée de workers, budget de délégations, profondeur maximale, plan invalide refusé en entier, vérification obligatoire de chaque sortie.
- **Pas de pensée de groupe** : chaque worker et chaque reviewer travaille sans voir les réponses des autres.
- **Le code calcule** les votes, scores et shortlists ; les modèles ne font que proposer.
- **Rien ne part vers un modèle externe sans votre confirmation** quand la demande vient du texte libre.

## Configuration minimale testée

| | |
|---|---|
| Système | Windows, Python 3.12 |
| Processeur | AMD Ryzen 7 7800X3D |
| Carte graphique | AMD Radeon RX 7800 XT, 16 Go (Ollama à 100 % GPU) |
| Mémoire | 32 Go |
| Modèles locaux | `qwen3.5:4b-q8_0` (5,2 Go), `qwen2.5:3b-instruct-q4_K_M` (1,9 Go) |
| API | Gemini `gemini-3.5-flash-lite`, niveau gratuit |

Un brainstorm complet (fiche, génération, dédup, revue) prend environ 50 secondes et 6 à 7 requêtes Gemini.

## Installation

1. **Python 3.11 ou plus.** Aucune dépendance obligatoire ; recommandé : `pip install -r requirements.txt`.
2. **Ollama** ([ollama.com](https://ollama.com)), puis :
   ```
   ollama pull qwen3.5:4b-q8_0
   ollama pull qwen2.5:3b-instruct-q4_K_M
   ```
   Vérifiez que les modèles tournent sur la carte graphique : `ollama ps` doit afficher `100% GPU`. Sur une carte AMD sous Windows, vérifiez le support de votre carte par Ollama.
3. **Clé Gemini** : [Google AI Studio](https://aistudio.google.com) > Clés API. Copiez `.env.example` en `.env` (nom exact) et renseignez `GEMINI_API_KEY`. Le programme refuse de démarrer sans clé.
4. **Vérifications** :
   ```
   python -m unittest discover -s tests -t .
   python -m gemini_client      # 1 requête Gemini : doit répondre « OK »
   python -m local_llm          # les deux modèles locaux doivent répondre
   python -m privacy selftest   # coffre chiffré du masquage
   ```
5. **Lancement** : `python -m qwen_ui`

## Quotas Gemini (niveau gratuit)

Le limiteur applique des valeurs prudentes, configurables dans `.env` :

| Règle | Valeur |
|---|---|
| Espacement entre deux requêtes | 5 s (12 par minute) |
| Alerte | 1 200 requêtes par jour |
| Arrêt dur | 1 400 requêtes par jour |
| Retries sur erreur 429 | 2, chacun compté ; le délai demandé par Google est respecté |
| Remise à zéro | minuit, heure du Pacifique |

Toutes les requêtes Gemini (organisateur, workers, reviewers, escalade) passent par **une seule file** et partagent ce budget. Le compteur est enregistré sur disque et survit aux redémarrages.

**Les limites exactes varient selon le modèle et la période** : vérifiez-les pour votre clé dans AI Studio ([page des limites](https://aistudio.google.com/rate-limit)). Lors de la construction (octobre 2026), Flash-Lite offrait 15 requêtes par minute et 1 450 par jour.

## Commandes du terminal

| Commande | Rôle | Requêtes Gemini |
|---|---|---|
| `/fiche <description>` | Qwen rédige la fiche projet, vous la validez | 0 |
| `/tache <consigne>` | l'organisateur découpe et délègue aux workers | 3 à 9 |
| `/dedup` | regroupe les idées en double (Qwen 2.5 3B) | 0 |
| `/revue` | 3 reviewers notent les idées ; vote 2/3 ; shortlist | 3 |
| `/valider <n°>`, `/refuser <n°> [raison]` | vos décisions ; les refus sont transmis à l'organisateur | 0 |
| `/escalader <n°>` | étude par le grand modèle, après contrat figé et confirmation | 1 (Gemini) ou 1 appel Claude |
| `/grand gemini\|claude`, `/organisateur gemini\|claude` | choix du fournisseur | 0 |
| `/profil [nom]`, `/masquer <texte>`, `/donnees` | profils et masquage local | 0 |
| `/detail <n°>`, `/resume`, `/workers`, `/quota`, `/aide`, `/quitter` | consultation | 0 |

Sans « / », Qwen analyse votre message et propose une commande, que vous confirmez (o/n). `GUIDE_PROMPTS_QWEN_V1.md` explique comment rédiger de bonnes fiches et consignes.

## Mode Claude (facultatif, sur votre abonnement)

Le mode Claude passe par **Claude Code** (`claude -p`), connecté à **votre abonnement claude.ai** : il **consomme le quota de votre abonnement**, il n'est jamais gratuit et le programme ne le présente jamais comme tel. Anthropic indique que `claude -p` puise dans les limites de l'abonnement ([article d'aide](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)).

- Installez Claude Code et connectez-vous : `claude auth login` (compte claude.ai).
- Une connexion par clé API est **refusée** (elle facturerait l'API), sauf si vous l'autorisez explicitement avec `CLAUDE_ALLOW_API_BILLING=1`.
- Claude est appelé sans aucun outil (ni fichiers, ni commandes), dans un dossier temporaire vide.
- Un abonnement chat ne donne pas accès à l'API Anthropic ; c'est pourquoi ce mode passe par Claude Code.

## Escalade vers le grand modèle

Une idée est candidate si vous l'avez validée, si 2 reviewers sur 3 l'ont approuvée et si son score de complexité (calculé par le code à partir de l'estimation de Qwen : fichiers touchés, dépendances, risque) dépasse le seuil. Ensuite :

1. le code construit un **contrat** (promesse, périmètre, hors périmètre, contraintes non négociables) que vous validez et qui est figé ;
2. le coût et la durée sont affichés ; **rien n'est appelé sans votre confirmation** ;
3. le grand modèle peut réduire le périmètre, changer l'approche ou échelonner, mais pas changer la promesse, lever une contrainte ou ajouter un élément hors périmètre ;
4. le code vérifie le diff, Qwen répond à une question fermée « la promesse est-elle identique ? » ; au moindre doute la version est rejetée et une nouvelle tentative repart du contrat initial.

## Données personnelles et RGPD

Le module `privacy/` masque les données avant tout envoi : minimisation des champs, détection déterministe (emails, téléphones, IBAN, NIR, SIRET/SIREN, adresses, codes postaux, dates de naissance, noms), indices indirects proposés par Qwen et retenus seulement s'ils sont vérifiables, aperçu à valider, **contrôle de fuite bloquant**, coffre local chiffré (DPAPI sous Windows) avec des jetons stables, réinjection locale des vraies valeurs.

**Limites à connaître :**
- Des données pseudonymisées restent des **données personnelles** (RGPD, considérant 26). Le masquage réduit le risque, il ne vous sort pas du RGPD.
- **Gemini gratuit et l'abonnement Claude sont exclus pour des données clients** (pas d'accord de traitement). Un profil « données personnelles » refuse donc tout fournisseur externe en V1 ; seul le masquage local est disponible. Un usage réel demande une offre payante avec accord de traitement (par exemple Vertex AI en région UE, ou l'API Claude commerciale avec DPA) et probablement une analyse d'impact (DPIA) avec un DPO ou un juriste.
- Le masquage automatique n'est pas parfait : une combinaison d'informations peut rester identifiante, un modèle peut recopier ou inventer une donnée. Sans spaCy (`fr_core_news_md`), un nom isolé sans civilité ni majuscules n'est repéré que s'il figure dans les champs déclarés. Pour des données très sensibles, seul un traitement entièrement local est sûr.
- **Testez uniquement avec des données fictives.**

## Profils

Un profil = un dossier `profiles/<nom>/` avec `profil.json` (workers, consignes, schémas de sortie, seuils, champs) et, facultativement, `donnees.json` (données **fictives**). Le code valide le profil au chargement.

- `brainstorm` : profil par défaut, idées et analyses.
- `bureautique_fictive` : exemple « données personnelles » sur données fictives ; démontre la minimisation, le masquage et le refus des fournisseurs externes.

Un nouveau cas d'usage s'ajoute **sans modifier le squelette** (un test le vérifie).

## Pilote et mesures

`python pilot.py [scenario.txt]` rejoue un scénario sans saisie (confirmations automatiques) et écrit une transcription et des mesures (durées, requêtes par commande) dans `logs/`. Sous Windows, `pilot.bat` le lance d'un double-clic. Les seuils (shortlist, approbation 6,5/10, complexité) sont à calibrer sur vos premiers projets.

## Architecture

```
config.py            quotas, modèles, fournisseurs (valeurs par défaut prudentes)
rate_limiter.py      file unique, espacement, retries, compteur journalier persistant
gemini_client.py     API Gemini (REST, bibliothèque standard), toujours via le limiteur
local_llm.py         modèles locaux, interface unique (Ollama en V1)
claude_cli.py        Claude via Claude Code sur abonnement
orchestrator.py      bus : plan validé, délégations isolées, vérification, synthèse, journal
review.py            reviewers, vote 2/3, shortlist
escalation.py        contrat, grand modèle, diff, contrôles
organizers/          organisateurs Gemini et Claude
workers/             contrat des workers, déduplication, workers par défaut
verify/              schéma JSON, réparation, contrôle de promesse, haiku.py (désactivé)
privacy/             détection, masquage, contrôle de fuite, coffre chiffré, politique
qwen_ui/             terminal, fiche, routage, présentation
profiles/            un dossier par cas d'usage
agents.py           agents interchangeables : rôles, registre, routage, repli
docs/                configuration, agents, calibration, interchangeabilité (en anglais)
tests/               ~445 tests, sans réseau ni modèle réel
```

Aucune clé n'est écrite dans le code ; `.env`, `state/` et `logs/` sont ignorés par git.

## Mode langage libre
Sans « / » initial, vous pouvez parler librement à Qwen. Un agent `reformulation` (Gemini par défaut, remplaçable, ou Qwen local avec `AGENT_REFORMULATION=qwen_interface`) réécrit votre message en demande claire pour Qwen ; le code n'exécute que des commandes d'une liste blanche et confirme les plus coûteuses. Gemini est sollicité **une seule fois par session** pour reformuler vos messages (texte masqué, contrôle de fuite avant chaque envoi) ; un refus garde tout en local. Le profil est choisi implicitement ; un profil de données personnelles est détecté par le code, prioritaire, révoque l'accord et interdit tout envoi externe. Vous pouvez répondre aux résultats en langage naturel (« je garde la 2, la 1 est trop générique ») : le programme confirme, demande une raison une fois (facultative), l'écrit dans `state/retours.jsonl` (local, masqué, ignoré par git) et donne quelques décisions récentes en exemple à Qwen (jamais en profil de données personnelles). Limites : le détecteur de données personnelles peut avoir des faux positifs, et les retours ne s'appliquent qu'à la dernière revue. Non validé avec de vrais modèles.

## Feuille de route

La V1 est volontairement limitée. Prévu pour les prochaines versions :

- **V2 :**
  - [x] Agents IA librement interchangeables (`agents.py` : rôles, registre, routage avec repli, entrée de test `gemini_test` ; voir `docs/interchangeabilite.md`). Validé par tests unitaires avec doubles uniquement, pas avec de vrais modèles.
  - [x] Interface plus simple pilotée par Qwen : mode langage libre (voir ci-dessus). Validé par tests unitaires avec doubles uniquement, pas avec de vrais modèles.
  - [~] Usage du projet sur lui-même : profil `auto_projet` et `docs/auto_projet.md` préparés, jamais exécutés sur de vrais modèles.
- [~] Calibrer les seuils depuis de vrais pilotes : seuils centralisés et configurables, méthode dans `docs/calibration.md` ; aucune calibration réelle faite.
- [~] Backends autres qu'Ollama : abstraction et backend local compatible OpenAI, testés avec des doubles uniquement. Autres plateformes que Windows : non validées, seul Windows est testé.

## Cadre légal des profils

Analyse d'un binaire autorisée seulement si vous le possédez ou avez le droit de l'étudier. Aucun profil n'est fourni pour décompiler un logiciel commercial ou contourner une protection (DRM, anti-triche).

## Licence

MIT — voir `LICENSE`.

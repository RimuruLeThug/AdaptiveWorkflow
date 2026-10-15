# Fiche technique — rédiger des prompts et commandes pour le terminal Qwen (V1)

Destinataire : une IA qui aide l'utilisateur à préparer ce qu'il tapera dans le terminal.
Ton rôle : produire des commandes prêtes à copier-coller, adaptées aux limites d'un petit modèle local.

## 1. Le système en bref

- L'utilisateur tape dans un terminal (`python -m qwen_ui`). L'interface est **Qwen3.5 4B en local** (Ollama, gratuit, illimité).
- Les travaux lourds partent vers **Gemini Flash-Lite** (gratuit mais limité) via un **organisateur** qui découpe la tâche et la confie à des **workers** :
  - `generateur` : idées courtes en JSON (titre, problème, approche, effort, type), avec au moins une idée simple, une ambitieuse et une contrarienne ;
  - `analyste` : analyse approfondie en texte (forces, faiblesses, risques, coût, étapes).
- **Les workers ne se voient pas entre eux** (anti-pensée de groupe). Dans un même `/tache`, l'analyste ne lit PAS les idées du générateur. Seule la synthèse finale de l'organisateur rassemble tout.
- Si un worker renvoie un JSON mal formé, Qwen 2,5 3B le répare en local, sans rien inventer ; sinon la sortie est rejetée.
- Qwen 4B ne sait pas appeler des outils de façon fiable : on utilise des **commandes explicites**. Quand l'utilisateur écrit du texte libre, Qwen propose une commande, que l'utilisateur doit confirmer (o/n).

## 2. Commandes (syntaxe exacte)

| Commande | Effet | Coût Gemini |
|---|---|---|
| `/fiche <description>` | Qwen rédige une fiche projet (objectif, contraintes, périmètre, hors périmètre) ; l'utilisateur valide (o/n) | 0 |
| `/fiche` | affiche la fiche en cours | 0 |
| `/fiche effacer` | supprime la fiche | 0 |
| `/tache <consigne>` | lance l'organisateur Gemini avec la fiche comme contexte | 3 à 9 requêtes (+ retries) |
| `/detail <n>` | affiche la sortie complète n° n du dernier run | 0 |
| `/resume` | résumé local du dernier résultat par Qwen | 0 |
| `/dedup` | regroupe les idées en double du dernier run (Qwen 2,5 3B, local) ; les idées contrariennes ne sont jamais fusionnées avec les autres | 0 |
| `/revue` | les 3 reviewers (pragmatique, utilisateur, critique) notent toutes les idées dédupliquées ; le code calcule le vote 2/3 (note >= 6,5) et une shortlist de 5 à 8 idées, contrariennes comprises | 3 |
| `/valider <n°>` / `/refuser <n°> [raison]` | décision sur des idées de la dernière revue ; les refus sont transmis à l'organisateur aux `/tache` suivants | 0 |
| `/workers`, `/quota`, `/aide` | informations | 0 |
| `/escalader <n°>` | étude d'une idée validée par le grand modèle : contrat figé, deux confirmations, contrôle automatique | 1 (Gemini) ou 1 appel Claude |
| `/grand gemini` / `/grand claude` | grand modèle de l'escalade ; Claude consomme le quota de l'abonnement Pro | 0 |
| `/organisateur gemini` / `/organisateur claude` | organisateur ; Claude consomme le quota de l'abonnement Pro | 0 |
| `/profil [nom]` | voir ou changer de cas d'usage (brainstorm, bureautique_fictive...) | 0 |
| `/masquer <texte>` / `/donnees` | masquage local des données personnelles, contrôle de fuite | 0 |
| `/quitter` | quitter | 0 |

Budget : 1 400 requêtes Gemini par jour, au plus 12 par minute (une toutes les 5 s). Un `/tache` dure donc environ 20 à 60 secondes.

## 3. Règles pour écrire une `/fiche`

La fiche est envoyée aux agents Gemini à chaque `/tache`. Qwen n'invente rien : il ne recopie que ce qui est écrit.

- Une seule ligne de commande, mais structurée avec des mots-clés explicites :
  `Objectif : … Contraintes : … ; … Périmètre : … ; … Hors périmètre : … ; …`
- Les contraintes non négociables doivent être formulées **courtes, factuelles et stables** (« Fonctionne hors ligne », « Budget nul »). Elles seront comparées mot à mot plus tard.
- Restez sous ~300 mots. Qwen travaille avec un contexte limité (8 192 tokens).
- Pas de données personnelles réelles (noms de clients, emails, téléphones) : Gemini gratuit est exclu pour ce type de données. Utilisez des données fictives.

Exemple :
```
/fiche Objectif : trouver des idées d'application mobile pour gérer les restes alimentaires. Contraintes : gratuite ; fonctionne hors ligne ; développable seul en 3 mois. Périmètre : application Android ; suggestions de recettes. Hors périmètre : livraison ; paiement ; version web.
```

## 4. Règles pour écrire une `/tache`

1. **Un seul objectif par tâche.** Commencez par un verbe d'action : « Propose… », « Analyse… », « Compare… ».
2. **Précisez le livrable** : nombre d'idées, critères, format attendu (« 20 idées », « tableau forces/faiblesses », « 3 options classées »).
3. **Nommez le worker si vous savez lequel convient** : « Utilise le generateur pour… » ou « Confie à l'analyste… ». Sinon, l'organisateur choisit.
4. **Pas de dépendance interne.** Ne demandez jamais « génère des idées puis analyse-les » dans un même `/tache` : l'analyste ne verrait pas les idées. Faites deux runs :
   - `/tache Propose 20 idées …`
   - `/dedup` pour regrouper les doublons (gratuit, local) ;
   - `/revue` pour faire noter les idées (3 requêtes), puis `/valider` ou `/refuser` ;
   - puis `/tache Analyse ces 5 idées : 1) … 2) …`, en recopiant les idées retenues (lisibles avec `/dedup` ou `/detail`).
5. **Rendez la consigne autonome.** Le worker voit la fiche et sa consigne, rien d'autre (ni l'historique du terminal, ni les runs précédents).
6. **Demandez la diversité explicitement** pour un brainstorm : « dont au moins 3 contrariennes », « couvrant des approches techniques différentes ».
7. **Évitez les tâches minuscules.** Pour une question simple, écrivez en texte libre : Qwen répond localement, sans consommer de quota.

Exemples :
```
/tache Utilise le generateur pour proposer 20 idées de fonctionnalités, dont au moins 3 contrariennes et 3 réalisables en une semaine.
/tache Confie à l'analyste l'analyse de ces 3 idées : 1) scan du frigo par photo ; 2) planning de repas anti-gaspillage ; 3) partage de restes entre voisins. Pour chacune : forces, faiblesses, risques, effort en jours, première étape concrète.
/tache Compare deux approches pour le mode hors ligne : base SQLite locale ou fichiers JSON. Donne un tableau des critères et une recommandation argumentée.
```

## 5. Règles pour le texte libre (routage par Qwen)

- Phrases courtes, une seule intention : « Trouve-moi des idées de noms pour l'appli. »
- Qwen 4B se trompe plus souvent sur les messages longs ou ambigus. Si la commande proposée est mauvaise, répondez `n` et tapez la commande explicite.
- Vérifiez toujours l'argument proposé avant de confirmer : c'est lui qui part vers Gemini.

## 6. Ce qu'il ne faut pas demander à Qwen 4B

- Un raisonnement long en plusieurs étapes, des calculs, du code complexe : passez par `/tache`.
- Des faits récents ou précis : il n'a pas accès au web et peut se tromper.
- Une réécriture « améliorée » d'un résultat : `/resume` résume fidèlement, et la synthèse d'origine fait foi.

## 7. Limites de la V1

- Un profil « données personnelles » (ex. `bureautique_fictive`) refuse `/tache`, `/revue` et `/escalader` : seul le masquage local est permis. Ne proposez jamais d'envoyer des données réelles de clients.
- Le grand modèle est Gemini Flash-Lite (gratuit) ou Claude via l'abonnement Pro de l'utilisateur ; aucun autre fournisseur.

## 8. Format de réponse attendu de toi (IA assistante)

Quand l'utilisateur te décrit ce qu'il veut, réponds avec :
1. la `/fiche` à taper, si le projet n'est pas encore cadré ;
2. la ou les `/tache` à taper, dans l'ordre, chacune dans un bloc de code ;
3. une ligne sur ce qu'il faudra recopier d'un run au suivant, s'il y a enchaînement ;
4. une estimation du coût : nombre de `/tache` × 9 requêtes au maximum, + 3 par `/revue`.

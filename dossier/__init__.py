"""Travail sur un dossier du PC (V3.2).

Le code décide, les modèles proposent :

- inventaire.py : choisir le dossier, l'inventorier et lire ses fichiers, en LECTURE SEULE
  (aucune écriture possible dans ce module) ; fichiers sensibles (.env, clés...) jamais lus.
- analyse.py : prompts et schémas des appels au rôle « dossier » (comprendre le dossier,
  choisir les fichiers utiles, répondre à une question, proposer des modifications) ;
  le code valide chaque chemin cité.
- modifications.py : transforme une proposition en changements vérifiés (chemins dans le
  dossier, fichier lu avant d'être modifié, extrait à remplacer présent une seule fois),
  affiche le diff, et SEULE fonction qui écrit dans le dossier : appliquer(), appelée
  uniquement après le « oui » explicite de l'utilisateur, avec sauvegarde préalable et
  annulation possible.
"""

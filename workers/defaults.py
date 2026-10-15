"""Workers par défaut du squelette : Worker 1 et Worker 2.

Le profil « brainstorm » (profiles/brainstorm/profil.json) déclare les mêmes
workers ; un test vérifie qu'ils restent identiques. Cette liste sert de repli
quand aucun profil n'est chargé.
"""

from __future__ import annotations

from workers.base import WorkerSpec

IDEAS_SCHEMA = {
    "type": "object",
    "required": ["idees"],
    "additionalProperties": False,
    "properties": {
        "idees": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "required": ["titre", "probleme", "approche", "effort", "type"],
                "additionalProperties": False,
                "properties": {
                    "titre": {"type": "string", "minLength": 1, "maxLength": 120},
                    "probleme": {"type": "string", "minLength": 1, "maxLength": 400},
                    "approche": {"type": "string", "minLength": 1, "maxLength": 400},
                    "effort": {"type": "string", "enum": ["faible", "moyen", "fort"]},
                    "type": {"type": "string", "enum": ["simple", "ambitieuse", "contrarienne", "autre"]},
                },
            },
        }
    },
}

GENERATEUR = WorkerSpec(
    name="generateur",
    description=(
        "Génère des idées courtes et variées en JSON (titre, problème, approche, effort, type). "
        "Inclut toujours au moins une idée simple, une ambitieuse et une contrarienne."
    ),
    system_prompt=(
        "Tu génères des idées. Chaque idée est courte et concrète. Varie les approches : "
        "au moins une idée simple, une ambitieuse et une contrarienne (qui prend le contre-pied "
        "de l'évidence). N'écris rien en dehors du JSON demandé. Format : "
        '{"idees": [{"titre": "...", "probleme": "...", "approche": "...", '
        '"effort": "faible|moyen|fort", "type": "simple|ambitieuse|contrarienne|autre"}]}'
    ),
    temperature=0.9,
    output_schema=IDEAS_SCHEMA,
)

ANALYSTE = WorkerSpec(
    name="analyste",
    description="Analyse approfondie d'idées retenues ou d'une tâche technique (texte structuré).",
    system_prompt=(
        "Tu es un analyste rigoureux. Tu examines ce qu'on te confie : forces, faiblesses, "
        "risques, coût et étapes concrètes. Tu distingues les faits des hypothèses et tu "
        "signales ce qui manque pour conclure. Pas de formules creuses."
    ),
    temperature=0.4,
)

DEFAULT_WORKERS = (GENERATEUR, ANALYSTE)

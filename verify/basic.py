"""Vérification de base d'une sortie de worker, appliquée par le bus avant tout retour.

Retourne la liste des problèmes ; une liste vide signifie « vérifié ».
Le contrôle de promesse (Qwen 3.5 4B) est dans verify/promise.py.
"""

from __future__ import annotations

from typing import Callable, List

from verify.schema import SchemaError, parse_json
from workers.base import Reply, WorkerSpec

Verifier = Callable[[WorkerSpec, Reply], List[str]]


def basic_verifier(spec: WorkerSpec, reply: Reply) -> List[str]:
    issues: List[str] = []
    if not reply.text.strip():
        issues.append("sortie vide")
    if reply.truncated:
        issues.append("sortie tronquée (limite de tokens atteinte)")
    if spec.output_schema is not None and reply.text.strip():
        try:
            parse_json(reply.text, spec.output_schema)
        except SchemaError as exc:
            issues.append(f"JSON non conforme : {exc}")
    return issues

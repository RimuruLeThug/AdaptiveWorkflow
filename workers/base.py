"""Contrat commun des workers.

Isolement (anti-pensée de groupe) : un worker ne reçoit QUE sa consigne et la
fiche projet. Il n'a aucun accès aux réponses des autres workers ni au bus.
C'est garanti par la signature de run(), pas par une consigne au modèle.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass(frozen=True)
class Reply:
    """Réponse d'un modèle, avec les métadonnées utiles aux mesures du pilote."""

    text: str
    attempts: int = 1  # requêtes réellement envoyées (retries compris)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_s: float = 0.0
    truncated: bool = False


@dataclass(frozen=True)
class WorkerSpec:
    """Déclaration d'un worker par un profil. La liste des workers est fermée."""

    name: str  # identifiant utilisé par l'organisateur : minuscules, chiffres, _
    description: str  # ce que l'organisateur voit pour choisir
    system_prompt: str
    temperature: float = 0.7
    output_schema: Optional[dict] = None  # si présent : sortie JSON validée par le code
    tools: tuple[str, ...] = field(default_factory=tuple)  # outils autorisés par le profil

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"nom de worker invalide : {self.name!r} (attendu : [a-z][a-z0-9_]*, 40 max)")
        if not self.description.strip():
            raise ValueError(f"worker {self.name} : description vide")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(f"worker {self.name} : température hors de [0, 2]")


class Worker(ABC):
    def __init__(self, spec: WorkerSpec) -> None:
        self.spec = spec

    @abstractmethod
    def run(self, instruction: str, context: str) -> Reply:
        """Exécute UNE consigne. context = fiche projet (données, pas des ordres)."""

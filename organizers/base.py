"""Interface commune des organisateurs.

L'organisateur PROPOSE (un plan en texte brut, une synthèse) ; le bus
(orchestrator.py) DÉCIDE : il valide le plan, applique la liste fermée de
workers, le budget de délégations et la profondeur maximale.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence

from workers.base import Reply, WorkerSpec


@dataclass(frozen=True)
class WorkerResult:
    """Résultat vérifié d'un worker, tel que transmis à la synthèse."""

    worker: str
    instruction: str
    text: str


class Organizer(ABC):
    @abstractmethod
    def propose_plan(
        self,
        task: str,
        context: str,
        workers: Sequence[WorkerSpec],
        max_tasks: int,
        feedback: Optional[str] = None,
    ) -> Reply:
        """Retourne un plan JSON brut : {"tasks": [{"worker": ..., "instruction": ...}]}."""

    @abstractmethod
    def synthesize(self, task: str, context: str, results: Sequence[WorkerResult]) -> Reply:
        """Rassemble les résultats vérifiés des workers."""

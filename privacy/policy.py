"""Fournisseurs autorisés selon les données (brief, section 7, règles juridiques).

- Gemini gratuit : exclu pour des données clients (pas d'accord de traitement).
- Claude via l'abonnement Pro (claude.ai) : offre grand public, pas d'accord de
  traitement (DPA) ; exclu aussi.
- Usage réel de données clients : offre payante avec DPA (Vertex AI en région UE,
  API Claude commerciale avec DPA) — hors V1.
- Modèles locaux (Ollama) : autorisés, rien ne quitte la machine.

Un profil « données personnelles » refuse donc tout fournisseur externe en V1,
même après masquage (données pseudonymisées = données personnelles, considérant 26).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ProviderInfo:
    label: str
    external: bool  # les données quittent la machine
    dpa: bool  # accord de traitement des données (RGPD art. 28) en place


PROVIDERS: Dict[str, ProviderInfo] = {
    "gemini": ProviderInfo("Gemini, niveau gratuit", external=True, dpa=False),
    "claude": ProviderInfo("Claude via abonnement Pro (claude.ai)", external=True, dpa=False),
    "local": ProviderInfo("Modèles locaux Ollama", external=False, dpa=True),
}


class PolicyError(Exception):
    """Fournisseur interdit pour ce type de données."""


def check_provider(provider: str, *, personal_data: bool) -> None:
    info = PROVIDERS.get(provider)
    if info is None:
        raise PolicyError(f"fournisseur inconnu : {provider}")
    if personal_data and info.external and not info.dpa:
        raise PolicyError(
            f"{info.label} est interdit pour un profil « données personnelles » : pas d'accord de "
            "traitement des données. Données pseudonymisées = données personnelles (RGPD, considérant 26). "
            "Utilisez des données fictives, ou une offre payante avec DPA (hors V1).")

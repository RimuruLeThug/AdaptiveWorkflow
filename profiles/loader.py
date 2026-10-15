"""Profils : un dossier par cas d'usage (brief, section 12).

    profiles/<nom>/profil.json      déclaration (workers, critères, données)
    profiles/<nom>/donnees.json     données FICTIVES d'exemple (facultatif)

Le squelette ne contient aucune logique métier : tout ce qui est propre à un
cas d'usage (workers, consignes, schémas de sortie, seuils, champs) vient du
profil, validé par le code au chargement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agents import validate_profile_agents
from verify.schema import SchemaError, parse_json
from workers.base import WorkerSpec
from workers.defaults import IDEAS_SCHEMA

PROFILES_DIR = Path(__file__).resolve().parent
NAMED_SCHEMAS = {"idees": IDEAS_SCHEMA}

PROFILE_SCHEMA = {
    "type": "object",
    "required": ["nom", "description", "donnees_personnelles", "workers"],
    "additionalProperties": False,
    "properties": {
        "nom": {"type": "string", "minLength": 1, "maxLength": 40},
        "description": {"type": "string", "minLength": 1, "maxLength": 500},
        "donnees_personnelles": {"type": "boolean"},
        "workers": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "required": ["nom", "description", "consigne_systeme"],
                "additionalProperties": False,
                "properties": {
                    "nom": {"type": "string"},
                    "description": {"type": "string"},
                    "consigne_systeme": {"type": "string", "minLength": 1},
                    "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                    "schema_sortie": {"type": ["string", "object", "null"]},
                },
            },
        },
        "revue": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "seuil": {"type": "number", "minimum": 0, "maximum": 10},
                "shortlist_min": {"type": "integer", "minimum": 1},
                "shortlist_max": {"type": "integer", "minimum": 1},
            },
        },
        "champs_utiles": {"type": "array", "items": {"type": "string"}},
        "champs_personnels": {"type": "array", "items": {"type": "string"}},
        "exemples": {"type": "array", "items": {"type": "string"}},
        # Agents interchangeables : déclarations et affectation rôle -> agent (voir agents.py).
        "agents": {"type": "object"},
    },
}


class ProfileError(Exception):
    """Profil introuvable ou invalide."""


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    personal_data: bool
    workers: Tuple[WorkerSpec, ...]
    review: Dict[str, float] = field(default_factory=dict)
    useful_fields: Tuple[str, ...] = ()
    personal_fields: Tuple[str, ...] = ()
    examples: Tuple[str, ...] = ()
    records: Tuple[dict, ...] = ()  # données fictives d'exemple, minimisées au chargement
    agents: Dict[str, dict] = field(default_factory=dict)  # clé « agents » validée (agents.py)


def _worker(w: dict) -> WorkerSpec:
    schema = w.get("schema_sortie")
    if isinstance(schema, str):
        if schema not in NAMED_SCHEMAS:
            raise ProfileError(f"schéma de sortie inconnu : {schema!r} (connus : {', '.join(NAMED_SCHEMAS)})")
        schema = NAMED_SCHEMAS[schema]
    try:
        return WorkerSpec(name=w["nom"], description=w["description"], system_prompt=w["consigne_systeme"],
                          temperature=float(w.get("temperature", 0.7)), output_schema=schema)
    except ValueError as exc:
        raise ProfileError(f"worker invalide : {exc}") from None


def load_profile(name: str, base: Path = PROFILES_DIR) -> Profile:
    folder = Path(base) / name
    path = folder / "profil.json"
    if not path.exists():
        raise ProfileError(f"profil introuvable : {name} (profils : {', '.join(list_profiles(base)) or 'aucun'})")
    try:
        doc = parse_json(path.read_text(encoding="utf-8"), PROFILE_SCHEMA)
    except SchemaError as exc:
        raise ProfileError(f"profil {name} invalide : {exc}") from None
    workers = tuple(_worker(w) for w in doc["workers"])
    names = [w.name for w in workers]
    if len(set(names)) != len(names):
        raise ProfileError(f"profil {name} : worker déclaré deux fois")
    review = doc.get("revue", {})
    if review.get("shortlist_min", 1) > review.get("shortlist_max", 99):
        raise ProfileError(f"profil {name} : shortlist_min > shortlist_max")
    useful = tuple(doc.get("champs_utiles", []))
    personal = tuple(doc.get("champs_personnels", []))
    if doc["donnees_personnelles"] and not useful:
        raise ProfileError(f"profil {name} : un profil « données personnelles » doit déclarer ses champs utiles "
                           "(minimisation)")
    records: Tuple[dict, ...] = ()
    data_path = folder / "donnees.json"
    if data_path.exists():
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            raise ProfileError(f"profil {name} : donnees.json doit être une liste d'objets")
        if useful:  # minimisation dès le chargement (brief, section 7, étape 1)
            raw = [{k: r[k] for k in useful if k in r} for r in raw]
        records = tuple(raw)
    try:
        agents_section = validate_profile_agents(doc.get("agents"), personal_data=doc["donnees_personnelles"])
    except ValueError as exc:
        raise ProfileError(f"profil {name} : agents invalides : {exc}") from None
    return Profile(doc["nom"], doc["description"], doc["donnees_personnelles"], workers, review, useful,
                   personal, tuple(doc.get("exemples", [])), records, agents_section)


def list_profiles(base: Path = PROFILES_DIR) -> List[str]:
    return sorted(p.parent.name for p in Path(base).glob("*/profil.json"))

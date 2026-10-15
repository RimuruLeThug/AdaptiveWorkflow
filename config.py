"""Configuration centrale : quotas, modèles, fournisseur du grand modèle.

Les valeurs par défaut sont celles du brief (sections 4, 5 et 6). Elles sont
volontairement prudentes et peuvent être surchargées par variables
d'environnement. Aucune clé n'est jamais écrite dans ce fichier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / "state"  # fichiers locaux (compteurs) ; ignoré par git
LOGS_DIR = PROJECT_ROOT / "logs"  # journaux des runs ; ignoré par git


class MissingKeyError(RuntimeError):
    """Une clé API requise est absente : le programme refuse de démarrer."""


def load_dotenv(path: Path | None = None) -> None:
    """Charge un fichier .env minimal (CLE=valeur) sans écraser l'environnement existant.

    Format accepté : une affectation par ligne, commentaires « # » en début de
    ligne ou après un espace, guillemets simples ou doubles optionnels.
    """
    path = Path(path) if path else PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def gemini_api_key() -> str:
    """Retourne la clé Gemini ou refuse de démarrer.

    La clé n'est volontairement pas stockée dans Settings, pour qu'elle
    n'apparaisse jamais dans un repr ou un journal.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        msg = (
            "GEMINI_API_KEY est absente. Créez un fichier nommé exactement .env à la racine du projet "
            "contenant la ligne GEMINI_API_KEY=votre_clé (Google AI Studio > Clés API)."
        )
        env = PROJECT_ROOT / ".env"
        others = sorted(p.name for p in PROJECT_ROOT.glob("*.env*") if p.name not in (".env", ".env.example"))
        if not env.exists() and others:
            msg += (f" Fichier(s) trouvé(s) avec un autre nom : {', '.join(others)}. "
                    f"Renommez-le en .env (PowerShell : Rename-Item {others[0]} .env).")
        elif env.exists():
            msg += " Le fichier .env existe mais ne contient pas de ligne GEMINI_API_KEY=... non vide."
        raise MissingKeyError(msg)
    return key


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw in (None, "") else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw in (None, "") else float(raw)


@dataclass(frozen=True)
class GeminiQuota:
    """Quota d'un compartiment Gemini (brief, section 4)."""

    # Limites du plan gratuit, rappelées pour référence (non utilisées pour décider).
    provider_rpm: int = 15
    provider_rpd: int = 1450
    # Valeurs appliquées par le code.
    min_interval_s: float = 5.0  # 12 RPM visés
    daily_alert: int = 1200
    daily_hard_stop: int = 1400
    max_retries: int = 2  # retries sur 429, chacun compté dans le budget
    reset_timezone: str = "America/Los_Angeles"  # minuit, heure du Pacifique


# Rôles des agents (roadmap V2, agents interchangeables, voir agents.py). Chaque rôle
# peut être confié à un autre agent par variable d'environnement AGENT_<ROLE>
# (ex. AGENT_ROUTAGE=gemini_test,qwen_interface : agent principal puis replis) et
# AGENT_<ROLE>_DIFFICILE (agent des tâches jugées difficiles par le code).
AGENT_ROLES = (
    "organisateur", "worker", "relecteur", "grand_modele",  # Gemini (ou Claude) par défaut
    "interface", "cadrage", "routage", "promesse",  # Qwen 3.5 4B par défaut
    "format", "dedup", "reparation",  # Qwen 2.5 3B par défaut
)


def _env_roles(suffix: str = "") -> tuple:
    out = []
    for role in AGENT_ROLES:
        raw = os.environ.get(f"AGENT_{role.upper()}{suffix}", "").strip()
        if raw:
            out.append((role, raw))
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    gemini: GeminiQuota = field(default_factory=GeminiQuota)

    # Modèle Gemini des organisateur, workers et reviewers (niveau gratuit).
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_s: float = 120.0

    # Bus d'orchestration (brief, section 3) : l'organisateur propose, le code décide.
    delegation_budget: int = 6  # délégations maximum par tâche, toutes tentatives confondues
    max_depth: int = 1  # l'organisateur délègue aux workers ; les workers ne délèguent pas
    max_plan_attempts: int = 2  # un plan refusé peut être corrigé une fois (appel compté)

    # Revue et vote (brief, sections 3 et 11) — à calibrer pendant le pilote (section 14).
    review_threshold: float = 6.5  # note minimale pour qu'un reviewer « approuve »
    review_votes_needed: int = 2  # vote 2/3
    shortlist_min: int = 5
    shortlist_max: int = 8
    contrarian_quota: int = 2  # contrariennes conservées même à score moyen...
    contrarian_floor: float = 4.0  # ... si leur score pondéré atteint ce plancher
    max_review_ideas: int = 40

    # Grand modèle (brief, section 5) : "gemini" (Flash-Lite, gratuit) ou "claude"
    # (Claude Code, abonnement Pro de l'utilisateur, jamais l'API sans autorisation).
    big_model_provider: str = "gemini"
    claude_model: str = "sonnet"  # alias Claude Code : sonnet, opus, haiku...
    claude_timeout_s: float = 600.0
    claude_allow_api_billing: bool = False
    # Score de complexité au-delà duquel une idée est candidate à l'escalade.
    # Valeur provisoire basse, à calibrer sur les premiers projets (section 14).
    complexity_threshold: int = 5

    # Modèles locaux via Ollama (brief, section 6). Tags de la bibliothèque
    # Ollama, alignés sur `ollama list` de la machine de référence. Autre variante :
    # QWEN_INTERFACE_MODEL / QWEN_FORMAT_MODEL dans .env.
    local_backend: str = "ollama"
    ollama_host: str = "http://localhost:11434"
    qwen_interface_model: str = "qwen3.5:4b-q8_0"  # « Qwen 5 Go » : interface, cadrage, routage, contrôle de promesse
    qwen_format_model: str = "qwen2.5:3b-instruct-q4_K_M"  # « Qwen 2 Go » : dédup, réparation JSON
    qwen_interface_num_ctx: int = 8192
    qwen_format_num_ctx: int = 16384  # réparation : document d'entrée + document réparé
    # Durée de maintien des modèles en VRAM après la dernière requête (ex. 2m, 30s, 0 = déchargé
    # aussitôt). Les points d'entrée déchargent en plus les modèles à l'arrêt du programme.
    ollama_keep_alive: str = "2m"

    state_dir: Path = STATE_DIR
    logs_dir: Path = LOGS_DIR
    profile: str = "brainstorm"  # profil chargé au démarrage (profiles/<nom>/profil.json)

    # Agents interchangeables (agents.py). Entrée de test « gemini_test » : agent Gemini
    # dont le modèle vient UNIQUEMENT de GEMINI_TEST_MODEL ; vide = agent indisponible.
    # Il passe par la même file du limiteur que les autres appels Gemini.
    gemini_test_model: str = ""
    # Affectation rôle -> agents, lue dans AGENT_<ROLE> ; vide = défauts du projet.
    agent_roles: tuple = ()  # ((rôle, "agent[,repli...]"), ...)
    agent_roles_difficile: tuple = ()  # ((rôle, agent), ...)
    # Critère déterministe du routage simple/difficile (seulement si un agent « difficile » est configuré).
    agent_seuil_caracteres: int = 6000
    agent_seuil_score: int = 5
    # Agents supplémentaires (JSON, même format que la clé « declarations » d'un profil).
    agent_declarations: str = ""


def load_settings() -> Settings:
    """Construit la configuration à partir de l'environnement et des valeurs par défaut."""
    q = GeminiQuota()
    gemini = GeminiQuota(
        min_interval_s=_env_float("GEMINI_MIN_INTERVAL_S", q.min_interval_s),
        daily_alert=_env_int("GEMINI_DAILY_ALERT", q.daily_alert),
        daily_hard_stop=_env_int("GEMINI_DAILY_HARD_STOP", q.daily_hard_stop),
        max_retries=_env_int("GEMINI_MAX_RETRIES", q.max_retries),
    )
    d = Settings()
    return Settings(
        gemini=gemini,
        gemini_model=os.environ.get("GEMINI_MODEL", d.gemini_model).strip(),
        gemini_timeout_s=_env_float("GEMINI_TIMEOUT_S", d.gemini_timeout_s),
        delegation_budget=_env_int("DELEGATION_BUDGET", d.delegation_budget),
        max_depth=_env_int("MAX_DEPTH", d.max_depth),
        max_plan_attempts=_env_int("MAX_PLAN_ATTEMPTS", d.max_plan_attempts),
        review_threshold=_env_float("REVIEW_THRESHOLD", d.review_threshold),
        review_votes_needed=_env_int("REVIEW_VOTES_NEEDED", d.review_votes_needed),
        shortlist_min=_env_int("SHORTLIST_MIN", d.shortlist_min),
        shortlist_max=_env_int("SHORTLIST_MAX", d.shortlist_max),
        contrarian_quota=_env_int("CONTRARIAN_QUOTA", d.contrarian_quota),
        contrarian_floor=_env_float("CONTRARIAN_FLOOR", d.contrarian_floor),
        max_review_ideas=_env_int("MAX_REVIEW_IDEAS", d.max_review_ideas),
        big_model_provider=os.environ.get("BIG_MODEL_PROVIDER", d.big_model_provider).strip().lower(),
        claude_model=os.environ.get("CLAUDE_MODEL", d.claude_model).strip(),
        claude_timeout_s=_env_float("CLAUDE_TIMEOUT_S", d.claude_timeout_s),
        claude_allow_api_billing=os.environ.get("CLAUDE_ALLOW_API_BILLING", "").strip() in ("1", "true", "oui"),
        complexity_threshold=_env_int("COMPLEXITY_THRESHOLD", d.complexity_threshold),
        ollama_host=os.environ.get("OLLAMA_HOST_URL", d.ollama_host),
        qwen_interface_model=os.environ.get("QWEN_INTERFACE_MODEL", d.qwen_interface_model),
        qwen_format_model=os.environ.get("QWEN_FORMAT_MODEL", d.qwen_format_model),
        qwen_interface_num_ctx=_env_int("QWEN_INTERFACE_NUM_CTX", d.qwen_interface_num_ctx),
        qwen_format_num_ctx=_env_int("QWEN_FORMAT_NUM_CTX", d.qwen_format_num_ctx),
        ollama_keep_alive=os.environ.get("LOCAL_KEEP_ALIVE", d.ollama_keep_alive).strip() or d.ollama_keep_alive,
        state_dir=Path(os.environ.get("ORCH_STATE_DIR", str(d.state_dir))),
        logs_dir=Path(os.environ.get("ORCH_LOGS_DIR", str(d.logs_dir))),
        profile=os.environ.get("PROFILE", d.profile).strip(),
        gemini_test_model=os.environ.get("GEMINI_TEST_MODEL", "").strip(),
        agent_roles=_env_roles(),
        agent_roles_difficile=_env_roles("_DIFFICILE"),
        agent_seuil_caracteres=_env_int("AGENT_SEUIL_CARACTERES", d.agent_seuil_caracteres),
        agent_seuil_score=_env_int("AGENT_SEUIL_SCORE", d.agent_seuil_score),
        agent_declarations=os.environ.get("AGENT_DECLARATIONS", "").strip(),
    )

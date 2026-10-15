"""Assemblage de l'application réelle (terminal et pilote partagent ce code).

Le compteur de quota Gemini est TOUJOURS celui de config.state_dir : un pilote
ou un autre point d'entrée ne peut pas contourner le budget journalier.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

import config
from gemini_client import GeminiClient
from local_llm import LocalLLMError, OllamaBackend
from orchestrator import Orchestrator
from organizers.gemini import GeminiOrganizer
from qwen_ui.app import App
from rate_limiter import RateLimiter
from review import ReviewPanel
from verify.basic import basic_verifier
from verify.repair import repair_json
from claude_cli import ClaudeCodeClient
from escalation import Escalator
from organizers.claude import ClaudeOrganizer
from privacy.detectors import spacy_name_detector
from privacy.masker import Masker
from privacy.vault import Vault, VaultError, default_cipher
from profiles.loader import Profile, ProfileError, list_profiles, load_profile
from workers.defaults import DEFAULT_WORKERS
from workers.gemini import GeminiWorker


class StartupError(Exception):
    """Le programme refuse de démarrer (clé absente, Qwen indisponible...)."""


def safe_console() -> None:
    """Console Windows : ne jamais planter sur un caractère non affichable."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def build_app(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    app_state_dir: Optional[Path] = None,
) -> App:
    """app_state_dir : dossier de la fiche et des décisions (par défaut config.state_dir).
    Le compteur de quota reste dans config.state_dir quoi qu'il arrive."""
    config.load_dotenv()
    s = config.load_settings()
    try:
        key = config.gemini_api_key()
    except config.MissingKeyError as exc:
        raise StartupError(str(exc)) from None

    llm = OllamaBackend(s.qwen_interface_model, host=s.ollama_host, num_ctx=s.qwen_interface_num_ctx,
                        keep_alive=s.ollama_keep_alive)
    try:
        llm.check_available()
    except LocalLLMError as exc:
        raise StartupError(f"Interface Qwen indisponible : {exc}") from None

    format_llm: Optional[OllamaBackend] = OllamaBackend(
        s.qwen_format_model, host=s.ollama_host, num_ctx=s.qwen_format_num_ctx, think=None,
        keep_alive=s.ollama_keep_alive)
    try:
        format_llm.check_available()
    except LocalLLMError as exc:
        output_fn(f"Attention : Qwen 2 Go indisponible, réparation JSON et déduplication limitées. {exc}")
        format_llm = None

    try:
        profile: Optional[Profile] = load_profile(s.profile)
    except ProfileError as exc:
        raise StartupError(f"Profil de démarrage invalide : {exc}") from None

    limiter = RateLimiter(s.gemini, s.state_dir / "gemini_quota.json")
    client = GeminiClient(key, s.gemini_model, limiter, timeout_s=s.gemini_timeout_s)

    def repairer(text: str, schema: dict):
        return repair_json(text, schema, format_llm)

    claude = ClaudeCodeClient(model=s.claude_model, timeout_s=s.claude_timeout_s,
                              allow_api_billing=s.claude_allow_api_billing,
                              counter_path=s.state_dir / "claude_calls.json")

    def factory(organizer: str, prof: Optional[Profile] = None) -> Orchestrator:
        if organizer not in ("gemini", "claude"):
            raise ValueError(f"organisateur non disponible : {organizer}")
        specs = prof.workers if prof else DEFAULT_WORKERS
        return Orchestrator(
            ClaudeOrganizer(claude) if organizer == "claude" else GeminiOrganizer(client),
            [GeminiWorker(spec, client) for spec in specs],
            basic_verifier,
            delegation_budget=s.delegation_budget,
            max_depth=s.max_depth,
            max_plan_attempts=s.max_plan_attempts,
            logs_dir=s.logs_dir,
            request_counter=lambda: client.requests_sent,
            repairer=repairer,
        )

    def review_factory(prof: Optional[Profile] = None) -> ReviewPanel:
        over = prof.review if prof else {}
        return ReviewPanel(
            client,
            threshold=over.get("seuil", s.review_threshold),
            votes_needed=s.review_votes_needed,
            shortlist_min=int(over.get("shortlist_min", s.shortlist_min)),
            shortlist_max=int(over.get("shortlist_max", s.shortlist_max)),
            contrarian_quota=s.contrarian_quota,
            contrarian_floor=s.contrarian_floor,
            max_ideas=s.max_review_ideas,
            repairer=repairer,
            logs_dir=s.logs_dir,
        )

    def escalator_factory(provider: str) -> Escalator:
        big = claude if provider == "claude" else client
        label = f"claude ({s.claude_model}, abonnement Pro)" if provider == "claude" else f"gemini ({s.gemini_model})"
        return Escalator(big, label, llm, repairer=repairer, logs_dir=s.logs_dir,
                         state_dir=app_state_dir or s.state_dir)

    def masker_factory() -> Masker:
        try:
            vault = Vault(s.state_dir / "coffre.bin", default_cipher(s.state_dir))
        except VaultError as exc:
            output_fn(f"Attention : coffre chiffré indisponible ({exc}) ; jetons en mémoire seulement.")
            vault = Vault(None)
        return Masker(vault, name_detector=spacy_name_detector(), indirect_llm=llm)

    return App(llm, factory, quota_info=lambda: (limiter.used_today, s.gemini.daily_hard_stop),
               state_dir=app_state_dir or s.state_dir, input_fn=input_fn, output_fn=output_fn,
               format_llm=format_llm, review_factory=review_factory, escalator_factory=escalator_factory,
               claude_check=claude.check_available, claude_calls=claude.calls_today,
               big_model=s.big_model_provider, complexity_threshold=s.complexity_threshold,
               profile=profile, profile_loader=load_profile, profile_names=list_profiles,
               masker_factory=masker_factory)

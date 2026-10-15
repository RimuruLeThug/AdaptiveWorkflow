"""Assemblage de l'application réelle (terminal et pilote partagent ce code).

Le compteur de quota Gemini est TOUJOURS celui de config.state_dir : un pilote
ou un autre point d'entrée ne peut pas contourner le budget journalier.

build_app() fait les entrées-sorties (configuration, clé, vérification d'Ollama,
profil) ; build_components() assemble les objets à partir de clients déjà
construits, sans réseau, pour être testé avec des doubles.

Agents interchangeables (agents.py) : sans AGENT_<ROLE>, AGENT_DECLARATIONS,
GEMINI_TEST_MODEL ni clé « agents » dans le profil, l'assemblage est exactement
celui d'avant. Sinon, seuls les rôles réaffectés (ou dont un agent est redéclaré)
passent par le routeur : RoleLLM pour les rôles locaux, RoleClient pour
organisateur, worker, relecteur et grand modèle. Le routeur réutilise le même
limiteur et le même GeminiClient (une seule file), le masqueur de l'application,
la confirmation o/n de l'utilisateur et un RunLogger pour journaliser chaque choix.

Langage libre (qwen_ui/langage_libre.py) : le routeur est TOUJOURS construit, pour le
rôle « reformulation » (agent Gemini par défaut, même GeminiClient et même file que le
reste de l'application). Sans configuration d'agents, il ne remplace aucun autre objet.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from agents import (AgentRouter, AgentSpec, LocalAgent, RoleClient, RoleLLM, build_registry, builtin_specs,
                    parse_declarations, resolve_routes)
from gemini_client import GeminiClient
from local_llm import LocalLLM, LocalLLMError, OllamaBackend
from orchestrator import Orchestrator, RunLogger
from organizers.gemini import GeminiOrganizer
from qwen_ui.app import YES, App
from qwen_ui.langage_libre import FeedbackJournal, LangageLibre, Reformulator
from rate_limiter import RateLimiter
from review import ReviewPanel
from verify.basic import basic_verifier
from verify.repair import repair_json
from claude_cli import ClaudeCodeClient
from escalation import Escalator
from organizers.claude import ClaudeOrganizer
from privacy.detectors import spacy_name_detector
from privacy.masker import Masker, declared_values
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


def agents_configured(s: config.Settings, profile: Optional[Profile]) -> bool:
    """Vrai si une configuration d'agents existe (variables AGENT_*, GEMINI_TEST_MODEL ou profil)."""
    return bool(s.agent_roles or s.agent_roles_difficile or s.agent_declarations or s.gemini_test_model
                or (profile is not None and profile.agents))


class _LazyMasker:
    """Le masqueur de l'application, construit seulement au premier envoi externe."""

    def __init__(self, factory: Callable[[], Masker]) -> None:
        self._factory = factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self._factory(), name)


@dataclass
class Components:
    """Arguments de App, plus le routeur d'agents (None : assemblage d'avant, inchangé)."""

    llm: LocalLLM
    orchestrator_factory: Callable[..., Orchestrator]
    app_kwargs: Dict[str, Any]
    router: Optional[AgentRouter] = None
    routed_roles: Tuple[str, ...] = ()
    _app: List[App] = field(default_factory=list)

    def bind(self, app: App) -> App:
        """Le routeur suit le profil courant de l'application (/profil compris)."""
        self._app[:] = [app]
        return app


def build_components(
    s: config.Settings,
    profile: Optional[Profile],
    *,
    llm: LocalLLM,
    format_llm: Optional[LocalLLM],
    limiter: RateLimiter,
    client: GeminiClient,
    claude: ClaudeCodeClient,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    app_state_dir: Optional[Path] = None,
    masker_factory: Optional[Callable[[], Masker]] = None,
    gemini_client_factory: Optional[Callable[[str, RateLimiter], GeminiClient]] = None,
    claude_client_factory: Optional[Callable[[str], ClaudeCodeClient]] = None,
    logger: Any = None,
) -> Components:
    """Assemblage pur (aucun appel réseau, aucun modèle lancé). Lève ValueError si la
    configuration des agents est invalide."""

    # Objets utilisés par rôle. Par défaut : ceux d'avant ; le routeur ne remplace que les rôles réaffectés.
    use: Dict[str, Any] = {"organisateur": client, "worker": client, "relecteur": client, "grand_modele": None,
                           "promesse": llm, "reparation": format_llm,
                           "request_counter": lambda: client.requests_sent}

    def repairer(text: str, schema: dict):
        return repair_json(text, schema, use["reparation"])

    def factory(organizer: str, prof: Optional[Profile] = None) -> Orchestrator:
        if organizer not in ("gemini", "claude"):
            raise ValueError(f"organisateur non disponible : {organizer}")
        specs = prof.workers if prof else DEFAULT_WORKERS
        return Orchestrator(
            ClaudeOrganizer(claude) if organizer == "claude" else GeminiOrganizer(use["organisateur"]),
            [GeminiWorker(spec, use["worker"]) for spec in specs],
            basic_verifier,
            delegation_budget=s.delegation_budget,
            max_depth=s.max_depth,
            max_plan_attempts=s.max_plan_attempts,
            logs_dir=s.logs_dir,
            request_counter=use["request_counter"],
            repairer=repairer,
        )

    def review_factory(prof: Optional[Profile] = None) -> ReviewPanel:
        over = prof.review if prof else {}
        return ReviewPanel(
            use["relecteur"],
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
        # Rôle grand_modele réaffecté : il s'applique au grand modèle par défaut ; un choix
        # explicite de l'utilisateur (/grand) prime et garde le client direct.
        if use["grand_modele"] is not None and provider == s.big_model_provider:
            big, label = use["grand_modele"]
        else:
            big = claude if provider == "claude" else client
            label = (f"claude ({s.claude_model}, abonnement Pro)" if provider == "claude"
                     else f"gemini ({s.gemini_model})")
        return Escalator(big, label, use["promesse"], repairer=repairer, logs_dir=s.logs_dir,
                         state_dir=app_state_dir or s.state_dir)

    masker_box: List[Masker] = []

    def default_masker_factory() -> Masker:
        try:
            vault = Vault(s.state_dir / "coffre.bin", default_cipher(s.state_dir))
        except VaultError as exc:
            output_fn(f"Attention : coffre chiffré indisponible ({exc}) ; jetons en mémoire seulement.")
            vault = Vault(None)
        return Masker(vault, name_detector=spacy_name_detector(), indirect_llm=llm)

    def shared_masker() -> Masker:
        """Un seul masqueur (une seule table de jetons) pour l'application et le routeur."""
        if not masker_box:
            masker_box.append((masker_factory or default_masker_factory)())
        return masker_box[0]

    app_kwargs: Dict[str, Any] = dict(
        quota_info=lambda: (limiter.used_today, s.gemini.daily_hard_stop),
        state_dir=app_state_dir or s.state_dir, format_llm=format_llm, review_factory=review_factory,
        escalator_factory=escalator_factory, claude_check=claude.check_available, claude_calls=claude.calls_today,
        big_model=s.big_model_provider, complexity_threshold=s.complexity_threshold,
        profile=profile, profile_loader=load_profile, profile_names=list_profiles,
        masker_factory=shared_masker)

    # ------------------------------------------------- agents interchangeables
    # Le routeur existe toujours (rôle « reformulation » du langage libre) ; sans configuration
    # d'agents, il ne remplace aucun des objets d'avant.
    configured = agents_configured(s, profile)
    gemini_clients: List[GeminiClient] = [client]

    def gemini_for(model: str, lim: RateLimiter) -> GeminiClient:
        if lim is not limiter:
            raise ValueError("un seul limiteur Gemini pour toute l'application")
        if model == client.model:
            return client  # le GeminiClient de l'application, pas une copie
        if gemini_client_factory is None:
            other = GeminiClient(config.gemini_api_key(), model, limiter, timeout_s=s.gemini_timeout_s)
        else:
            other = gemini_client_factory(model, limiter)
        gemini_clients.append(other)
        return other

    def claude_for(model: str) -> ClaudeCodeClient:
        if model == claude.model:
            return claude
        if claude_client_factory is not None:
            return claude_client_factory(model)
        return ClaudeCodeClient(model=model, timeout_s=s.claude_timeout_s, allow_api_billing=s.claude_allow_api_billing,
                                counter_path=s.state_dir / "claude_calls.json")

    registry = build_registry(s, profile=profile, gemini_limiter=limiter, gemini_client_factory=gemini_for,
                              claude_client_factory=claude_for)
    # Agents Qwen intégrés non redéclarés : les backends déjà vérifiés au démarrage.
    builtin = {sp.name: sp for sp in builtin_specs(s)}
    for name, backend in (("qwen_interface", llm), ("qwen_format", format_llm)):
        if backend is not None and registry.spec(name) == builtin[name]:
            registry.declare(builtin[name], lambda spec, b=backend: LocalAgent(spec, b))

    routes = resolve_routes(s, profile, registry.names())
    redeclared = {sp.name for sp in parse_declarations(s.agent_declarations, source="AGENT_DECLARATIONS")}
    redeclared |= {sp.name for sp in parse_declarations((profile.agents if profile else {}).get("declarations"),
                                                        source="profil")}
    routed = tuple(role for role, r in routes.items()
                   if r.source != "défaut" or any(a in redeclared for a in r.agents()))

    app_box: List[App] = []

    def current_profile() -> Optional[Profile]:
        return app_box[0].profile if app_box else profile

    def personal_data() -> bool:
        prof = current_profile()
        # Donnée personnelle détectée dans le texte libre : verrou de session, même sans profil restrictif.
        return bool(prof and prof.personal_data) or bool(app_box and app_box[0].personal_lock)

    def declared() -> List[str]:
        prof = current_profile()
        return declared_values(list(prof.records), prof.personal_fields) if prof else []

    def confirm(preview: str) -> bool:
        output_fn(preview)
        try:
            return input_fn("Envoyer ce texte à un agent externe ? (o/n) ").strip().lower() in YES
        except (EOFError, KeyboardInterrupt):
            return False

    if logger is None:
        logger = RunLogger(s.logs_dir, f"agents-{uuid.uuid4().hex[:8]}", log_content=False)
    router = AgentRouter(registry, routes, logger=logger, personal_data=personal_data,
                         masker=_LazyMasker(shared_masker), declared=declared, confirm=confirm)
    app_kwargs["langage_libre"] = LangageLibre(
        Reformulator(router, llm), FeedbackJournal((app_state_dir or s.state_dir) / "retours.jsonl"),
        n_exemples=s.feedback_exemples, profil_perso=s.profil_donnees_perso, producteur=routes["worker"].agent)
    if not configured:
        return Components(llm, factory, app_kwargs, None, (), app_box)  # objets d'avant, à l'identique

    logger.event("agent_routes", routed=list(routed), routes={
        role: {"agent": r.agent, "difficile": r.difficile, "repli": list(r.repli), "source": r.source}
        for role, r in routes.items()})
    if routed:
        output_fn("Agents configurés : " + ", ".join(
            f"{role} -> {routes[role].agent} ({routes[role].source})" for role in routed))

    for role in ("organisateur", "worker", "relecteur"):
        if role in routed:
            use[role] = RoleClient(router, role)
    if "grand_modele" in routed:
        r = routes["grand_modele"]
        use["grand_modele"] = (RoleClient(router, "grand_modele"), f"agent {r.agent} (rôle grand_modele, {r.source})")
    for role in ("promesse", "reparation"):
        if role in routed:
            use[role] = RoleLLM(router, role)
    use["request_counter"] = lambda: sum(c.requests_sent for c in gemini_clients)
    if "dedup" in routed:
        app_kwargs["format_llm"] = RoleLLM(router, "dedup")
    role_llms = {role: RoleLLM(router, role) for role in ("interface", "cadrage", "routage") if role in routed}
    if role_llms:
        app_kwargs["role_llms"] = role_llms
    return Components(llm, factory, app_kwargs, router, routed, app_box)


def make_app(c: Components, *, input_fn: Callable[[str], str] = input,
             output_fn: Callable[[str], None] = print) -> App:
    return c.bind(App(c.llm, c.orchestrator_factory, input_fn=input_fn, output_fn=output_fn, **c.app_kwargs))


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
    claude = ClaudeCodeClient(model=s.claude_model, timeout_s=s.claude_timeout_s,
                              allow_api_billing=s.claude_allow_api_billing,
                              counter_path=s.state_dir / "claude_calls.json")
    try:
        components = build_components(s, profile, llm=llm, format_llm=format_llm, limiter=limiter, client=client,
                                      claude=claude, input_fn=input_fn, output_fn=output_fn,
                                      app_state_dir=app_state_dir)
    except ValueError as exc:
        raise StartupError(f"Configuration des agents invalide : {exc}") from None
    return make_app(components, input_fn=input_fn, output_fn=output_fn)

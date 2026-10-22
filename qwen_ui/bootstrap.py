"""Assemblage de l'application réelle (terminal et pilote partagent ce code).

Le compteur de quota Gemini est TOUJOURS celui de config.state_dir : un pilote
ou un autre point d'entrée ne peut pas contourner le budget journalier.

build_app() fait les entrées-sorties (configuration, clé, vérification d'Ollama et de
Claude Code, profil) ; build_components() assemble les objets à partir de clients déjà
construits, sans réseau, pour être testé avec des doubles.

V3 : TOUS les rôles passent par le routeur d'agents (agents.py). Par défaut, chaque rôle
est confié à claude_haiku (Claude Code sur l'abonnement de l'utilisateur), avec le Qwen
local (un seul modèle) en repli ; AGENT_<ROLE>, AGENT_DECLARATIONS ou la clé « agents » du
profil les réaffectent. Le routeur réutilise le même limiteur et le même GeminiClient (une
seule file), le masqueur de l'application, la confirmation o/n de l'utilisateur et un
RunLogger pour journaliser chaque choix. /organisateur et /grand peuvent encore forcer un
client direct (gemini ou claude). Ni la clé Gemini ni Ollama ne sont obligatoires au
démarrage : un agent indisponible est sauté au profit de son repli.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from agents import (QWEN_ALIASES, ROLES, AgentRouter, ClaudeAgent, LocalAgent, RoleClient, RoleLLM, build_registry,
                    builtin_specs, resolve_routes)
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
from claude_cli import COST_NOTE, ClaudeCodeClient, ClaudeCodeError
from escalation import Escalator
from organizers.claude import ClaudeOrganizer
from privacy.detectors import spacy_name_detector
from privacy.masker import Masker, declared_values
from privacy.vault import Vault, VaultError, default_cipher
from profiles.loader import Profile, ProfileError, list_profiles, load_profile
from workers.defaults import DEFAULT_WORKERS
from workers.gemini import GeminiWorker

class StartupError(Exception):
    """Le programme refuse de démarrer (profil ou configuration des agents invalide...)."""


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
    """Arguments de App, plus le routeur d'agents."""

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


PROVIDER_LABELS = {"claude": "Claude via Claude Code, abonnement", "gemini": "Gemini, quota commun",
                   "ollama": "local", "local_openai": "local"}


def build_components(
    s: config.Settings,
    profile: Optional[Profile],
    *,
    llm: LocalLLM,
    format_llm: Optional[LocalLLM],
    limiter: RateLimiter,
    client: Optional[GeminiClient],
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
    configuration des agents est invalide.

    llm : le Qwen local (agent « qwen » et ses alias) ; format_llm : backend de l'alias
    « qwen_format » (le même modèle en V3 ; None = llm). client : GeminiClient de
    l'application, ou None si la clé est absente (les agents Gemini sont alors indisponibles).
    """

    def direct_gemini() -> GeminiClient:
        if client is None:
            config.gemini_api_key()  # lève MissingKeyError avec le message habituel
        return client

    gemini_clients: List[GeminiClient] = [client] if client is not None else []

    def gemini_for(model: str, lim: RateLimiter) -> GeminiClient:
        if lim is not limiter:
            raise ValueError("un seul limiteur Gemini pour toute l'application")
        if client is not None and model == client.model:
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
    # Agents Qwen intégrés non redéclarés : le backend local de l'application (un seul modèle).
    builtin = {sp.name: sp for sp in builtin_specs(s)}
    for name in ("qwen", *QWEN_ALIASES):
        backend = (format_llm or llm) if name == "qwen_format" else llm
        if registry.spec(name) == builtin[name]:
            registry.declare(builtin[name], lambda spec, b=backend: LocalAgent(spec, b))

    routes = resolve_routes(s, profile, registry.names())
    routed = tuple(routes)  # V3 : tous les rôles passent par le routeur

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

    def role_label(role: str) -> str:
        """Agent qui traitera le rôle maintenant (profil « données personnelles » : l'agent local)."""
        r = routes[role]
        name = r.agent
        if personal_data() and registry.spec(name).external:
            local = [a for a in r.agents() if not registry.spec(a).external]
            name = local[0] if local else ROLES[role].local_agent
        sp = registry.spec(name)
        return f"{name} ({PROVIDER_LABELS.get(sp.provider, sp.provider)})"

    def role_uses_gemini(role: str) -> bool:
        if personal_data():
            return False
        return any(registry.spec(a).provider == "gemini" for a in routes[role].agents())

    # Objets utilisés par rôle : tous routés.
    grand = routes["grand_modele"]
    use: Dict[str, Any] = {
        "organisateur": RoleClient(router, "organisateur"), "worker": RoleClient(router, "worker"),
        "relecteur": RoleClient(router, "relecteur"),
        "grand_modele": (RoleClient(router, "grand_modele"), f"agent {grand.agent} (rôle grand_modele, {grand.source})"),
        "promesse": RoleLLM(router, "promesse"), "reparation": RoleLLM(router, "reparation"),
        "request_counter": lambda: sum(c.requests_sent for c in gemini_clients),
    }

    def repairer(text: str, schema: dict):
        return repair_json(text, schema, use["reparation"])

    def factory(organizer: str, prof: Optional[Profile] = None) -> Orchestrator:
        if organizer not in ("agents", "gemini", "claude"):
            raise ValueError(f"organisateur non disponible : {organizer}")
        specs = prof.workers if prof else DEFAULT_WORKERS
        if organizer == "claude":
            org = ClaudeOrganizer(claude)
        elif organizer == "gemini":
            org = GeminiOrganizer(direct_gemini())  # choix explicite /organisateur gemini
        else:
            org = GeminiOrganizer(use["organisateur"])  # organisateur générique, client = rôle
        return Orchestrator(
            org,
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
        # « agents » : le rôle grand_modele (BIG_MODEL_PROVIDER y est déjà appliqué) ; un choix
        # explicite de l'utilisateur (/grand gemini|claude) garde le client direct.
        if provider == "agents":
            big, label = use["grand_modele"]
        elif provider == "claude":
            big, label = claude, f"claude ({s.claude_model}, abonnement Pro)"
        else:
            big, label = direct_gemini(), f"gemini ({s.gemini_model})"
        return Escalator(big, label, use["promesse"], repairer=repairer, logs_dir=s.logs_dir,
                         state_dir=app_state_dir or s.state_dir)

    app_kwargs: Dict[str, Any] = dict(
        quota_info=lambda: (limiter.used_today, s.gemini.daily_hard_stop),
        state_dir=app_state_dir or s.state_dir, format_llm=RoleLLM(router, "dedup"), review_factory=review_factory,
        escalator_factory=escalator_factory, claude_check=claude.check_available, claude_calls=claude.calls_today,
        big_model="agents", complexity_threshold=s.complexity_threshold,
        profile=profile, profile_loader=load_profile, profile_names=list_profiles,
        masker_factory=shared_masker, organizer="agents", workers_provider="agents", reviewers_provider="agents",
        role_label=role_label, role_uses_gemini=role_uses_gemini,
        role_llms={role: RoleLLM(router, role) for role in ("interface", "cadrage", "routage")},
        conversation=s.mode_conversation)
    app_kwargs["langage_libre"] = LangageLibre(
        Reformulator(router, llm), FeedbackJournal((app_state_dir or s.state_dir) / "retours.jsonl"),
        n_exemples=s.feedback_exemples, profil_perso=s.profil_donnees_perso, producteur=routes["worker"].agent)

    logger.event("agent_routes", routed=list(routed), routes={
        role: {"agent": r.agent, "difficile": r.difficile, "repli": list(r.repli), "source": r.source}
        for role, r in routes.items()})
    changed = [role for role, r in routes.items() if r.source != "défaut"]
    if changed:
        output_fn("Agents configurés : " + ", ".join(
            f"{role} -> {routes[role].agent} ({routes[role].source})" for role in changed))
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
    Le compteur de quota reste dans config.state_dir quoi qu'il arrive.

    V3 : ni la clé Gemini ni Ollama ne sont obligatoires ; seuls des avertissements sont
    affichés quand un agent par défaut ou son repli est indisponible."""
    config.load_dotenv()
    s = config.load_settings()
    legacy = config.legacy_qwen_variables()
    if legacy:
        output_fn(f"Attention : {', '.join(legacy)} ignorée(s) depuis la V3 (un seul modèle local) : "
                  f"utilisez QWEN_MODEL (actuel : {s.qwen_model}) et QWEN_NUM_CTX.")
    try:
        key: Optional[str] = config.gemini_api_key()
    except config.MissingKeyError:
        key = None

    llm = OllamaBackend(s.qwen_model, host=s.ollama_host, num_ctx=s.qwen_num_ctx, think=False,
                        keep_alive=s.ollama_keep_alive)
    try:
        llm.check_available()
    except LocalLLMError as exc:
        output_fn(f"Attention : Qwen local indisponible ({exc}). Les replis locaux, le profil « données "
                  "personnelles » et le repérage des indices indirects ne fonctionneront pas.")

    try:
        profile: Optional[Profile] = load_profile(s.profile)
    except ProfileError as exc:
        raise StartupError(f"Profil de démarrage invalide : {exc}") from None

    limiter = RateLimiter(s.gemini, s.state_dir / "gemini_quota.json")
    client = GeminiClient(key, s.gemini_model, limiter, timeout_s=s.gemini_timeout_s) if key else None
    claude = ClaudeCodeClient(model=s.claude_model, timeout_s=s.claude_timeout_s,
                              allow_api_billing=s.claude_allow_api_billing,
                              counter_path=s.state_dir / "claude_calls.json")
    try:
        components = build_components(s, profile, llm=llm, format_llm=None, limiter=limiter, client=client,
                                      claude=claude, input_fn=input_fn, output_fn=output_fn,
                                      app_state_dir=app_state_dir)
    except ValueError as exc:
        raise StartupError(f"Configuration des agents invalide : {exc}") from None
    router = components.router
    providers = {router.registry.spec(a).provider for r in router.routes.values() for a in r.agents()}
    if "claude" in providers:
        try:
            method = claude.check_available()
            output_fn(f"Claude Code connecté ({method}) : les rôles confiés à Claude {COST_NOTE}")
        except ClaudeCodeError as exc:
            output_fn(f"Attention : Claude indisponible ({exc}). Les rôles confiés à Claude passeront "
                      "directement à leur repli (Qwen local par défaut).")
            for name in router.registry.names():
                if router.registry.spec(name).provider == "claude":
                    agent = router.registry.get(name)
                    if isinstance(agent, ClaudeAgent):
                        agent.mark_unavailable(str(exc))
    if "gemini" in providers and key is None:
        output_fn("Attention : GEMINI_API_KEY absente ; les agents Gemini sont indisponibles (repli utilisé).")
    return make_app(components, input_fn=input_fn, output_fn=output_fn)

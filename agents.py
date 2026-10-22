"""Agents IA interchangeables (V2, défauts V3).

Le code décide, les modèles proposent : ce module ne fait que choisir QUEL modèle
traite une requête, selon une configuration, puis normaliser sa réponse.

- Contrat d'agent (Agent) : un nom, un fournisseur, un modèle ; call() retourne un
  AgentResult normalisé (texte, JSON validé ou réparé, durée, tentatives).
- Adaptateurs minces au-dessus des clients existants, jamais réécrits :
  LocalAgent (OllamaBackend ou tout LocalLLM), GeminiAgent (GeminiClient, donc la
  file unique du limiteur), ClaudeAgent (ClaudeCodeClient, abonnement Pro).
- Backend local alternatif (LocalOpenAIBackend) : serveur compatible OpenAI sur la
  machine, construit sur l'ABC LocalLLM ; testé uniquement avec des doubles.
- Registre d'agents nommés (AgentRegistry) : claude_haiku, qwen (modèle local unique ;
  qwen_interface et qwen_format en sont des alias), gemini, gemini_test, claude, plus les
  agents déclarés par configuration ou profil.
- Rôles (ROLES) et affectation rôle -> agents (RoleRoute) : défauts du projet (V3 : tous les
  rôles sur claude_haiku, repli sur le Qwen local), puis variables d'environnement
  AGENT_<ROLE>, puis profil (clé « agents »).
- Routage (AgentRouter) : critère déterministe simple/difficile, replis sur
  indisponibilité ou échec (timeout, 429, vide, hors schéma), journal de chaque
  choix (RunLogger du bus ou équivalent).
- RoleLLM / RoleClient : présentent un rôle sous la forme attendue par le code
  existant (LocalLLM pour route(), cadrer(), check_promise(), repair_json(),
  deduplicate() ; generate() pour organisateurs, workers, relecteurs, escalade).
  Le squelette n'est pas modifié : on remplace un agent par configuration.

Confidentialité (privacy/, jamais assouplie) : un rôle confié à Gemini ou Claude est
un envoi externe. Profil « données personnelles » : tout agent externe est écarté avant
le moindre envoi, y compris en repli, et le rôle est traité par son agent local (Qwen,
repli automatique V3) ; rien ne quitte la machine. Sinon : masquage et contrôle de fuite
avant l'envoi, et confirmation de l'utilisateur (aperçu masqué, destinataire nommé,
redemandée si le repli change de destinataire) pour tout rôle dont le contenu peut dériver
du texte libre (routage, cadrage, réparation...). Ces rôles (accord_session) acceptent, à la
place, un accord unique de session pour UN fournisseur (grant/revoke_session_consent), donné
explicitement par l'utilisateur et caduc en profil « données personnelles » ; masquage et
contrôle de fuite restent faits avant chaque envoi. Les valeurs réelles sont réinjectées en
local par le code.

Quota journalier Gemini épuisé : aucun autre agent Gemini n'est tenté et, faute de repli
non Gemini abouti, RoleDailyQuotaError (sous-classe de GeminiDailyQuotaError) arrête le
bus, la revue et l'application comme sur le chemin par défaut.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import config
from claude_cli import ClaudeCodeClient, ClaudeCodeError
from gemini_client import GeminiClient, GeminiDailyQuotaError, GeminiError
from local_llm import LocalLLM, LocalLLMError, LocalReply, Message, OllamaBackend
from local_llm import urllib_transport as local_urllib_transport
from privacy.detectors import detect
from privacy.masker import LeakBlocked, Masker, PreviewRefused, render_preview
from privacy.policy import PROVIDERS as POLICY_PROVIDERS
from privacy.policy import PolicyError, check_provider
from rate_limiter import DailyQuotaExhausted, RateLimiter, RetriesExhausted
from verify.repair import repair_json
from verify.schema import SchemaError, parse_json

# --------------------------------------------------------------------- rôles


@dataclass(frozen=True)
class RoleInfo:
    name: str
    description: str
    default_agent: str
    # Le contenu envoyé peut dériver du texte libre tapé par l'utilisateur (directement,
    # ou indirectement : sortie de Qwen reprenant ce texte, JSON à réparer, idées...).
    # Tout envoi externe d'un tel rôle exige la confirmation avec aperçu masqué.
    free_text: bool = False
    # L'utilisateur peut donner un accord UNIQUE pour la session (un fournisseur, ce rôle seul)
    # à la place de la confirmation par envoi. Masquage et contrôle de fuite restent faits
    # avant CHAQUE envoi ; jamais pour un profil « données personnelles ».
    accord_session: bool = False
    # Agent local du rôle : repli par défaut, et seul agent utilisé en profil « données personnelles ».
    local_agent: str = "qwen"


DEFAULT_AGENT = "claude_haiku"  # V3 : agent par défaut de tous les rôles
LOCAL_AGENT = "qwen"  # V3 : modèle local unique (Ollama)

ROLES: Dict[str, RoleInfo] = {r.name: r for r in (
    RoleInfo("organisateur", "décompose la tâche et délègue aux workers", DEFAULT_AGENT),
    RoleInfo("worker", "exécute une consigne isolée", DEFAULT_AGENT),
    RoleInfo("relecteur", "note les idées (revue)", DEFAULT_AGENT),
    RoleInfo("grand_modele", "étudie une idée validée (escalade)", DEFAULT_AGENT),
    RoleInfo("interface", "interface, résumés", DEFAULT_AGENT, free_text=True, accord_session=True),
    RoleInfo("cadrage", "cadrage de la fiche projet", DEFAULT_AGENT, free_text=True, accord_session=True),
    RoleInfo("routage", "propose une commande à partir du texte libre", DEFAULT_AGENT, free_text=True,
             accord_session=True),
    RoleInfo("promesse", "contrôle de promesse, réponse fermée oui/non", DEFAULT_AGENT, free_text=True,
             accord_session=True),
    RoleInfo("format", "mise en forme JSON", DEFAULT_AGENT, free_text=True, accord_session=True),
    RoleInfo("dedup", "dédoublonnage des idées", DEFAULT_AGENT, free_text=True, accord_session=True),
    # Répare aussi le JSON invalide d'un rôle en texte libre (routage, cadrage...) : même règle.
    RoleInfo("reparation", "réparation d'un JSON invalide", DEFAULT_AGENT, free_text=True, accord_session=True),
    # Reçoit directement le texte libre : envoi externe seulement après l'accord de session.
    RoleInfo("reformulation", "reformule le texte libre en demande claire", DEFAULT_AGENT,
             free_text=True, accord_session=True),
)}

# Rôles qui étaient locaux (Qwen) en V2 : ils voient le texte libre ; leur envoi externe exige
# la confirmation ou l'accord de session.
TEXTE_LIBRE_ROLES = ("interface", "cadrage", "routage", "promesse", "format", "dedup", "reparation",
                     "reformulation")

if tuple(ROLES) != tuple(config.AGENT_ROLES):  # garde-fou : une seule liste de rôles
    raise RuntimeError("agents.ROLES et config.AGENT_ROLES divergent")
# Garde-fou : un rôle qui peut voir le texte libre ne doit jamais l'envoyer dehors sans confirmation.
if any(not ROLES[r].free_text for r in TEXTE_LIBRE_ROLES):
    raise RuntimeError("un rôle en texte libre doit exiger la confirmation de tout envoi externe (free_text=True)")
# Garde-fou : l'accord de session ne remplace la confirmation que pour un rôle en texte libre.
if any(r.accord_session and not r.free_text for r in ROLES.values()):
    raise RuntimeError("accord_session réservé aux rôles en texte libre")
# Garde-fou : l'agent local d'un rôle est un agent local intégré (jamais externe).
if any(r.local_agent not in ("qwen", "qwen_interface", "qwen_format") for r in ROLES.values()):
    raise RuntimeError("l'agent local d'un rôle doit être le Qwen local")

# Fournisseur technique -> fournisseur au sens de la politique de données (privacy/policy.py).
PROVIDER_POLICY = {"ollama": "local", "local_openai": "local", "gemini": "gemini", "claude": "claude"}

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


# -------------------------------------------------------------------- erreurs


class AgentError(Exception):
    """Échec d'un agent ; kind sert au journal et à la décision de repli."""

    kind = "erreur"


class AgentUnavailable(AgentError):
    kind = "indisponible"


class AgentTimeout(AgentError):
    kind = "timeout"


class AgentRateLimited(AgentError):
    kind = "http_429"


class AgentDailyQuota(AgentRateLimited):
    """Quota JOURNALIER Gemini épuisé (arrêt dur du limiteur ou 429 PerDay de Google).

    Commun à tous les agents Gemini (une seule file, un seul quota) : aucun autre agent
    Gemini n'est tenté, et l'arrêt est propagé (RoleDailyQuotaError) si aucun repli
    non Gemini n'aboutit.
    """

    kind = "quota_journalier"

    def __init__(self, message: str, original: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.original = original


class AgentEmptyResponse(AgentError):
    kind = "vide"


class AgentSchemaError(AgentError):
    kind = "hors_schema"


class NoAgentSucceeded(AgentError):
    """Aucun agent du rôle n'a donné de réponse exploitable (replis compris)."""

    kind = "aucun_agent"

    def __init__(self, role: str, failures: List[Tuple[str, str]]):
        detail = "; ".join(f"{a} : {k}" for a, k in failures) or "aucun agent configuré"
        super().__init__(f"rôle « {role} » : aucun agent n'a abouti ({detail})")
        self.role = role
        self.failures = failures


class ExternalSendBlocked(LocalLLMError):
    """Envoi externe bloqué (fuite détectée ou aperçu refusé) : rien n'est parti."""


class RoleCallError(GeminiError):
    """Échec d'un rôle appelé via RoleClient.

    Sous-classe de GeminiError pour que le bus, la revue et l'escalade existants
    le traitent comme un échec de délégation ordinaire, sans modification.
    """


class RoleDailyQuotaError(RoleCallError, GeminiDailyQuotaError):
    """Quota journalier Gemini épuisé pendant un rôle : même type d'arrêt que le chemin
    par défaut (GeminiDailyQuotaError), donc le bus, la revue et App.handle s'arrêtent
    et affichent le message dédié."""


# ---------------------------------------------------------------- spécification


@dataclass(frozen=True)
class AgentSpec:
    name: str
    provider: str  # ollama | local_openai | gemini | claude
    model: str  # vide : agent indisponible (jamais de nom de modèle inventé)
    options: Tuple[Tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not _AGENT_NAME_RE.match(self.name):
            raise ValueError(f"nom d'agent invalide : {self.name!r} (attendu : [a-z][a-z0-9_]*, 40 max)")
        if self.provider not in PROVIDER_POLICY:
            raise ValueError(f"agent {self.name} : fournisseur inconnu {self.provider!r} "
                             f"(connus : {', '.join(PROVIDER_POLICY)})")

    def option(self, key: str, default: Any = None) -> Any:
        return dict(self.options).get(key, default)

    @property
    def policy_provider(self) -> str:
        return PROVIDER_POLICY[self.provider]

    @property
    def external(self) -> bool:
        return POLICY_PROVIDERS[self.policy_provider].external


@dataclass
class AgentResult:
    """Réponse normalisée, identique quel que soit le fournisseur."""

    agent: str
    provider: str
    model: str
    text: str  # texte brut, ou JSON validé (éventuellement réparé) re-sérialisé
    data: Any = None  # objet JSON validé si un schéma était demandé
    repair: Optional[str] = None  # "deterministe" | "qwen" si la sortie a été réparée
    latency_s: float = 0.0
    attempts: int = 1
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    truncated: bool = False
    role: str = ""
    reason: str = ""  # raison du routage


@dataclass(frozen=True)
class RawReply:
    text: str
    attempts: int = 1
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    truncated: bool = False


SCHEMA_HINT = "\n\nRéponds uniquement avec un document JSON conforme à ce schéma, sans texte autour :\n{schema}"


def with_schema_hint(system: Optional[str], schema: Optional[dict]) -> Optional[str]:
    """Fournisseurs sans décodage contraint : le schéma est rappelé dans la consigne."""
    if schema is None:
        return system
    return (system or "") + SCHEMA_HINT.format(schema=json.dumps(schema, ensure_ascii=False))


# ------------------------------------------------------------------- contrat


class Agent(ABC):
    """Contrat commun. Les sous-classes ne font que traduire l'appel et les erreurs."""

    def __init__(self, spec: AgentSpec, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.spec = spec
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.spec.name!r}, model={self.spec.model!r})"

    def unavailable_reason(self) -> str:
        """Chaîne vide si l'agent est utilisable, sinon la raison."""
        return "" if self.spec.model else "modèle non renseigné"

    @abstractmethod
    def _generate(self, prompt: str, *, system: Optional[str], temperature: float,
                  schema: Optional[dict], json_output: bool) -> RawReply:
        """Un appel au fournisseur. Lève une AgentError (jamais l'erreur du client)."""

    def call(self, prompt: str, *, system: Optional[str] = None, schema: Optional[dict] = None,
             temperature: float = 0.2, json_output: bool = False,
             repair_llm: Optional[LocalLLM] = None) -> AgentResult:
        """Appel normalisé : réponse vide refusée, JSON validé par le code, réparé si possible."""
        reason = self.unavailable_reason()
        if reason:
            raise AgentUnavailable(f"agent {self.spec.name} indisponible : {reason}")
        t0 = self._monotonic()
        raw = self._generate(prompt, system=system, temperature=temperature, schema=schema,
                             json_output=json_output or schema is not None)
        text = (raw.text or "").strip()
        if not text:
            raise AgentEmptyResponse(f"agent {self.spec.name} : réponse vide")
        data, repair = None, None
        if schema is not None:
            res = repair_json(text, schema, repair_llm)
            if not res.ok:
                raise AgentSchemaError(f"agent {self.spec.name} : réponse hors schéma "
                                       f"({'; '.join([res.original_error] + res.reasons)})")
            if res.method != "aucune":
                text, repair = res.text, res.method
            data = parse_json(text, schema)
        return AgentResult(self.spec.name, self.spec.provider, self.spec.model, text, data, repair,
                           latency_s=self._monotonic() - t0, attempts=raw.attempts,
                           input_tokens=raw.input_tokens, output_tokens=raw.output_tokens,
                           truncated=raw.truncated)


# ---------------------------------------------------------------- adaptateurs


def _classify(message: str, default: type = AgentError) -> type:
    m = message.lower()
    if "timeout" in m or "timed out" in m or "n'a pas répondu en" in m:
        return AgentTimeout
    if "429" in m or "quota" in m or "limite d'usage" in m:
        return AgentRateLimited
    if "vide" in m or "sans texte" in m:
        return AgentEmptyResponse
    return default


class LocalAgent(Agent):
    """Modèle local : OllamaBackend ou tout autre LocalLLM (rien ne quitte la machine)."""

    def __init__(self, spec: AgentSpec, llm: LocalLLM, **kw) -> None:
        if spec.external:
            raise ValueError(f"agent {spec.name} : un LocalAgent ne peut pas être externe")
        super().__init__(spec, **kw)
        self.llm = llm

    def _generate(self, prompt, *, system, temperature, schema, json_output) -> RawReply:
        msgs = ([Message("system", system)] if system else []) + [Message("user", prompt)]
        try:
            reply = self.llm.chat(msgs, temperature=temperature, json_schema=schema)
        except LocalLLMError as exc:
            msg = str(exc)
            kind = _classify(msg, AgentUnavailable if ("absent" in msg or "ne répond pas" in msg) else AgentError)
            raise kind(f"agent {self.spec.name} : {msg}") from None
        except (TimeoutError, OSError) as exc:
            raise AgentTimeout(f"agent {self.spec.name} : {type(exc).__name__}: {exc}") from None
        return RawReply(reply.text, 1, reply.input_tokens, reply.output_tokens)


GeminiClientFactory = Callable[[str, RateLimiter], GeminiClient]


class GeminiAgent(Agent):
    """Gemini via GeminiClient : chaque tentative passe par la file unique du limiteur."""

    def __init__(self, spec: AgentSpec, client_factory: GeminiClientFactory, limiter: RateLimiter, **kw) -> None:
        if spec.provider != "gemini":
            raise ValueError(f"agent {spec.name} : fournisseur {spec.provider}, gemini attendu")
        if limiter is None:
            raise ValueError("un agent Gemini exige la file unique du limiteur")
        super().__init__(spec, **kw)
        self.limiter = limiter
        self._factory = client_factory
        self._client: Optional[GeminiClient] = None

    def unavailable_reason(self) -> str:
        if not self.spec.model:
            var = self.spec.option("variable")
            return f"modèle non renseigné ({var} vide)" if var else "modèle non renseigné"
        try:
            self.client  # construction locale, sans réseau : clé absente = indisponible avant tout aperçu
        except AgentUnavailable as exc:
            return str(exc)
        return ""

    @property
    def client(self) -> GeminiClient:
        if self._client is None:
            try:
                client = self._factory(self.spec.model, self.limiter)
            except config.MissingKeyError as exc:
                raise AgentUnavailable(f"agent {self.spec.name} : {exc}") from None
            if client.limiter is not self.limiter:
                raise ValueError(f"agent {self.spec.name} : client Gemini hors de la file unique du limiteur")
            if client.model != self.spec.model:
                raise ValueError(f"agent {self.spec.name} : modèle du client {client.model!r} ≠ {self.spec.model!r}")
            self._client = client
        return self._client

    def _generate(self, prompt, *, system, temperature, schema, json_output) -> RawReply:
        client = self.client
        try:
            r = client.generate(prompt, system=with_schema_hint(system, schema), temperature=temperature,
                                json_output=json_output)
        except (GeminiDailyQuotaError, DailyQuotaExhausted) as exc:
            raise AgentDailyQuota(f"agent {self.spec.name} : {exc}", exc) from exc
        except RetriesExhausted as exc:
            raise AgentRateLimited(f"agent {self.spec.name} : {exc}") from None
        except GeminiError as exc:
            raise _classify(str(exc))(f"agent {self.spec.name} : {exc}") from None
        return RawReply(r.text, r.attempts, r.input_tokens, r.output_tokens, r.truncated)


class ClaudeAgent(Agent):
    """Claude via Claude Code (abonnement Pro) ; hors limiteur Gemini, compté à part."""

    def __init__(self, spec: AgentSpec, client: ClaudeCodeClient, **kw) -> None:
        if spec.provider != "claude":
            raise ValueError(f"agent {spec.name} : fournisseur {spec.provider}, claude attendu")
        super().__init__(spec, **kw)
        self.client = client
        # Claude Code absent, non connecté ou connecté par clé API : inutile de relancer un
        # processus à chaque appel ; l'agent reste indisponible pour la session (replis directs).
        self._missing = ""
        self._probed = False

    def unavailable_reason(self) -> str:
        reason = super().unavailable_reason() or self._missing
        if not reason and not self._probed:
            # Sonde gratuite (aucun processus lancé) : commande « claude » introuvable = indisponible,
            # AVANT tout masquage ou demande de confirmation d'un envoi qui ne pourrait pas partir.
            self._probed = True
            executable = getattr(self.client, "executable", None)
            if callable(executable):
                try:
                    executable()
                except ClaudeCodeError as exc:
                    self._missing = reason = str(exc)
        return reason

    def mark_unavailable(self, reason: str) -> None:
        """Indisponible pour la session (ex. vérification de connexion échouée au démarrage)."""
        self._missing = reason or "indisponible"

    def _generate(self, prompt, *, system, temperature, schema, json_output) -> RawReply:
        try:
            r = self.client.generate(prompt, system=with_schema_hint(system, schema), temperature=temperature,
                                     json_output=json_output)
        except ClaudeCodeError as exc:
            msg = str(exc)
            unavailable = any(s in msg for s in ("pas installé", "pas connecté", "facturerait", "pas pu démarrer"))
            if unavailable:
                self._missing = msg
            raise _classify(msg, AgentUnavailable if unavailable else AgentError)(
                f"agent {self.spec.name} : {msg}") from None
        return RawReply(r.text, r.attempts, r.input_tokens, r.output_tokens, r.truncated)


# ------------------------------------------------------- backend local alternatif

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def require_loopback(host: str) -> str:
    """Un backend « local » doit l'être vraiment : seul l'hôte de la machine est accepté."""
    hostname = urllib.parse.urlsplit(host).hostname or ""
    if hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"hôte non local refusé ({hostname or host!r}) : un backend local doit tourner sur "
                         "cette machine (localhost, 127.0.0.1, ::1), sinon les données la quitteraient")
    return host.rstrip("/")


class LocalOpenAIBackend(LocalLLM):
    """Backend local alternatif : serveur compatible OpenAI sur la machine
    (llama.cpp server, LM Studio, vLLM...). Bibliothèque standard, transport injectable.

    Même interface que OllamaBackend (ABC LocalLLM) : il peut remplacer Qwen dans
    n'importe quel rôle local par simple déclaration (fournisseur « local_openai »).
    """

    def __init__(self, model: str, *, host: str = "http://127.0.0.1:8080", timeout_s: float = 300.0,
                 transport=local_urllib_transport, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not model:
            raise ValueError("nom de modèle local vide")
        self.model = model
        self.host = require_loopback(host)
        self.timeout_s = timeout_s
        self._transport = transport
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return f"LocalOpenAIBackend(model={self.model!r}, host={self.host!r})"

    def _request(self, method: str, path: str, payload: Optional[dict], timeout: float) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, raw = self._transport(method, self.host + path, body, timeout)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if status != 200:
            err = data.get("error")
            err = err.get("message") if isinstance(err, dict) else err
            raise LocalLLMError(f"backend local HTTP {status} : {err or raw[:300].decode('utf-8', 'replace')}")
        return data

    def chat(self, messages, *, temperature=0.2, json_schema=None) -> LocalReply:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        if json_schema is not None:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "sortie", "schema": json_schema}}
        t0 = self._monotonic()
        data = self._request("POST", "/v1/chat/completions", payload, self.timeout_s)
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
        text = _THINK_RE.sub("", str(msg.get("content") or "")).strip()
        usage = data.get("usage") or {}
        return LocalReply(text=text, model=self.model, latency_s=self._monotonic() - t0,
                          input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"))

    def list_models(self) -> List[str]:
        data = self._request("GET", "/v1/models", None, 10.0)
        return [str(m.get("id", "")) for m in data.get("data") or [] if isinstance(m, dict)]


# ------------------------------------------------------------------- registre

AgentFactory = Callable[[AgentSpec], Agent]


class AgentRegistry:
    """Agents nommés, construits à la demande. Déclarer un nom existant le remplace."""

    def __init__(self) -> None:
        self._specs: Dict[str, AgentSpec] = {}
        self._factories: Dict[str, AgentFactory] = {}
        self._agents: Dict[str, Agent] = {}
        self.gemini_limiter: Optional[RateLimiter] = None  # file unique, fixée au premier agent Gemini

    def declare(self, spec: AgentSpec, factory: AgentFactory) -> None:
        self._specs[spec.name] = spec
        self._factories[spec.name] = factory
        self._agents.pop(spec.name, None)

    def register(self, agent: Agent) -> None:
        """Agent déjà construit (doubles de test, intégrations)."""
        self.declare(agent.spec, lambda _spec, a=agent: a)

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> List[str]:
        return list(self._specs)

    def spec(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise AgentUnavailable(f"agent inconnu : {name} (agents : {', '.join(self._specs)})") from None

    def get(self, name: str) -> Agent:
        if name not in self._agents:
            agent = self._factories[self.spec(name).name](self._specs[name])
            if isinstance(agent, GeminiAgent):
                # Tout appel Gemini, quel que soit l'agent, passe par la même file.
                if self.gemini_limiter is None:
                    self.gemini_limiter = agent.limiter
                elif agent.limiter is not self.gemini_limiter:
                    raise ValueError(f"agent {name} : deux files Gemini distinctes sont interdites "
                                     "(un seul limiteur pour tous les appels Gemini)")
            self._agents[name] = agent
        return self._agents[name]


BUILTIN_EXTERNAL = {"gemini", "gemini_test", "claude", "claude_haiku"}
DECLARATION_KEYS = {"fournisseur", "modele", "num_ctx", "hote", "timeout_s"}


# V3 : un seul modèle Qwen local. « qwen_interface » et « qwen_format » (noms de la V2) sont
# conservés comme alias du même modèle, avec les mêmes options : Ollama ne le charge qu'une fois.
QWEN_ALIASES = ("qwen_interface", "qwen_format")


def builtin_specs(s: config.Settings) -> List[AgentSpec]:
    qwen_opts = (("num_ctx", s.qwen_num_ctx), ("think", False))
    return [
        AgentSpec("claude_haiku", "claude", s.claude_haiku_model, (("variable", "CLAUDE_HAIKU_MODEL"),)),
        AgentSpec("qwen", "ollama", s.qwen_model, qwen_opts),
        *(AgentSpec(alias, "ollama", s.qwen_model, qwen_opts) for alias in QWEN_ALIASES),
        AgentSpec("gemini", "gemini", s.gemini_model, (("variable", "GEMINI_MODEL"),)),
        AgentSpec("gemini_test", "gemini", s.gemini_test_model, (("variable", "GEMINI_TEST_MODEL"),)),
        AgentSpec("claude", "claude", s.claude_model, (("variable", "CLAUDE_MODEL"),)),
    ]


BUILTIN_NAMES = ("claude_haiku", "qwen", "qwen_interface", "qwen_format", "gemini", "gemini_test", "claude")


def parse_declarations(raw: Any, *, source: str) -> List[AgentSpec]:
    """Déclarations d'agents : {"nom": {"fournisseur": ..., "modele": ..., ...}}. Lève ValueError."""
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source} : JSON invalide ({exc})") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{source} : objet attendu {{nom: déclaration}}")
    specs = []
    for name, d in raw.items():
        if name == "gemini_test":
            raise ValueError(f"{source} : le modèle de gemini_test vient uniquement de GEMINI_TEST_MODEL")
        if not isinstance(d, dict):
            raise ValueError(f"{source} : déclaration de {name} invalide (objet attendu)")
        extra = set(d) - DECLARATION_KEYS
        if extra:
            raise ValueError(f"{source} : champs non autorisés pour {name} : {sorted(extra)}")
        provider = d.get("fournisseur")
        model = d.get("modele")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{source} : agent {name} sans modèle (champ « modele » obligatoire)")
        opts: List[Tuple[str, Any]] = []
        if "hote" in d:
            if provider not in ("ollama", "local_openai"):
                raise ValueError(f"{source} : « hote » réservé aux fournisseurs locaux ({name})")
            opts.append(("hote", require_loopback(str(d["hote"]))))
        for key in ("num_ctx", "timeout_s"):
            if key in d:
                if not isinstance(d[key], (int, float)) or isinstance(d[key], bool) or d[key] <= 0:
                    raise ValueError(f"{source} : {key} invalide pour {name}")
                opts.append((key, d[key]))
        specs.append(AgentSpec(str(name), str(provider), model.strip(), tuple(opts)))
    return specs


def build_registry(
    settings: Optional[config.Settings] = None,
    *,
    profile: Any = None,
    gemini_limiter: Optional[RateLimiter] = None,
    gemini_client_factory: Optional[GeminiClientFactory] = None,
    local_transport=None,
    claude_client_factory: Optional[Callable[[str], ClaudeCodeClient]] = None,
) -> AgentRegistry:
    """Registre réel : agents intégrés, puis AGENT_DECLARATIONS, puis déclarations du profil.

    gemini_limiter : passez le limiteur déjà utilisé par l'application, pour que
    tous les appels Gemini partagent la même file (sinon il est créé au premier
    agent Gemini, sur le fichier d'état de config.state_dir). Rien n'est construit
    ni appelé ici : les agents le sont à la demande.
    """
    s = settings or config.load_settings()
    limiter_box: List[RateLimiter] = [gemini_limiter] if gemini_limiter is not None else []

    def limiter() -> RateLimiter:
        if not limiter_box:
            limiter_box.append(RateLimiter(s.gemini, s.state_dir / "gemini_quota.json"))
        return limiter_box[0]

    def default_gemini_client(model: str, lim: RateLimiter) -> GeminiClient:
        return GeminiClient(config.gemini_api_key(), model, lim, timeout_s=s.gemini_timeout_s)

    def default_claude_client(model: str) -> ClaudeCodeClient:
        return ClaudeCodeClient(model=model, timeout_s=s.claude_timeout_s, allow_api_billing=s.claude_allow_api_billing,
                                counter_path=s.state_dir / "claude_calls.json")

    transport_kw = {"transport": local_transport} if local_transport is not None else {}

    def ollama(spec: AgentSpec) -> Agent:
        return LocalAgent(spec, OllamaBackend(
            spec.model, host=spec.option("hote", s.ollama_host), num_ctx=int(spec.option("num_ctx", 8192)),
            think=spec.option("think", None), keep_alive=s.ollama_keep_alive,
            timeout_s=float(spec.option("timeout_s", 300.0)), **transport_kw))

    def local_openai(spec: AgentSpec) -> Agent:
        return LocalAgent(spec, LocalOpenAIBackend(spec.model, host=spec.option("hote", "http://127.0.0.1:8080"),
                                                   timeout_s=float(spec.option("timeout_s", 300.0)), **transport_kw))

    def gemini(spec: AgentSpec) -> Agent:
        return GeminiAgent(spec, gemini_client_factory or default_gemini_client, limiter())

    def claude(spec: AgentSpec) -> Agent:
        return ClaudeAgent(spec, (claude_client_factory or default_claude_client)(spec.model))

    factories = {"ollama": ollama, "local_openai": local_openai, "gemini": gemini, "claude": claude}
    reg = AgentRegistry()
    specs = builtin_specs(s) + parse_declarations(s.agent_declarations, source="AGENT_DECLARATIONS")
    section = getattr(profile, "agents", None) or {}
    specs += parse_declarations(section.get("declarations"), source="profil")
    for spec in specs:
        reg.declare(spec, factories[spec.provider])
    return reg


# --------------------------------------------------------------- affectation


@dataclass(frozen=True)
class RoleRoute:
    """Agents d'un rôle : principal, agent des tâches difficiles (facultatif), replis."""

    role: str
    agent: str
    difficile: Optional[str] = None
    repli: Tuple[str, ...] = ()
    seuil_caracteres: int = 6000
    seuil_score: int = 5
    source: str = "défaut"  # défaut | configuration | profil

    def agents(self) -> List[str]:
        return list(dict.fromkeys([self.agent] + ([self.difficile] if self.difficile else []) + list(self.repli)))

    def plan(self, prompt: str, score: Optional[int] = None) -> Tuple[List[str], str]:
        """Critère déterministe, calculé par le code : score fourni par l'appelant, sinon longueur."""
        if not self.difficile:
            return list(dict.fromkeys([self.agent, *self.repli])), f"rôle {self.role} -> {self.agent} ({self.source})"
        if score is not None:
            hard = score >= self.seuil_score
            why = f"score {score} {'>=' if hard else '<'} {self.seuil_score}"
        else:
            hard = len(prompt) >= self.seuil_caracteres
            why = f"{len(prompt)} caractères {'>=' if hard else '<'} {self.seuil_caracteres}"
        if hard:
            return (list(dict.fromkeys([self.difficile, self.agent, *self.repli])),
                    f"tâche difficile ({why}) -> {self.difficile} ({self.source})")
        return (list(dict.fromkeys([self.agent, *self.repli])),
                f"tâche simple ({why}) -> {self.agent} ({self.source})")


ROLE_KEYS = {"agent", "difficile", "repli", "seuil_caracteres", "seuil_score"}


def _role_entry(role: str, value: Any, base: RoleRoute, source: str) -> RoleRoute:
    if role not in ROLES:
        raise ValueError(f"{source} : rôle inconnu « {role} » (rôles : {', '.join(ROLES)})")
    if isinstance(value, str):
        names = [n.strip() for n in value.split(",") if n.strip()]
        if not names:
            raise ValueError(f"{source} : rôle {role} sans agent")
        return replace(base, agent=names[0], repli=tuple(names[1:]), source=source)
    if not isinstance(value, dict):
        raise ValueError(f"{source} : rôle {role} : chaîne ou objet attendu")
    extra = set(value) - ROLE_KEYS
    if extra:
        raise ValueError(f"{source} : rôle {role} : champs non autorisés {sorted(extra)}")
    repli = value.get("repli", list(base.repli))
    if isinstance(repli, str):
        repli = [repli]
    if not isinstance(repli, list) or not all(isinstance(r, str) and r for r in repli):
        raise ValueError(f"{source} : rôle {role} : « repli » doit être une liste de noms d'agents")
    changes: Dict[str, Any] = {"repli": tuple(repli), "source": source}
    for key in ("agent", "difficile"):
        if key in value:
            if not isinstance(value[key], str) or not value[key]:
                raise ValueError(f"{source} : rôle {role} : « {key} » doit être un nom d'agent")
            changes[key] = value[key]
    for key in ("seuil_caracteres", "seuil_score"):
        if key in value:
            if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < 0:
                raise ValueError(f"{source} : rôle {role} : « {key} » doit être un entier positif")
            changes[key] = value[key]
    return replace(base, **changes)


def default_routes(s: config.Settings) -> Dict[str, RoleRoute]:
    """Défauts V3 : agent du rôle (claude_haiku), puis repli sur son agent local (Qwen)."""
    routes = {}
    for role, info in ROLES.items():
        agent = info.default_agent
        if role == "grand_modele" and s.big_model_provider in ("gemini", "claude"):
            agent = s.big_model_provider
        repli = () if agent == info.local_agent else (info.local_agent,)
        routes[role] = RoleRoute(role, agent, repli=repli, seuil_caracteres=s.agent_seuil_caracteres,
                                 seuil_score=s.agent_seuil_score)
    return routes


def resolve_routes(s: config.Settings, profile: Any = None,
                   known_agents: Optional[Iterable[str]] = None) -> Dict[str, RoleRoute]:
    """Défauts du projet, puis AGENT_<ROLE> / AGENT_<ROLE>_DIFFICILE, puis profil. Lève ValueError."""
    routes = default_routes(s)
    for role, raw in s.agent_roles:
        routes[role] = _role_entry(role, raw, routes.get(role) or RoleRoute(role, raw), "configuration")
    for role, raw in s.agent_roles_difficile:
        routes[role] = _role_entry(role, {"difficile": raw}, routes.get(role) or RoleRoute(role, raw),
                                   "configuration")
    section = getattr(profile, "agents", None) or {}
    for role, value in (section.get("roles") or {}).items():
        routes[role] = _role_entry(role, value, routes.get(role) or RoleRoute(role, ""), "profil")
    if known_agents is not None:
        known = set(known_agents)
        for r in routes.values():
            unknown = [a for a in r.agents() if a not in known]
            if unknown:
                raise ValueError(f"rôle {r.role} : agent(s) inconnu(s) {unknown} (agents : {', '.join(sorted(known))})")
    return routes


def validate_profile_agents(section: Any, *, personal_data: bool) -> dict:
    """Validation de la clé « agents » d'un profil (au chargement). Lève ValueError.

    Format :
        "agents": {
          "declarations": {"<nom>": {"fournisseur": "ollama", "modele": "...", "num_ctx": 4096}},
          "roles": {"<rôle>": "<agent>[,repli...]"
                    | {"agent": ..., "difficile": ..., "repli": [...], "seuil_caracteres": n, "seuil_score": n}}
        }
    Profil « données personnelles » : aucun agent externe, ni déclaré ni affecté.
    """
    if section in (None, {}):
        return {}
    if not isinstance(section, dict) or set(section) - {"declarations", "roles"}:
        raise ValueError("clé « agents » : objet avec « declarations » et/ou « roles » attendu")
    declared = {sp.name: sp for sp in parse_declarations(section.get("declarations"), source="profil")}
    external = {n for n in BUILTIN_EXTERNAL if n not in declared} | {n for n, sp in declared.items() if sp.external}
    known = (set(BUILTIN_NAMES) | set(declared))
    if personal_data and any(sp.external for sp in declared.values()):
        raise ValueError("un profil « données personnelles » ne peut déclarer aucun agent externe")
    roles = section.get("roles") or {}
    if not isinstance(roles, dict):
        raise ValueError("clé « agents.roles » : objet {rôle: agent} attendu")
    for role, value in roles.items():
        route = _role_entry(role, value, RoleRoute(role, ROLES.get(role, ROLES["format"]).default_agent), "profil")
        for name in route.agents():
            if name not in known:
                raise ValueError(f"rôle {role} : agent inconnu « {name} » (déclarez-le dans « declarations »)")
            if personal_data and name in external:
                raise ValueError(f"rôle {role} : agent externe « {name} » interdit pour un profil "
                                 "« données personnelles » (RGPD, aucun accord de traitement)")
    return section


# ------------------------------------------------------------------- routage


class AgentRouter:
    """Choisit l'agent d'un rôle, applique la politique de données, replie, journalise.

    logger : RunLogger du bus (orchestrator.py) ou tout objet avec event(kind, **meta).
    Le journal ne contient jamais le texte envoyé : seulement les métadonnées.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        routes: Mapping[str, RoleRoute],
        *,
        logger: Any = None,
        personal_data: Union[bool, Callable[[], bool]] = False,
        masker: Optional[Masker] = None,
        declared: Union[Iterable[str], Callable[[], Iterable[str]]] = (),
        confirm: Optional[Callable[[str], bool]] = None,
        repair: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.routes = dict(routes)
        for r in self.routes.values():
            for name in r.agents():
                registry.spec(name)  # agent inconnu : refus dès la construction
        self.logger = logger
        self._personal = personal_data
        self.masker = masker
        self._declared = declared
        self.confirm = confirm
        self._monotonic = monotonic
        self.journal: List[dict] = []
        # Accords de session : {(rôle, fournisseur de politique)}. Jamais d'accord global.
        self._session_consent: set = set()
        # Réparation JSON par le rôle « reparation » (claude_haiku puis qwen par défaut), mêmes règles.
        self.repair_llm: Optional[LocalLLM] = (RoleLLM(self, "reparation")
                                               if repair and "reparation" in self.routes else None)

    # ---------------------------------------------------------------- outils

    def personal_data(self) -> bool:
        return bool(self._personal() if callable(self._personal) else self._personal)

    def declared(self) -> List[str]:
        return list(self._declared() if callable(self._declared) else self._declared)

    def role_llm(self, role: str) -> "RoleLLM":
        return RoleLLM(self, role)

    def role_client(self, role: str) -> "RoleClient":
        return RoleClient(self, role)

    # ------------------------------------------------------- accord de session

    def grant_session_consent(self, role: str, provider: str) -> None:
        """Accord unique de l'utilisateur pour CE rôle et CE fournisseur, jusqu'à révocation.

        Refusé pour un rôle sans accord_session (la confirmation par envoi reste due) et
        pour un profil « données personnelles » (PolicyError, avant tout envoi).
        """
        info = ROLES.get(role)
        if info is None or not info.accord_session:
            raise ValueError(f"rôle « {role} » : accord de session non prévu (confirmation à chaque envoi)")
        if provider not in POLICY_PROVIDERS or not POLICY_PROVIDERS[provider].external:
            raise ValueError(f"fournisseur externe attendu : {provider!r}")
        check_provider(provider, personal_data=self.personal_data())  # lève PolicyError
        self._session_consent.add((role, provider))

    def grant_session_consent_all(self, provider: str) -> List[str]:
        """Accord explicite de l'utilisateur pour UN fournisseur, sur tous les rôles à accord de
        session dont un agent relève de ce fournisseur. Retourne ces rôles. Lève PolicyError en
        profil « données personnelles » (avant tout envoi)."""
        roles = [role for role, info in ROLES.items() if info.accord_session and role in self.routes
                 and any(self.registry.spec(a).policy_provider == provider for a in self.routes[role].agents())]
        for role in roles:
            self.grant_session_consent(role, provider)
        return roles

    def revoke_session_consent(self, role: Optional[str] = None) -> None:
        """Révoque l'accord d'un rôle, ou tous les accords (role=None)."""
        self._session_consent = {c for c in self._session_consent if role is not None and c[0] != role}

    def has_session_consent(self, role: str, provider: str) -> bool:
        """Lecture seule : jamais d'accord effectif en profil « données personnelles »."""
        if self.personal_data():
            return False
        return (role, provider) in self._session_consent

    def log_choice(self, role: str, agent: Optional[str], result: str, reason: str, **extra) -> None:
        """Journalise un choix fait hors de call() (repli local décidé par l'appelant...)."""
        spec = self.registry.spec(agent) if agent and agent in self.registry else None
        self._log(role, spec, result, reason, attempt=1, **extra)

    def _log(self, role: str, spec: Optional[AgentSpec], result: str, reason: str, *, attempt: int,
             duration_s: float = 0.0, **extra) -> None:
        rec = {"role": role, "agent": spec.name if spec else None, "provider": spec.provider if spec else None,
               "model": spec.model if spec else None, "external": spec.external if spec else None,
               "duration_s": round(duration_s, 3), "result": result, "reason": reason, "attempt": attempt, **extra}
        self.journal.append(rec)
        if self.logger is not None:
            self.logger.event("agent_call", **rec)

    # ------------------------------------------------------- envoi externe

    def _mask_external(self, prompt: str) -> Tuple[str, str]:
        """Masquage et contrôle de fuite. Retourne (texte masqué, aperçu). Lève LeakBlocked."""
        declared = self.declared()
        if self.masker is not None:
            res = self.masker.mask(prompt, declared=declared)
            leaks = self.masker.leak_check(res.text, declared=declared)
            text, preview = res.text, render_preview(res)
        else:
            leaks = detect(prompt, declared=declared)
            text = prompt
            preview = "\n".join(["=== Aperçu de ce qui sera envoyé (aucun masquage configuré) ===", prompt])
        if leaks:
            raise LeakBlocked(leaks)
        return text, preview

    def _confirm_external(self, role: str, sp: AgentSpec, preview: str) -> None:
        """Confirmation de l'utilisateur, en nommant le destinataire. Lève PreviewRefused."""
        dest = (f"=== Destinataire : agent externe « {sp.name} » (fournisseur {sp.provider}, "
                f"modèle {sp.model}), rôle « {role} » ===")
        if self.confirm is None or not self.confirm(dest + "\n" + preview):
            raise PreviewRefused("envoi externe non confirmé : rien n'a été envoyé")

    def _unmask(self, res: AgentResult, schema: Optional[dict]) -> AgentResult:
        if self.masker is None:
            return res
        unknown: List[str] = []

        def restore(value: Any) -> Any:
            if isinstance(value, str):
                text, bad = self.masker.unmask(value)
                unknown.extend(bad)
                return text
            if isinstance(value, list):
                return [restore(v) for v in value]
            if isinstance(value, dict):
                return {k: restore(v) for k, v in value.items()}
            return value

        if schema is not None:
            data = restore(res.data)
            try:
                text = json.dumps(data, ensure_ascii=False)
                data = parse_json(text, schema)
            except SchemaError as exc:
                raise AgentSchemaError(f"agent {res.agent} : hors schéma après réinjection locale ({exc})") from None
            res = replace(res, text=text, data=data)
        else:
            res = replace(res, text=restore(res.text))
        if unknown:
            self.journal.append({"role": res.role, "agent": res.agent, "result": "jetons_inconnus",
                                 "count": len(unknown)})
        return res

    # ------------------------------------------------------------------ appel

    def call(self, role: str, prompt: str, *, system: Optional[str] = None, schema: Optional[dict] = None,
             temperature: float = 0.2, json_output: bool = False, score: Optional[int] = None,
             free_text: Optional[bool] = None) -> AgentResult:
        if role not in self.routes:
            raise ValueError(f"rôle inconnu : {role} (rôles : {', '.join(self.routes)})")
        # free_text ne peut que renforcer la règle du rôle, jamais l'assouplir.
        free_text = bool(free_text) or (ROLES[role].free_text if role in ROLES else True)
        candidates, reason = self.routes[role].plan(prompt, score)
        specs = [self.registry.spec(n) for n in candidates]

        # 1. Politique de données, AVANT tout envoi et pour TOUS les candidats (replis compris) :
        #    en profil « données personnelles », les agents externes sont écartés et le rôle est
        #    traité en local (agent local du rôle, Qwen par défaut). Rien ne quitte la machine.
        if self.personal_data():
            self.revoke_session_consent()  # profil « données personnelles » : accords caducs (écriture explicite)
            kept = []
            for i, sp in enumerate(specs, 1):
                if sp.external:
                    self._log(role, sp, "refuse_politique", reason, attempt=i)
                else:
                    kept.append(sp)
            if not kept:
                local = ROLES[role].local_agent if role in ROLES else LOCAL_AGENT
                kept = [self.registry.spec(local)]
            if kept[0].name != specs[0].name:
                reason = f"profil « données personnelles » : repli local -> {kept[0].name}"
            specs = kept

        failures: List[Tuple[str, str]] = []
        outbound: Optional[Tuple[str, str]] = None  # (texte masqué, aperçu), préparés une seule fois
        confirmed: set = set()  # destinataires (fournisseur, modèle) déjà confirmés pour cet appel
        quota: Optional[AgentDailyQuota] = None  # quota journalier Gemini épuisé pendant cet appel
        repair_llm = None if role == "reparation" else self.repair_llm
        for i, sp in enumerate(specs, 1):
            why = reason if i == 1 else f"repli après {failures[-1][0]} ({failures[-1][1]})"
            if quota is not None and sp.provider == "gemini":
                # Même file, même quota : inutile de tenter un autre agent Gemini.
                failures.append((sp.name, AgentDailyQuota.kind))
                self._log(role, sp, AgentDailyQuota.kind, why, attempt=i, detail="non appelé")
                continue
            agent = self.registry.get(sp.name)
            missing = agent.unavailable_reason()
            if missing:
                failures.append((sp.name, "indisponible"))
                self._log(role, sp, "indisponible", why, attempt=i, detail=missing)
                continue
            text = prompt
            if sp.external:
                try:
                    if outbound is None:
                        outbound = self._mask_external(prompt)
                    # Confirmation redemandée si le repli bascule vers un autre destinataire externe,
                    # sauf accord de session donné pour CE rôle et CE fournisseur.
                    if free_text and (sp.provider, sp.model) not in confirmed:
                        if not self.has_session_consent(role, sp.policy_provider):
                            self._confirm_external(role, sp, outbound[1])
                        confirmed.add((sp.provider, sp.model))
                except LeakBlocked:
                    self._log(role, sp, "fuite_bloquee", why, attempt=i)
                    raise
                except PreviewRefused:
                    self._log(role, sp, "apercu_refuse", why, attempt=i)
                    raise
                text = outbound[0]
            t0 = self._monotonic()
            try:
                res = agent.call(text, system=system, schema=schema, temperature=temperature,
                                 json_output=json_output, repair_llm=repair_llm)
                if sp.external:
                    res = self._unmask(res, schema)
            except AgentDailyQuota as exc:
                quota = exc
                failures.append((sp.name, exc.kind))
                self._log(role, sp, exc.kind, why, attempt=i, duration_s=self._monotonic() - t0)
                continue
            except AgentError as exc:
                failures.append((sp.name, exc.kind))
                self._log(role, sp, exc.kind, why, attempt=i, duration_s=self._monotonic() - t0)
                continue
            res.role, res.reason = role, why
            self._log(role, sp, "ok", why, attempt=i, duration_s=self._monotonic() - t0, repair=res.repair)
            return res
        if quota is not None:
            raise quota  # arrêt du quota journalier, jamais dilué en échec ordinaire
        raise NoAgentSucceeded(role, failures)


# ------------------------------------------------- adaptateurs vers l'existant


class RoleLLM(LocalLLM):
    """Un rôle présenté comme un LocalLLM : route(), cadrer(), check_promise(),
    repair_json(), deduplicate(), estimate_complexity() l'utilisent sans modification,
    quel que soit l'agent configuré. Même forme de sortie : le schéma est validé
    par le code ; un échec devient LocalLLMError (ces fonctions échouent fermé).
    """

    def __init__(self, router: AgentRouter, role: str) -> None:
        if role not in router.routes:
            raise ValueError(f"rôle inconnu : {role}")
        self.router = router
        self.role = role
        self.model = f"role:{role}"
        self.last_result: Optional[AgentResult] = None

    def __repr__(self) -> str:
        return f"RoleLLM(role={self.role!r})"

    def chat(self, messages: Sequence[Message], *, temperature: float = 0.2,
             json_schema: Optional[dict] = None) -> LocalReply:
        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        prompt = "\n\n".join(m.content for m in messages if m.role != "system")
        try:
            res = self.router.call(self.role, prompt, system=system, schema=json_schema, temperature=temperature)
        except (LeakBlocked, PreviewRefused) as exc:
            raise ExternalSendBlocked(f"rôle « {self.role} » : {exc}") from None
        except AgentDailyQuota as exc:
            # Pas un échec local : l'arrêt du quota journalier remonte tel quel (App.handle).
            raise RoleDailyQuotaError(f"rôle « {self.role} » : {exc}") from exc.original
        except AgentError as exc:
            raise LocalLLMError(f"rôle « {self.role} » : {exc}") from None
        self.last_result = res
        return LocalReply(res.text, f"{res.agent}:{res.model}", res.latency_s, res.input_tokens, res.output_tokens)

    def list_models(self) -> List[str]:
        return self.router.routes[self.role].agents()


@dataclass
class RoleReply:
    """Forme de GeminiResult / ClaudeResult attendue par organisateurs, workers, revue, escalade."""

    text: str
    attempts: int = 1
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_s: float = 0.0
    truncated: bool = False
    model: str = ""
    agent: str = ""


class RoleClient:
    """Un rôle présenté comme un client generate() (GeminiOrganizer, GeminiWorker,
    ReviewPanel, Escalator l'acceptent tel quel)."""

    def __init__(self, router: AgentRouter, role: str, *, score: Optional[int] = None) -> None:
        if role not in router.routes:
            raise ValueError(f"rôle inconnu : {role}")
        self.router = router
        self.role = role
        self.score = score
        self.model = f"role:{role}"

    def __repr__(self) -> str:
        return f"RoleClient(role={self.role!r})"

    def generate(self, prompt: str, *, system: Optional[str] = None, temperature: float = 0.7,
                 json_output: bool = False, max_output_tokens: Optional[int] = None) -> RoleReply:
        try:
            r = self.router.call(self.role, prompt, system=system, temperature=temperature,
                                 json_output=json_output, score=self.score)
        except (LeakBlocked, PreviewRefused) as exc:
            raise RoleCallError(f"rôle « {self.role} » : {exc}") from None
        except AgentDailyQuota as exc:
            raise RoleDailyQuotaError(f"rôle « {self.role} » : {exc}") from exc.original
        except AgentError as exc:
            raise RoleCallError(str(exc)) from None
        return RoleReply(r.text, r.attempts, r.input_tokens, r.output_tokens, r.latency_s, r.truncated,
                         r.model, r.agent)


def build_router(settings: Optional[config.Settings] = None, *, profile: Any = None,
                 registry: Optional[AgentRegistry] = None, **router_kw) -> AgentRouter:
    """Assemblage : registre + affectation (configuration puis profil) + politique du profil."""
    s = settings or config.load_settings()
    reg = registry or build_registry(s, profile=profile)
    routes = resolve_routes(s, profile, reg.names())
    router_kw.setdefault("personal_data", bool(getattr(profile, "personal_data", False)))
    return AgentRouter(reg, routes, **router_kw)

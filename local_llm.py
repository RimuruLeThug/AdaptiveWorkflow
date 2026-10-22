"""Modèles locaux : interface unique, indépendante du backend.

Implémentation V1 : Ollama (API REST locale, bibliothèque standard).
Les modèles locaux ne passent PAS par le limiteur Gemini : leur concurrence
se règle côté Ollama (OLLAMA_NUM_PARALLEL, VRAM).

Vérification rapide :
    python -m local_llm
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from verify.schema import parse_json


class LocalLLMError(Exception):
    """Le modèle local est injoignable ou a répondu une erreur."""


@dataclass(frozen=True)
class LocalReply:
    text: str
    model: str
    latency_s: float = 0.0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str


class LocalLLM(ABC):
    model: str

    @abstractmethod
    def chat(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.2,
        json_schema: Optional[dict] = None,
    ) -> LocalReply:
        """Une réponse complète (pas de streaming). json_schema : sortie contrainte au format JSON."""

    @abstractmethod
    def list_models(self) -> List[str]:
        """Modèles disponibles sur le backend."""

    def ask(self, prompt: str, *, system: Optional[str] = None, temperature: float = 0.2) -> LocalReply:
        msgs = ([Message("system", system)] if system else []) + [Message("user", prompt)]
        return self.chat(msgs, temperature=temperature)

    def ask_json(self, prompt: str, schema: dict, *, system: Optional[str] = None, temperature: float = 0.0):
        """Retourne (objet validé, réponse brute). Lève SchemaError si le JSON est invalide."""
        msgs = ([Message("system", system)] if system else []) + [Message("user", prompt)]
        reply = self.chat(msgs, temperature=temperature, json_schema=schema)
        return parse_json(reply.text, schema), reply


# Transport : (méthode, url, corps ou None, timeout) -> (statut, corps)
Transport = Callable[[str, str, Optional[bytes], float], "tuple[int, bytes]"]


def urllib_transport(method: str, url: str, body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalLLMError(
            f"Ollama ne répond pas ({type(exc).__name__}: {exc}). "
            "Vérifiez que l'application Ollama est lancée."
        ) from None


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Durée pendant laquelle Ollama garde un modèle en VRAM après la dernière requête.
# Courte : si le programme est tué sans passer par l'arrêt propre (fenêtre fermée,
# plantage), la VRAM est tout de même rendue au bout de ce délai.
DEFAULT_KEEP_ALIVE = "2m"


class OllamaBackend(LocalLLM):
    def __init__(
        self,
        model: str,
        *,
        host: str = "http://localhost:11434",
        num_ctx: int = 8192,
        think: Optional[bool] = False,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
        timeout_s: float = 300.0,
        transport: Transport = urllib_transport,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise ValueError("nom de modèle Ollama vide")
        self.model = model
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        self.think = think  # False : désactive le raisonnement des modèles « thinking » (plus rapide)
        self.keep_alive = keep_alive
        self.timeout_s = timeout_s
        self._transport = transport
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return f"OllamaBackend(model={self.model!r}, host={self.host!r})"

    def _post(self, path: str, payload: dict) -> dict:
        status, body = self._transport("POST", self.host + path, json.dumps(payload).encode("utf-8"), self.timeout_s)
        return self._decode(status, body)

    def _decode(self, status: int, body: bytes) -> dict:
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if status != 200:
            err = str(data.get("error") or body[:300].decode("utf-8", "replace"))
            if status == 404 or "not found" in err.lower():
                raise LocalLLMError(f"Modèle « {self.model} » absent d'Ollama. Installez-le : ollama pull {self.model}")
            raise LocalLLMError(f"Ollama HTTP {status} : {err}")
        return data

    def chat(self, messages, *, temperature=0.2, json_schema=None) -> LocalReply:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        if json_schema is not None:
            payload["format"] = json_schema  # sortie structurée : Ollama contraint le décodage
        if self.think is not None:
            payload["think"] = self.think
        t0 = self._monotonic()
        try:
            data = self._post("/api/chat", payload)
        except LocalLLMError as exc:
            # Modèle sans mode « thinking » : on retire le paramètre et on réessaie une fois.
            if "think" in payload and "think" in str(exc).lower():
                self.think = None
                del payload["think"]
                data = self._post("/api/chat", payload)
            else:
                raise
        msg = data.get("message") or {}
        text = _THINK_RE.sub("", str(msg.get("content", ""))).strip()
        return LocalReply(
            text=text,
            model=self.model,
            latency_s=self._monotonic() - t0,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
        )

    def unload(self) -> bool:
        """Décharge ce modèle de la VRAM tout de suite (keep_alive=0). Ne lève jamais d'exception."""
        try:
            payload = json.dumps({"model": self.model, "keep_alive": 0}).encode("utf-8")
            status, _ = self._transport("POST", self.host + "/api/generate", payload, 15.0)
            return status == 200
        except Exception:  # Ollama éteint ou injoignable : rien à libérer
            return False

    def loaded_models(self) -> List[str]:
        """Noms des modèles actuellement chargés en mémoire par Ollama (/api/ps)."""
        status, body = self._transport("GET", self.host + "/api/ps", None, 10.0)
        data = self._decode(status, body)
        return [str(m.get("name") or m.get("model") or "") for m in data.get("models") or [] if isinstance(m, dict)]

    def list_models(self) -> List[str]:
        status, body = self._transport("GET", self.host + "/api/tags", None, 10.0)
        data = self._decode(status, body)
        return [str(m.get("name", "")) for m in data.get("models") or [] if isinstance(m, dict)]

    def check_available(self) -> None:
        """Lève LocalLLMError si Ollama est injoignable ou si le modèle n'est pas installé."""
        names = self.list_models()
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if self.model not in names and wanted not in names:
            family = self.model.split(":")[0] + ":"
            variants = [n for n in names if n.startswith(family)]
            hint = (f" Variante déjà installée : {', '.join(variants)} — indiquez-la dans .env "
                    f"(QWEN_MODEL=...).") if variants else ""
            raise LocalLLMError(
                f"Modèle « {self.model} » absent d'Ollama (installés : {', '.join(names) or 'aucun'}). "
                f"Installez-le : ollama pull {self.model}.{hint}"
            )


def build_qwen_llm() -> OllamaBackend:
    """Le modèle Qwen unique du projet (V3), réflexion désactivée."""
    import config

    config.load_dotenv()
    s = config.load_settings()
    return OllamaBackend(s.qwen_model, host=s.ollama_host, num_ctx=s.qwen_num_ctx, think=False,
                         keep_alive=s.ollama_keep_alive)


def _tag(name: str) -> str:
    return name if ":" in name else name + ":latest"


def release_vram(
    models: Sequence[str],
    *,
    host: str = "http://localhost:11434",
    transport: Transport = urllib_transport,
) -> List[str]:
    """Décharge de la VRAM les modèles du projet qui sont chargés. Ne lève jamais d'exception.

    Ne touche QUE aux modèles listés : les autres modèles que vous utilisez dans
    Ollama pour autre chose restent chargés. Retourne les noms réellement déchargés
    (liste vide si Ollama est éteint ou si rien n'était chargé).
    """
    wanted = {_tag(m) for m in models if m}
    if not wanted:
        return []
    probe = OllamaBackend(next(iter(wanted)), host=host, transport=transport)
    try:
        loaded = probe.loaded_models()
    except Exception:  # Ollama éteint : la VRAM est déjà libre
        return []
    released: List[str] = []
    for name in loaded:
        if _tag(name) in wanted and OllamaBackend(name, host=host, transport=transport).unload():
            released.append(name)
    return released


def release_project_vram() -> List[str]:
    """Décharge le modèle Qwen du projet (réglages courants de config)."""
    import config

    config.load_dotenv()
    s = config.load_settings()
    return release_vram([s.qwen_model], host=s.ollama_host)


if __name__ == "__main__":
    if "unload" in sys.argv[1:]:
        done = release_project_vram()
        print("Modèles déchargés de la VRAM : " + (", ".join(done) if done else "aucun (rien n'était chargé)"))
        sys.exit(0)
    llm = build_qwen_llm()
    try:
        llm.check_available()
        r = llm.ask("Réponds uniquement par le mot : OK", temperature=0)
        print(f"{llm.model} : {r.text!r} en {r.latency_s:.1f} s ({r.output_tokens} tokens)")
    except LocalLLMError as exc:
        print(f"{llm.model} : ÉCHEC — {exc}")
    print("Vérifiez ensuite le GPU avec : ollama ps   (colonne PROCESSOR : 100% GPU attendu)")

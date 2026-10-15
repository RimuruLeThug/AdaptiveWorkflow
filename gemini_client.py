"""Client Gemini minimal (API REST, bibliothèque standard uniquement).

Chaque requête passe par le limiteur de débit : file unique, espacement,
retries sur 429, compteur journalier. Ce module n'appelle JAMAIS l'API en
dehors du limiteur.

La clé API est envoyée dans l'en-tête x-goog-api-key (jamais dans l'URL) et
n'apparaît dans aucun message d'erreur ni journal.

Test réel (1 requête, compte dans le quota) :
    python -m gemini_client
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from rate_limiter import RateLimited, RateLimiter

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    """Erreur de l'API Gemini ou réponse inexploitable (non retentée)."""


class GeminiDailyQuotaError(GeminiError):
    """Google signale un quota JOURNALIER épuisé : inutile de retenter avant minuit (Pacifique)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict
    body: bytes


# Transport : (url, headers, body, timeout) -> HttpResponse. Injectable pour les tests.
Transport = Callable[[str, dict, bytes, float], HttpResponse]


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> HttpResponse:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeminiError(f"Réseau : impossible de joindre l'API Gemini ({type(exc).__name__}: {exc})") from None


@dataclass
class GeminiResult:
    text: str
    model: str
    finish_reason: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_s: float = 0.0
    attempts: int = 1
    raw_usage: dict = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "MAX_TOKENS"


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _parse_retry_after(headers: dict, payload: dict) -> Optional[float]:
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    for d in (payload.get("error") or {}).get("details") or []:
        if isinstance(d, dict) and str(d.get("@type", "")).endswith("google.rpc.RetryInfo"):
            m = _DURATION_RE.match(str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def _is_daily_quota(payload: dict) -> bool:
    for d in (payload.get("error") or {}).get("details") or []:
        if not isinstance(d, dict) or not str(d.get("@type", "")).endswith("google.rpc.QuotaFailure"):
            continue
        for v in d.get("violations") or []:
            quota_id = str(v.get("quotaId", "")) + str(v.get("quotaMetric", ""))
            if "PerDay" in quota_id or "per_day" in quota_id.lower():
                return True
    return False


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        limiter: RateLimiter,
        *,
        transport: Transport = urllib_transport,
        timeout_s: float = 120.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("clé API Gemini vide")
        if not model:
            raise ValueError("modèle Gemini non renseigné")
        self._api_key = api_key
        self.model = model
        self.limiter = limiter
        self._transport = transport
        self._timeout = timeout_s
        self._monotonic = monotonic
        self.requests_sent = 0  # cumul des requêtes réellement envoyées par ce client

    def __repr__(self) -> str:  # ne jamais exposer la clé
        return f"GeminiClient(model={self.model!r})"

    def _scrub(self, text: str) -> str:
        return text.replace(self._api_key, "***")

    def generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        json_output: bool = False,
        max_output_tokens: Optional[int] = None,
    ) -> GeminiResult:
        """Une génération. Toutes les tentatives passent par le limiteur."""
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        gen: dict = {"temperature": temperature}
        if json_output:
            gen["responseMimeType"] = "application/json"
        if max_output_tokens:
            gen["maxOutputTokens"] = max_output_tokens
        body["generationConfig"] = gen
        data = json.dumps(body).encode("utf-8")
        url = f"{API_BASE}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}

        attempts = 0
        t0 = self._monotonic()

        def once() -> dict:
            nonlocal attempts
            attempts += 1
            self.requests_sent += 1
            resp = self._transport(url, headers, data, self._timeout)
            try:
                payload = json.loads(resp.body.decode("utf-8")) if resp.body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if resp.status == 429:
                if _is_daily_quota(payload):
                    raise GeminiDailyQuotaError("Google signale le quota journalier épuisé (429 PerDay).")
                raise RateLimited(retry_after_s=_parse_retry_after(resp.headers, payload))
            if resp.status != 200:
                msg = (payload.get("error") or {}).get("message") or resp.body[:300].decode("utf-8", "replace")
                raise GeminiError(self._scrub(f"HTTP {resp.status} : {msg}"))
            return payload

        payload = self.limiter.call(once)
        latency = self._monotonic() - t0
        return self._parse(payload, latency, attempts)

    def _parse(self, payload: dict, latency: float, attempts: int) -> GeminiResult:
        usage = payload.get("usageMetadata") or {}
        candidates = payload.get("candidates") or []
        if not candidates:
            reason = (payload.get("promptFeedback") or {}).get("blockReason", "aucun candidat")
            raise GeminiError(f"Réponse vide de Gemini ({reason}).")
        cand = candidates[0]
        finish = str(cand.get("finishReason", "UNKNOWN"))
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        if not text:
            raise GeminiError(f"Réponse sans texte de Gemini (finishReason={finish}).")
        return GeminiResult(
            text=text,
            model=self.model,
            finish_reason=finish,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            latency_s=latency,
            attempts=attempts,
            raw_usage=usage,
        )


def build_default_client() -> GeminiClient:
    """Client réel à partir de .env et de config.py. Refuse de démarrer sans clé."""
    import config

    config.load_dotenv()
    s = config.load_settings()
    limiter = RateLimiter(s.gemini, s.state_dir / "gemini_quota.json")
    return GeminiClient(config.gemini_api_key(), s.gemini_model, limiter, timeout_s=s.gemini_timeout_s)


if __name__ == "__main__":
    client = build_default_client()
    print(f"Modèle : {client.model} — requêtes utilisées aujourd'hui : {client.limiter.used_today}")
    r = client.generate("Réponds uniquement par le mot : OK", temperature=0)
    print(f"Réponse : {r.text.strip()!r} ({r.latency_s:.1f} s, {r.input_tokens} → {r.output_tokens} tokens)")
    print(f"Requêtes utilisées aujourd'hui : {client.limiter.used_today}/{client.limiter.quota.daily_hard_stop}")

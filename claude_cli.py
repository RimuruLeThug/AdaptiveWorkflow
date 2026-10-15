"""Claude via Claude Code en mode non interactif (`claude -p`), sur l'abonnement Pro.

Choix de l'utilisateur : le mode Claude consomme le quota de son abonnement
claude.ai (Pro), pas l'API facturée à l'usage. D'après la documentation
d'Anthropic, `claude -p` (hors mode --bare) puise dans les limites de
l'abonnement de la session connectée.

Garde-fous :
- Claude Code doit être installé et connecté avec un compte claude.ai
  (`claude auth status` → authMethod « claude.ai ») ; une connexion par clé API
  est REFUSÉE, sauf autorisation explicite (CLAUDE_ALLOW_API_BILLING=1), pour ne
  jamais facturer l'API sans le vouloir ;
- ANTHROPIC_API_KEY est retirée de l'environnement de l'appel ;
- aucun outil : Claude ne lit ni n'écrit aucun fichier, n'exécute rien ;
- exécution dans un dossier temporaire vide ; session non sauvegardée ;
- Claude n'est jamais présenté comme « sans coût » : il consomme le quota Pro.

Ces appels ne passent pas par le limiteur Gemini ; ils sont comptés à part
(state/claude_calls.json, jour local).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

Runner = Callable[..., subprocess.CompletedProcess]


class ClaudeCodeError(Exception):
    """Claude Code indisponible, non connecté, ou réponse en erreur."""


@dataclass
class ClaudeResult:
    text: str
    attempts: int = 1
    truncated: bool = False
    latency_s: float = 0.0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_estimate_usd: Optional[float] = None  # estimation « équivalent API » de Claude Code, pas une facture


COST_NOTE = ("consomme le quota de ton abonnement Claude Pro (pas de facturation API). "
             "Les limites d'usage sont celles de ton abonnement.")


class ClaudeCodeClient:
    def __init__(
        self,
        *,
        model: str = "sonnet",
        executable: Optional[str] = None,
        timeout_s: float = 600.0,
        allow_api_billing: bool = False,
        counter_path: Optional[Path] = None,
        runner: Runner = subprocess.run,
        which: Callable[[str], Optional[str]] = shutil.which,
        monotonic: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.model = model
        self._executable = executable
        self.timeout_s = timeout_s
        self.allow_api_billing = allow_api_billing
        self.counter_path = counter_path
        self._run = runner
        self._which = which
        self._monotonic = monotonic
        self._today = today
        self._checked = False

    def __repr__(self) -> str:
        return f"ClaudeCodeClient(model={self.model!r})"

    # ------------------------------------------------------------ disponibilité

    def executable(self) -> str:
        exe = self._executable or self._which("claude")
        if not exe:
            raise ClaudeCodeError(
                "Claude Code n'est pas installé (commande « claude » introuvable). "
                "Installez-le, puis connectez-vous avec votre compte Claude Pro : claude auth login")
        return exe

    def _env(self) -> dict:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # forcer l'abonnement, jamais l'API par accident
        return env

    def check_available(self) -> str:
        """Vérifie l'installation et la connexion. Retourne la méthode d'authentification."""
        exe = self.executable()
        try:
            proc = self._run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60, env=self._env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaudeCodeError(f"Claude Code ne répond pas : {exc}") from None
        try:
            status = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            status = {}
        method = str(status.get("authMethod", "none"))
        if proc.returncode != 0 or method == "none":
            raise ClaudeCodeError("Claude Code n'est pas connecté. Lancez : claude auth login (compte Claude Pro)")
        if method != "claude.ai" and not self.allow_api_billing:
            raise ClaudeCodeError(
                f"Claude Code est connecté par « {method} », ce qui facturerait l'API. Le mode Claude est prévu "
                "sur l'abonnement Pro : reconnectez-vous avec claude auth login (compte claude.ai), "
                "ou autorisez explicitement la facturation API avec CLAUDE_ALLOW_API_BILLING=1.")
        self._checked = True
        return method

    # ------------------------------------------------------------- génération

    def generate(self, prompt: str, *, system: Optional[str] = None, temperature: Optional[float] = None,
                 json_output: bool = False, max_output_tokens: Optional[int] = None) -> ClaudeResult:
        """Un appel `claude -p`. temperature et max_output_tokens ne sont pas réglables via la CLI."""
        if not self._checked:
            self.check_available()
        exe = self.executable()
        system_text = (system or "Tu es un assistant rigoureux.") + (
            "\n\nRéponds uniquement avec un document JSON valide, sans texte autour." if json_output else "")
        with tempfile.TemporaryDirectory(prefix="orch-claude-") as workdir:
            sys_file = Path(workdir) / "system.txt"
            sys_file.write_text(system_text, encoding="utf-8")
            cmd = [
                exe, "-p",
                "--output-format", "json",
                "--model", self.model,
                "--system-prompt-file", str(sys_file),
                "--disallowedTools", "*",
                "--strict-mcp-config",
                "--permission-mode", "dontAsk",
                "--no-session-persistence",
                "--max-turns", "3",
            ]
            t0 = self._monotonic()
            self._count()
            try:
                proc = self._run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=self.timeout_s, cwd=workdir, env=self._env())
            except subprocess.TimeoutExpired:
                raise ClaudeCodeError(f"Claude Code n'a pas répondu en {self.timeout_s:.0f} s") from None
            except OSError as exc:
                raise ClaudeCodeError(f"Claude Code n'a pas pu démarrer : {exc}") from None
            latency = self._monotonic() - t0
        return self._parse(proc, latency)

    def _parse(self, proc: subprocess.CompletedProcess, latency: float) -> ClaudeResult:
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out.splitlines()[-1] if out else "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict) or not data:
            err = (proc.stderr or out or "réponse vide")[-400:]
            raise ClaudeCodeError(f"réponse illisible de Claude Code (code {proc.returncode}) : {err}")
        if data.get("is_error") or proc.returncode != 0:
            msg = str(data.get("result") or data.get("subtype") or proc.stderr or "erreur inconnue")[:400]
            if "limit" in msg.lower():
                msg = f"limite d'usage de l'abonnement atteinte ? {msg}"
            raise ClaudeCodeError(f"Claude Code a renvoyé une erreur : {msg}")
        text = str(data.get("result", "")).strip()
        if not text:
            raise ClaudeCodeError("Claude Code a renvoyé une réponse vide")
        usage = data.get("usage") or {}
        return ClaudeResult(
            text=text,
            latency_s=latency,
            truncated=data.get("stop_reason") == "max_tokens",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_estimate_usd=data.get("total_cost_usd"),
        )

    # -------------------------------------------------------------- compteur

    def _count(self) -> None:
        if self.counter_path is None:
            return
        today = self._today().isoformat()
        try:
            raw = json.loads(self.counter_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        n = raw.get("count", 0) + 1 if raw.get("day") == today else 1
        self.counter_path.parent.mkdir(parents=True, exist_ok=True)
        self.counter_path.write_text(json.dumps({"day": today, "count": n}), encoding="utf-8")

    def calls_today(self) -> int:
        if self.counter_path is None or not self.counter_path.exists():
            return 0
        try:
            raw = json.loads(self.counter_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return raw.get("count", 0) if raw.get("day") == self._today().isoformat() else 0

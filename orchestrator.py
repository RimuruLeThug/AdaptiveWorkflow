"""Bus d'orchestration (brief, section 3).

Les agents ne s'appellent jamais directement : tout passe par ce code.
L'organisateur propose, le code décide :
- liste fermée de workers (ceux déclarés par le profil) ;
- budget de délégations par tâche ;
- profondeur maximale (l'organisateur délègue, les workers ne délèguent pas) ;
- plan invalide refusé EN ENTIER (jamais exécuté partiellement ni tronqué) ;
- vérification obligatoire de chaque sortie avant tout retour ou synthèse ;
- isolement des workers : chacun ne voit que sa consigne et la fiche projet.

Chaque run écrit un journal JSONL dans logs/ (mesures du pilote, section 14).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Sequence

from gemini_client import GeminiDailyQuotaError, GeminiError
from organizers.base import Organizer, WorkerResult
from rate_limiter import DailyQuotaExhausted, RetriesExhausted
from verify.basic import Verifier
from verify.repair import RepairResult
from verify.schema import SchemaError, parse_json
from workers.base import Reply, Worker, WorkerSpec

log = logging.getLogger(__name__)

MAX_INSTRUCTION_CHARS = 4000


class OrchestrationError(Exception):
    """Base des refus du bus."""


class PlanRejected(OrchestrationError):
    """Plan de l'organisateur refusé par le code."""


class DepthExceeded(OrchestrationError):
    """Délégation au-delà de la profondeur maximale."""


def plan_schema(worker_names: Sequence[str], max_tasks: int) -> dict:
    return {
        "type": "object",
        "required": ["tasks"],
        "additionalProperties": False,
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_tasks,
                "items": {
                    "type": "object",
                    "required": ["worker", "instruction"],
                    "additionalProperties": False,
                    "properties": {
                        "worker": {"type": "string", "enum": list(worker_names)},
                        "instruction": {"type": "string", "minLength": 1, "maxLength": MAX_INSTRUCTION_CHARS},
                    },
                },
            }
        },
    }


@dataclass(frozen=True)
class PlannedTask:
    worker: str
    instruction: str


def validate_plan(raw: str, worker_names: Sequence[str], max_tasks: int) -> List[PlannedTask]:
    """Le code décide : JSON strict, workers de la liste fermée, budget respecté."""
    if max_tasks < 1:
        raise PlanRejected("budget de délégations épuisé")
    try:
        doc = parse_json(raw, plan_schema(worker_names, max_tasks))
    except SchemaError as exc:
        msg = str(exc)
        if "hors de" in msg and ".worker" in msg:
            msg = f"worker inconnu ({msg}). Workers autorisés : {', '.join(worker_names)}"
        elif "maximum" in msg and "éléments" in msg:
            msg = f"trop de délégations ({msg}), budget restant : {max_tasks}"
        raise PlanRejected(msg) from None
    tasks = [PlannedTask(t["worker"], t["instruction"].strip()) for t in doc["tasks"]]
    if any(not t.instruction for t in tasks):
        raise PlanRejected("consigne vide")
    return tasks


@dataclass
class TaskOutcome:
    worker: str
    instruction: str
    text: str = ""
    verified: bool = False
    issues: List[str] = field(default_factory=list)
    error: Optional[str] = None
    attempts: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_s: float = 0.0
    repair: Optional[str] = None  # méthode de réparation JSON appliquée, le cas échéant


Repairer = Callable[[str, dict], RepairResult]  # (texte, schéma) -> résultat


@dataclass
class RunReport:
    run_id: str
    task: str
    status: str = "pending"  # ok | partial | failed | aborted
    plan: List[PlannedTask] = field(default_factory=list)
    accepted: List[TaskOutcome] = field(default_factory=list)  # vérifiés
    rejected: List[TaskOutcome] = field(default_factory=list)  # échec ou vérification refusée
    synthesis: Optional[str] = None
    plan_attempts: int = 0
    delegations_used: int = 0
    gemini_requests: int = 0  # requêtes envoyées, retries compris (exact si request_counter fourni)
    abort_reason: Optional[str] = None
    log_path: Optional[Path] = None


class RunLogger:
    """Journal JSONL d'un run. log_content=False : pas de texte, seulement les métadonnées."""

    def __init__(self, logs_dir: Optional[Path], run_id: str, *, log_content: bool = True) -> None:
        self.log_content = log_content
        self.path: Optional[Path] = None
        if logs_dir is not None:
            Path(logs_dir).mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.path = Path(logs_dir) / f"run-{stamp}-{run_id}.jsonl"

    def event(self, kind: str, *, content: Optional[dict] = None, **meta) -> None:
        if self.path is None:
            return
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": kind, **meta}
        if content and self.log_content:
            rec.update(content)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


_ABORT = (DailyQuotaExhausted, GeminiDailyQuotaError)


class Orchestrator:
    def __init__(
        self,
        organizer: Organizer,
        workers: Sequence[Worker],
        verifier: Verifier,
        *,
        delegation_budget: int = 6,
        max_depth: int = 1,
        max_plan_attempts: int = 2,
        logs_dir: Optional[Path] = None,
        log_content: bool = True,
        request_counter: Optional[Callable[[], int]] = None,
        repairer: Optional[Repairer] = None,
    ) -> None:
        """request_counter : compteur cumulatif des requêtes Gemini envoyées
        (ex. lambda: client.requests_sent). Sans lui, le décompte est un minimum."""
        if verifier is None:
            raise ValueError("un vérificateur est obligatoire (aucun retour sans vérification)")
        if delegation_budget < 1 or max_depth < 1 or max_plan_attempts < 1:
            raise ValueError("delegation_budget, max_depth et max_plan_attempts doivent être >= 1")
        self._workers: Dict[str, Worker] = {}
        for w in workers:
            if w.spec.name in self._workers:
                raise ValueError(f"worker déclaré deux fois : {w.spec.name}")
            self._workers[w.spec.name] = w
        if not self._workers:
            raise ValueError("aucun worker déclaré")
        self.organizer = organizer
        self.verifier = verifier
        self.delegation_budget = delegation_budget
        self.max_depth = max_depth
        self.max_plan_attempts = max_plan_attempts
        self.logs_dir = logs_dir
        self.log_content = log_content
        self.request_counter = request_counter
        self.repairer = repairer  # Qwen 2 Go : répare un JSON invalide, puis le code revérifie

    @property
    def worker_specs(self) -> List[WorkerSpec]:
        return [w.spec for w in self._workers.values()]

    def run(self, task: str, context: str = "", *, synthesize: bool = True, depth: int = 0) -> RunReport:
        """Exécute une tâche : plan → délégations isolées → vérification → synthèse."""
        if depth + 1 > self.max_depth:
            raise DepthExceeded(f"profondeur {depth + 1} > maximum {self.max_depth} : délégation refusée")
        if not task.strip():
            raise ValueError("tâche vide")

        run_id = uuid.uuid4().hex[:8]
        rlog = RunLogger(self.logs_dir, run_id, log_content=self.log_content)
        report = RunReport(run_id=run_id, task=task, log_path=rlog.path)
        rlog.event("run_start", depth=depth, budget=self.delegation_budget,
                   workers=list(self._workers), content={"task": task, "context": context})
        t_run = time.monotonic()
        counter_start = self.request_counter() if self.request_counter else None
        try:
            report.plan = self._plan(task, context, report, rlog)
            self._execute(report.plan, context, report, rlog)
            if synthesize and report.accepted:
                self._synthesize(task, context, report, rlog)
            report.status = self._status(report)
        except _ABORT as exc:
            report.status = "aborted"
            report.abort_reason = str(exc)
            rlog.event("run_aborted", reason=str(exc))
            raise
        except PlanRejected as exc:
            report.status = "failed"
            report.abort_reason = str(exc)
            rlog.event("run_failed", reason=str(exc))
            raise
        finally:
            if counter_start is not None:
                report.gemini_requests = self.request_counter() - counter_start
            rlog.event("run_end", status=report.status, plan_attempts=report.plan_attempts,
                       delegations_used=report.delegations_used, gemini_requests=report.gemini_requests,
                       accepted=len(report.accepted), rejected=len(report.rejected),
                       duration_s=round(time.monotonic() - t_run, 3))
        return report

    # ------------------------------------------------------------------ étapes

    def _plan(self, task: str, context: str, report: RunReport, rlog: RunLogger) -> List[PlannedTask]:
        feedback: Optional[str] = None
        names = list(self._workers)
        budget_left = self.delegation_budget - report.delegations_used
        for attempt in range(1, self.max_plan_attempts + 1):
            report.plan_attempts = attempt
            try:
                reply = self.organizer.propose_plan(task, context, self.worker_specs, budget_left, feedback)
            except GeminiError as exc:
                if isinstance(exc, GeminiDailyQuotaError):
                    raise
                raise PlanRejected(f"organisateur indisponible : {exc}") from exc
            except RetriesExhausted as exc:
                raise PlanRejected(f"organisateur indisponible : {exc}") from exc
            report.gemini_requests += reply.attempts
            try:
                plan = validate_plan(reply.text, names, budget_left)
            except PlanRejected as exc:
                feedback = str(exc)
                rlog.event("plan_rejected", attempt=attempt, reason=feedback, content={"raw": reply.text},
                           attempts=reply.attempts, latency_s=reply.latency_s,
                           input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
                log.warning("Plan refusé (tentative %d/%d) : %s", attempt, self.max_plan_attempts, feedback)
                continue
            rlog.event("plan_accepted", attempt=attempt, n_tasks=len(plan),
                       content={"plan": [asdict(t) for t in plan]},
                       attempts=reply.attempts, latency_s=reply.latency_s,
                       input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
            return plan
        raise PlanRejected(f"plan refusé après {self.max_plan_attempts} tentative(s) : {feedback}")

    def _execute(self, plan: List[PlannedTask], context: str, report: RunReport, rlog: RunLogger) -> None:
        for i, pt in enumerate(plan):
            if report.delegations_used >= self.delegation_budget:  # garde-fou, normalement impossible
                raise PlanRejected("budget de délégations dépassé pendant l'exécution")
            worker = self._workers[pt.worker]
            report.delegations_used += 1
            out = TaskOutcome(worker=pt.worker, instruction=pt.instruction)
            try:
                # Isolement : uniquement la consigne et la fiche projet.
                reply = worker.run(pt.instruction, context)
            except _ABORT:
                raise
            except (GeminiError, RetriesExhausted) as exc:
                out.error = f"{type(exc).__name__}: {exc}"
                report.gemini_requests += 1  # minimum ; exact via request_counter
                report.rejected.append(out)
                rlog.event("delegation_failed", index=i, worker=pt.worker, error=out.error)
                continue
            out.text = reply.text
            out.attempts = reply.attempts
            out.input_tokens, out.output_tokens, out.latency_s = reply.input_tokens, reply.output_tokens, reply.latency_s
            report.gemini_requests += reply.attempts
            out.issues = list(self.verifier(worker.spec, reply))
            repair_content: dict = {}
            if out.issues:
                out.issues = self._try_repair(worker.spec, reply, out, out.issues, repair_content)
            out.verified = not out.issues
            (report.accepted if out.verified else report.rejected).append(out)
            rlog.event("delegation", index=i, worker=pt.worker, verified=out.verified, issues=out.issues,
                       attempts=out.attempts, latency_s=out.latency_s, repair=out.repair,
                       input_tokens=out.input_tokens, output_tokens=out.output_tokens,
                       content={"instruction": pt.instruction, "output": reply.text, **repair_content})

    def _try_repair(self, spec: WorkerSpec, reply: Reply, out: TaskOutcome, issues: List[str],
                    content: dict) -> List[str]:
        """Réparation locale d'un JSON invalide. Retourne les problèmes restants."""
        if self.repairer is None or spec.output_schema is None or reply.truncated or not reply.text.strip():
            return issues  # rien à réparer, ou sortie tronquée : on n'invente pas la suite
        result = self.repairer(reply.text, spec.output_schema)
        if not result.ok:
            return issues + result.reasons
        fixed = replace(reply, text=result.text)
        remaining = list(self.verifier(spec, fixed))  # le code revérifie toujours
        if remaining:
            return issues + [f"réparation non conforme : {'; '.join(remaining)}"]
        out.text = result.text
        out.repair = result.method
        content["repaired_output"] = result.text
        return []

    def _synthesize(self, task: str, context: str, report: RunReport, rlog: RunLogger) -> None:
        results = [WorkerResult(o.worker, o.instruction, o.text) for o in report.accepted]
        try:
            reply = self.organizer.synthesize(task, context, results)
        except _ABORT:
            raise
        except (GeminiError, RetriesExhausted) as exc:
            rlog.event("synthesis_failed", error=f"{type(exc).__name__}: {exc}")
            return
        report.gemini_requests += reply.attempts
        ok = bool(reply.text.strip()) and not reply.truncated
        rlog.event("synthesis", verified=ok, attempts=reply.attempts, latency_s=reply.latency_s,
                   input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                   content={"output": reply.text})
        if ok:
            report.synthesis = reply.text

    @staticmethod
    def _status(report: RunReport) -> str:
        if not report.accepted:
            return "failed"
        return "ok" if not report.rejected else "partial"

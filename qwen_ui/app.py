"""Terminal de l'interface Qwen 5 Go (V1, mode de repli à commandes explicites)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

from config import MissingKeyError
from gemini_client import GeminiDailyQuotaError, GeminiError
from local_llm import LocalLLM, LocalLLMError
from orchestrator import Orchestrator, OrchestrationError, RunReport
from qwen_ui.fiche import FicheProjet, cadrer, load_fiche, save_fiche
from qwen_ui.presenter import render_detail, render_report, summarize
from qwen_ui.router import COMMANDS, Command, parse_command, route
from rate_limiter import DailyQuotaExhausted, RateLimiterError
from review import ReviewError, ReviewPanel, ReviewReport, render_review
from verify.schema import SchemaError
from workers.dedup import DedupResult, collect_ideas, deduplicate, render_dedup
from claude_cli import COST_NOTE, ClaudeCodeError
from privacy.masker import Masker, declared_values, render_preview
from privacy.policy import PolicyError, check_provider
from profiles.loader import Profile, ProfileError
from escalation import (Complexity, EscalationResult, Escalator, build_contract, estimate_complexity,
                        render_contract, render_escalation)

PROVIDERS = {
    "gemini": "Gemini Flash-Lite (niveau gratuit, quota commun)",
    "claude": "Claude via Claude Code (consomme le quota de ton abonnement Pro)",
}
ORGANIZERS = PROVIDERS  # même choix pour l'organisateur et le grand modèle

BANNER = """\
Orchestrateur multi-agents — interface locale (Qwen)
Tapez /aide pour la liste des commandes. Le texte libre est analysé par Qwen,
qui propose une commande : rien n'est envoyé à Gemini sans votre confirmation."""

YES = {"o", "oui", "y", "yes"}


class App:
    def __init__(
        self,
        llm: LocalLLM,
        orchestrator_factory: Callable[[str, Optional[Profile]], Orchestrator],
        *,
        quota_info: Callable[[], Tuple[int, int]],
        state_dir: Path,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        format_llm: Optional[LocalLLM] = None,
        review_factory: Optional[Callable[[Optional[Profile]], ReviewPanel]] = None,
        escalator_factory: Optional[Callable[[str], Escalator]] = None,
        claude_check: Optional[Callable[[], str]] = None,
        claude_calls: Optional[Callable[[], int]] = None,
        big_model: str = "gemini",
        complexity_threshold: int = 5,
        profile: Optional[Profile] = None,
        profile_loader: Optional[Callable[[str], Profile]] = None,
        profile_names: Optional[Callable[[], List[str]]] = None,
        masker_factory: Optional[Callable[[], Masker]] = None,
        role_llms: Optional[Mapping[str, LocalLLM]] = None,
    ) -> None:
        self.llm = llm
        # Agents interchangeables (agents.py) : modèle par rôle ; rôle absent = llm, comme avant.
        self._role_llms = dict(role_llms or {})
        self.format_llm = format_llm  # Qwen 2 Go : déduplication
        self._factory = orchestrator_factory
        self._quota_info = quota_info  # () -> (utilisées, limite)
        self._fiche_path = Path(state_dir) / "fiche_projet.json"
        self._in = input_fn
        self._out = output_fn
        self.organizer = "gemini"
        self.fiche: Optional[FicheProjet] = load_fiche(self._fiche_path)
        self.last_report: Optional[RunReport] = None
        self.last_dedup: Optional[DedupResult] = None
        self.last_review: Optional[ReviewReport] = None
        self._review_factory = review_factory
        self._escalator_factory = escalator_factory
        self._claude_check = claude_check
        self._claude_calls = claude_calls
        self.big_model = big_model if big_model in PROVIDERS else "gemini"
        self.complexity_threshold = complexity_threshold
        self.last_escalation: Optional[EscalationResult] = None
        self._decisions_path = Path(state_dir) / "decisions.jsonl"
        self.refusals: List[dict] = self._load_refusals()
        self._orch: Optional[Orchestrator] = None
        self.profile = profile
        self._profile_loader = profile_loader
        self._profile_names = profile_names
        self._masker_factory = masker_factory
        self._masker: Optional[Masker] = None

    def _llm(self, role: str) -> LocalLLM:
        """Modèle local du rôle (routage, cadrage, interface) : llm sauf affectation configurée."""
        return self._role_llms.get(role, self.llm)

    # ------------------------------------------------------------- boucle

    def loop(self) -> None:
        self._out(BANNER)
        if self.profile:
            self._out(f"\nProfil : {self.profile.name} — {self.profile.description}")
        if self.fiche:
            self._out(f"Fiche projet chargée : {self.fiche.objectif}")
        while True:
            try:
                line = self._in("\n> ")
            except (EOFError, KeyboardInterrupt):
                self._out("\nAu revoir.")
                return
            if not self.handle(line):
                return

    def handle(self, line: str) -> bool:
        """Traite une ligne. Retourne False pour quitter."""
        line = line.strip()
        if not line:
            return True
        try:
            cmd = parse_command(line)
            if cmd is None:
                return self._free_text(line)
            return self._dispatch(cmd)
        except (DailyQuotaExhausted, GeminiDailyQuotaError) as exc:
            self._out(f"Quota Gemini épuisé pour aujourd'hui : {exc}")
        except MissingKeyError as exc:
            self._out(str(exc))
        except LocalLLMError as exc:
            self._out(f"Modèle local indisponible : {exc}")
        except SchemaError as exc:
            self._out(f"Qwen a renvoyé une réponse mal formée ({exc}). Réessayez ou utilisez une commande explicite.")
        except PolicyError as exc:
            self._out(f"Refusé par la politique de données : {exc}")
        except ProfileError as exc:
            self._out(f"Profil : {exc}")
        except ClaudeCodeError as exc:
            self._out(f"Claude indisponible : {exc}")
        except ReviewError as exc:
            self._out(f"Revue impossible : {exc}")
        except OrchestrationError as exc:
            self._out(f"Tâche refusée par le bus : {exc}")
        except (GeminiError, RateLimiterError) as exc:
            self._out(f"Erreur Gemini : {exc}")
        return True

    # ------------------------------------------------------------ routage

    def _free_text(self, line: str) -> bool:
        proposal = route(self._llm("routage"), line, self.fiche.to_context() if self.fiche else "")
        if proposal.command is None:
            self._out(proposal.reply or "(pas de réponse)")
            return True
        self._out(f"Commande proposée : {proposal.command.render()}")
        if not self._confirm("Exécuter cette commande ?"):
            self._out("Annulé.")
            return True
        return self._dispatch(proposal.command)

    def _confirm(self, question: str) -> bool:
        try:
            return self._in(f"{question} (o/n) ").strip().lower() in YES
        except (EOFError, KeyboardInterrupt):
            return False

    def _dispatch(self, cmd: Command) -> bool:
        handler = getattr(self, f"_cmd_{cmd.name}", None)
        if handler is None:
            self._out(f"Commande inconnue : /{cmd.name}. Tapez /aide.")
            return True
        return handler(cmd.argument) is not False

    # ----------------------------------------------------------- commandes

    def _cmd_aide(self, arg: str) -> None:
        width = max(len(n) for n in COMMANDS)
        self._out("Commandes :\n" + "\n".join(f"  /{n.ljust(width)}  {d}" for n, d in COMMANDS.items()))
        self._out("Sans « / », Qwen analyse votre message et propose une commande à confirmer.")

    def _cmd_quitter(self, arg: str) -> bool:
        self._out("Au revoir.")
        return False

    def _cmd_fiche(self, arg: str) -> None:
        if not arg:
            self._out(self.fiche.render() if self.fiche else "Aucune fiche. Utilisez : /fiche <description du projet>")
            return
        if arg.lower() == "effacer":
            self.fiche = None
            self._fiche_path.unlink(missing_ok=True)
            self._out("Fiche effacée.")
            return
        self._out("Cadrage par Qwen (local, 0 requête Gemini)…")
        fiche = cadrer(self._llm("cadrage"), arg)
        self._out(fiche.render())
        if self._confirm("Valider cette fiche ?"):
            self.fiche = fiche
            save_fiche(fiche, self._fiche_path)
            self._out("Fiche enregistrée. Elle sera transmise aux agents avec chaque /tache.")
        else:
            self._out("Fiche non enregistrée. Reformulez la description avec /fiche <description>.")

    def _choose_provider(self, arg: str, role: str, current: str) -> Optional[str]:
        """Choix gemini/claude, avec vérification et confirmation pour Claude."""
        name = arg.strip().lower()
        if not name:
            self._out(f"{role} actuel : {PROVIDERS[current]}")
            return None
        if name not in PROVIDERS:
            self._out(f"Choix inconnu : {name}. Possibles : {', '.join(PROVIDERS)}")
            return None
        if name == "claude":
            if self._claude_check is None:
                self._out("Claude non configuré.")
                return None
            method = self._claude_check()  # lève ClaudeCodeError si absent ou non connecté
            self._out(f"Claude Code connecté ({method}). Ce choix {COST_NOTE}")
            if not self._confirm(f"Utiliser Claude comme {role.lower()} ?"):
                self._out("Annulé.")
                return None
        return name

    def _cmd_organisateur(self, arg: str) -> None:
        name = self._choose_provider(arg, "Organisateur", self.organizer)
        if name is None:
            return
        self.organizer = name
        self._orch = None
        self._out(f"Organisateur : {PROVIDERS[name]}")

    def _cmd_grand(self, arg: str) -> None:
        name = self._choose_provider(arg, "Grand modèle", self.big_model)
        if name is None:
            return
        self.big_model = name
        self._out(f"Grand modèle : {PROVIDERS[name]}")

    def _orchestrator(self) -> Orchestrator:
        if self._orch is None:
            self._orch = self._factory(self.organizer, self.profile)
        return self._orch

    def _check_policy(self, *providers: str) -> None:
        """Profil « données personnelles » : aucun fournisseur externe sans accord de traitement."""
        personal = bool(self.profile and self.profile.personal_data)
        for p in providers:
            check_provider(p, personal_data=personal)

    def _cmd_tache(self, arg: str) -> None:
        if not arg:
            self._out("Usage : /tache <consigne>")
            return
        self._check_policy(self.organizer, "gemini")  # organisateur + workers
        orch = self._orchestrator()
        used, limit = self._quota_info()
        max_requests = orch.max_plan_attempts + orch.delegation_budget + 1  # plans + délégations + synthèse
        if limit - used < max_requests:
            self._out(f"Attention : il reste {limit - used} requêtes Gemini aujourd'hui ; un run peut en utiliser "
                      f"jusqu'à {max_requests} hors retries.")
        if not self.fiche:
            self._out("(Aucune fiche projet : les agents ne recevront que la consigne. /fiche pour cadrer.)")
        self._out(f"Envoi à l'organisateur {self.organizer}… (requêtes espacées de 5 s)")
        report = orch.run(arg, self._context())
        self.last_report = report
        self.last_dedup = None
        self.last_review = None
        used, limit = self._quota_info()
        self._out(render_report(report, budget=orch.delegation_budget, remaining_quota=limit - used))

    def _cmd_detail(self, arg: str) -> None:
        if not self.last_report:
            self._out("Aucun run pour l'instant.")
            return
        try:
            index = int(arg)
        except ValueError:
            self._out("Usage : /detail <numéro>")
            return
        self._out(render_detail(self.last_report, index))

    def _cmd_dedup(self, arg: str) -> None:
        if not self.last_report:
            self._out("Aucun run pour l'instant.")
            return
        ideas = collect_ideas([o.text for o in self.last_report.accepted])
        if not ideas:
            self._out("Le dernier run ne contient pas d'idées au format JSON (worker generateur).")
            return
        if self.format_llm is None:
            self._out("(Qwen 2 Go indisponible : seuls les doublons exacts seront regroupés.)")
        self._out(f"Déduplication locale de {len(ideas)} idées (0 requête Gemini)…")
        self.last_dedup = deduplicate(ideas, self.format_llm)
        self._out(render_dedup(self.last_dedup))

    # ---------------------------------------------------------- revue et décisions

    def _context(self) -> str:
        """Fiche projet + refus de l'utilisateur (remontés à l'organisateur)."""
        parts = [self.fiche.to_context()] if self.fiche else []
        if self.refusals:
            recent = self.refusals[-20:]
            parts.append("Idées déjà refusées par l'utilisateur (ne pas les reproposer telles quelles) :\n" +
                         "\n".join(f"- {r['titre']}" + (f" (raison : {r['raison']})" if r.get("raison") else "")
                                   for r in recent))
        return "\n\n".join(parts)

    def _cmd_revue(self, arg: str) -> None:
        if not self.last_report:
            self._out("Aucun run pour l'instant : lancez d'abord /tache.")
            return
        if self._review_factory is None:
            self._out("Revue non configurée.")
            return
        self._check_policy("gemini")  # avant tout travail, même local
        if self.last_dedup is None:
            self._cmd_dedup("")
            if self.last_dedup is None:
                return
        ideas = self.last_dedup.kept
        panel = self._review_factory(self.profile)
        used, limit = self._quota_info()
        if limit - used < panel.max_requests:
            self._out(f"Quota insuffisant : {limit - used} requêtes restantes, la revue en demande {panel.max_requests}.")
            return
        self._out(f"Revue de {len(ideas)} idées par {len(panel.reviewers)} reviewers "
                  f"({panel.max_requests} requêtes Gemini, espacées de 5 s)…")
        report = panel.review(ideas, self.fiche.to_context() if self.fiche else "")
        self.last_review = report
        used, limit = self._quota_info()
        self._out(render_review(report, remaining_quota=limit - used))

    def _parse_ids(self, arg: str) -> Tuple[List[int], str]:
        m = re.match(r"^\s*([\d\s,;]+)(.*)$", arg)
        if not m:
            return [], ""
        ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
        return list(dict.fromkeys(ids)), m.group(2).strip()

    def _decide(self, arg: str, decision: str) -> None:
        if not self.last_review:
            self._out("Aucune revue pour l'instant : lancez /revue.")
            return
        ids, reason = self._parse_ids(arg)
        known = {v.idea.id: v for v in self.last_review.verdicts}
        unknown = [i for i in ids if i not in known]
        if not ids or unknown:
            self._out(f"Numéros invalides{f' : {unknown}' if unknown else ''}. "
                      f"Usage : /{'valider' if decision == 'validée' else 'refuser'} <numéros> [raison]")
            return
        self._decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._decisions_path, "a", encoding="utf-8") as f:
            for i in ids:
                v = known[i]
                rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "review_id": self.last_review.review_id, "idea_id": i, "titre": v.idea.titre,
                       "decision": decision, "raison": reason, "score": v.weighted_score,
                       "votes": v.approvals, "idea": v.idea.data}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if decision == "refusée":
                    self.refusals.append(rec)
        titles = ", ".join(f"[{i}] {known[i].idea.titre}" for i in ids)
        if decision == "validée":
            self._out(f"Validée(s) : {titles}. Pour faire étudier une idée par le grand modèle : "
                      f"/escalader {ids[0]}")
        else:
            self._out(f"Refusée(s) : {titles}. Ce refus sera transmis à l'organisateur au prochain /tache.")

    def _cmd_valider(self, arg: str) -> None:
        self._decide(arg, "validée")

    def _cmd_refuser(self, arg: str) -> None:
        self._decide(arg, "refusée")

    def _load_refusals(self) -> List[dict]:
        out: List[dict] = []
        if not self._decisions_path.exists():
            return out
        for line in self._decisions_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("decision") == "refusée" and rec.get("titre"):
                out.append(rec)
        return out

    # ------------------------------------------------------------- profils

    def _cmd_profil(self, arg: str) -> None:
        name = arg.strip()
        names = self._profile_names() if self._profile_names else []
        if not name:
            current = self.profile.name if self.profile else "(aucun)"
            self._out(f"Profil actuel : {current}. Profils disponibles : {', '.join(names) or 'aucun'}")
            if self.profile and self.profile.examples:
                self._out("Exemples :\n" + "\n".join(f"  {e}" for e in self.profile.examples))
            return
        if self._profile_loader is None:
            self._out("Profils non configurés.")
            return
        profile = self._profile_loader(name)  # lève ProfileError
        self.profile = profile
        self._orch = None
        self.last_report = self.last_dedup = self.last_review = None
        mode = (" Profil « données personnelles » : aucun fournisseur externe autorisé, masquage local seulement."
                if profile.personal_data else "")
        self._out(f"Profil : {profile.name} — {profile.description}{mode}")

    def _masker_or_none(self) -> Optional[Masker]:
        if self._masker is None and self._masker_factory is not None:
            self._masker = self._masker_factory()
        return self._masker

    def _declared(self) -> List[str]:
        if not self.profile:
            return []
        return declared_values(list(self.profile.records), self.profile.personal_fields)

    def _cmd_masquer(self, arg: str) -> None:
        if not arg.strip():
            self._out("Usage : /masquer <texte> (local : rien n'est envoyé)")
            return
        masker = self._masker_or_none()
        if masker is None:
            self._out("Masquage non configuré.")
            return
        declared = self._declared()
        res = masker.mask(arg, declared=declared)
        self._out(render_preview(res))
        leaks = masker.leak_check(res.text, declared=declared)
        self._out("Contrôle de fuite : " + ("OK" if not leaks else
                  f"BLOQUÉ, {len(leaks)} donnée(s) restante(s) : ce texte ne doit pas être envoyé"))

    def _cmd_donnees(self, arg: str) -> None:
        if not self.profile or not self.profile.records:
            self._out("Le profil actuel n'a pas de données d'exemple.")
            return
        self._out(f"Données fictives du profil, minimisées aux champs utiles ({', '.join(self.profile.useful_fields)}) :")
        masker = self._masker_or_none()
        for r in self.profile.records:
            line = json.dumps(r, ensure_ascii=False)
            if masker is not None and self.profile.personal_data:
                line = masker.mask(line, declared=self._declared()).text
            self._out(f"  {line}")

    def _cmd_resume(self, arg: str) -> None:
        if not self.last_report:
            self._out("Aucun run pour l'instant.")
            return
        self._out(summarize(self._llm("interface"), self.last_report))

    def _cmd_workers(self, arg: str) -> None:
        specs = self._orchestrator().worker_specs
        self._out("Workers déclarés (liste fermée) :\n" + "\n".join(f"  {s.name} — {s.description}" for s in specs))

    def _cmd_quota(self, arg: str) -> None:
        used, limit = self._quota_info()
        self._out(f"Gemini aujourd'hui (heure du Pacifique) : {used}/{limit} requêtes, {max(0, limit - used)} restantes.")
        if self._claude_calls is not None:
            self._out(f"Claude aujourd'hui : {self._claude_calls()} appel(s) via Claude Code "
                      "(limites gérées par ton abonnement Pro).")

    # ------------------------------------------------------------------ escalade

    def _validated(self, review_id: str, idea_id: int) -> bool:
        if not self._decisions_path.exists():
            return False
        last = None
        for line in self._decisions_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("review_id") == review_id and rec.get("idea_id") == idea_id:
                last = rec.get("decision")
        return last == "validée"

    def _cmd_escalader(self, arg: str) -> None:
        parts = arg.split()
        force = "force" in [p.lower() for p in parts[1:]]
        if not self.last_review or not parts or not parts[0].isdigit():
            self._out("Usage : /escalader <numéro> [force] (après /revue et /valider)")
            return
        if self._escalator_factory is None:
            self._out("Escalade non configurée.")
            return
        self._check_policy(self.big_model)
        idea_id = int(parts[0])
        verdict = next((v for v in self.last_review.verdicts if v.idea.id == idea_id), None)
        if verdict is None:
            self._out(f"Idée {idea_id} absente de la dernière revue.")
            return
        if not self._validated(self.last_review.review_id, idea_id):
            self._out(f"L'idée {idea_id} n'est pas validée : /valider {idea_id} d'abord.")
            return
        if not verdict.approved:
            self._out(f"L'idée {idea_id} n'a pas été approuvée par les reviewers "
                      f"({verdict.approvals}/{len(self.last_review.reviews)} votes) : escalade refusée.")
            return

        # 1. Complexité : Qwen estime, le code calcule le score.
        self._out("Estimation de la complexité par Qwen (local)…")
        fiche_text = self.fiche.to_context() if self.fiche else ""
        cx: Complexity = estimate_complexity(self._llm("interface"), verdict.idea, fiche_text)
        self._out(f"Complexité : {cx.fichiers} fichier(s), {cx.dependances} dépendance(s) nouvelle(s), "
                  f"risque {cx.risque} -> score {cx.score} (seuil {self.complexity_threshold}). {cx.justification}")
        if cx.score <= self.complexity_threshold and not force:
            self._out("Pas candidate à l'escalade : complexité sous le seuil. Les workers suffisent "
                      f"(/tache). Pour forcer : /escalader {idea_id} force")
            return

        # 2. Contrat construit par le code, validé puis figé.
        if not self.fiche:
            self._out("Attention : aucune fiche projet, le contrat n'aura ni contraintes ni périmètre.")
        f = self.fiche
        contract = build_contract(verdict.idea, f.contraintes if f else [], f.perimetre if f else [],
                                  f.hors_perimetre if f else [])
        self._out("Contrat d'idée proposé :\n" + render_contract(contract))
        if not self._confirm("Figer ce contrat ?"):
            self._out("Contrat non figé : escalade annulée.")
            return

        # 3. Coût et durée, puis confirmation explicite avant tout appel.
        if self.big_model == "claude":
            cost = f"1 appel à Claude via Claude Code : {COST_NOTE} Durée estimée : 1 à 3 minutes."
        else:
            used, limit = self._quota_info()
            cost = (f"1 requête Gemini Flash-Lite (niveau gratuit, {limit - used} restantes aujourd'hui), "
                    "plus 2 retries au maximum en cas de 429. Durée estimée : 10 à 60 secondes.")
        self._out(f"Grand modèle : {PROVIDERS[self.big_model]}\nCoût : {cost}")
        if not self._confirm("Appeler le grand modèle maintenant ?"):
            self._out("Escalade annulée : aucun appel effectué.")
            return

        # 4-5. Appel, puis contrôles automatiques.
        escalator = self._escalator_factory(self.big_model)
        self._out("Appel du grand modèle en cours…")
        result = escalator.run(verdict.idea, contract, fiche_text)
        self.last_escalation = result
        self._out(render_escalation(result))

"""Calibration des seuils à partir de VOS données réelles (journaux et décisions).

    python calibrate.py                          # dossiers logs/ et state/ de la configuration
    python calibrate.py --logs DIR --state DIR   # autres dossiers
    python calibrate.py --inclure-pilote         # compte aussi les décisions scriptées du pilote

Lit, sans rien modifier :
- state/**/decisions.jsonl : décisions garder/jeter (/valider, /refuser, retours libres),
  avec le score pondéré de l'idée (écrites par qwen_ui/app.py) ;
- state/**/retours.jsonl : retours en langage libre (qwen_ui/langage_libre.py) ; ce sont
  les mêmes décisions que decisions.jsonl, sans note : servent au taux de garde par agent ;
- logs/review-*.jsonl : notes de chaque reviewer (review.py), jointes par review_id et idea_id ;
- logs/*.jsonl, événements « agent_call » (RunLogger, orchestrator.py / agents.py) : routage
  simple/difficile et résultat de chaque appel ;
- logs/escalation-*.json : complexité, SI un champ « complexity_score » y figure (le code
  actuel ne l'écrit pas : COMPLEXITY_THRESHOLD reste alors sans suggestion).

Affiche des suggestions en français et des lignes .env à copier À LA MAIN : ce script
n'écrit jamais dans .env (ni ne le lit). Aucun modèle, aucun réseau. Les titres et raisons
des idées ne sont jamais affichés.

Les fonctions de calcul sont pures (aucune I/O) ; les fonctions load_* lisent les fichiers
en tolérant fichiers absents et lignes corrompues.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MIN_SAMPLE = 20  # en dessous : anecdotique, pas statistique
MIN_PER_GROUP = 5  # en dessous, dans un des deux groupes : séparation fragile
BOOTSTRAP_RUNS = 300
BOOTSTRAP_SEED = 1234  # résultat reproductible d'une exécution à l'autre

# Échecs qui disent quelque chose de la difficulté de la tâche pour l'agent. Les autres
# (indisponible, 429, quota, politique, fuite, aperçu refusé) relèvent de l'infrastructure.
CAPABILITY_FAILURES = frozenset({"timeout", "vide", "hors_schema", "erreur"})
KEPT = frozenset({"validée", "garde"})
REJECTED = frozenset({"refusée", "jette"})

_RE_CHARS = re.compile(r"^tâche (difficile|simple) \((\d+) caractères (>=|<) (\d+)\)")
_RE_SCORE = re.compile(r"^tâche (difficile|simple) \(score (-?\d+) (>=|<) (-?\d+)\)")


# ------------------------------------------------------------------ calcul pur


@dataclass
class Separation:
    """Seuil séparant deux groupes : « positif » si valeur >= seuil."""

    threshold: Optional[float]
    n_pos: int
    n_neg: int
    accuracy: Optional[float] = None  # exactitude équilibrée au seuil choisi (0..1)
    interval: Optional[Tuple[float, float]] = None  # intervalle indicatif (bootstrap 10e-90e centile)
    median_pos: Optional[float] = None
    median_neg: Optional[float] = None
    reason: str = ""  # pourquoi aucun seuil n'est proposé

    @property
    def n(self) -> int:
        return self.n_pos + self.n_neg


def _balanced_accuracy(pos_sorted: Sequence[float], neg_sorted: Sequence[float], t: float) -> float:
    """Listes triées : (part des positifs >= t + part des négatifs < t) / 2."""
    tpr = (len(pos_sorted) - bisect.bisect_left(pos_sorted, t)) / len(pos_sorted)
    tnr = bisect.bisect_left(neg_sorted, t) / len(neg_sorted)
    return (tpr + tnr) / 2


def best_threshold(pos: Sequence[float], neg: Sequence[float]) -> Tuple[float, float]:
    """Seuil maximisant l'exactitude équilibrée (positif si valeur >= seuil).

    Candidats : milieux entre valeurs observées consécutives. En cas d'égalité, le milieu
    du plateau des meilleurs candidats (choix neutre, ni le plus bas ni le plus haut).
    Les deux groupes doivent être non vides.
    """
    if not pos or not neg:
        raise ValueError("deux groupes non vides sont nécessaires")
    values = sorted(set(pos) | set(neg))
    candidates = [(a + b) / 2 for a, b in zip(values, values[1:])] or [values[0]]
    ps, ns = sorted(pos), sorted(neg)
    scored = [(_balanced_accuracy(ps, ns, t), t) for t in candidates]
    best = max(acc for acc, _ in scored)
    ties = [t for acc, t in scored if abs(acc - best) < 1e-12]
    return (min(ties) + max(ties)) / 2, best


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    k = (len(sorted_values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def bootstrap_interval(pos: Sequence[float], neg: Sequence[float], *, runs: int = BOOTSTRAP_RUNS,
                       seed: int = BOOTSTRAP_SEED) -> Tuple[float, float]:
    """Intervalle indicatif du seuil (10e-90e centile sur rééchantillonnages)."""
    rng = random.Random(seed)
    found = []
    for _ in range(runs):
        p = [rng.choice(pos) for _ in pos]
        q = [rng.choice(neg) for _ in neg]
        found.append(best_threshold(p, q)[0])
    found.sort()
    return _percentile(found, 0.10), _percentile(found, 0.90)


def separate(pos: Sequence[float], neg: Sequence[float], *, pos_label: str = "positifs",
             neg_label: str = "négatifs") -> Separation:
    """Fonction pure : seuil, exactitude, intervalle et médianes, ou la raison de l'absence."""
    pos = [float(x) for x in pos]
    neg = [float(x) for x in neg]
    sep = Separation(None, len(pos), len(neg),
                     median_pos=statistics.median(pos) if pos else None,
                     median_neg=statistics.median(neg) if neg else None)
    if not pos or not neg:
        missing = pos_label if not pos else neg_label
        sep.reason = f"aucune donnée dans le groupe « {missing} » : impossible de séparer deux groupes"
        return sep
    sep.threshold, sep.accuracy = best_threshold(pos, neg)
    sep.interval = bootstrap_interval(pos, neg)
    return sep


def kth_highest(scores: Iterable[float], k: int) -> Optional[float]:
    """k-ième meilleure note : une idée est approuvée ssi cette note >= REVIEW_THRESHOLD
    (vote k/n de review.compute_verdicts). None si moins de k notes."""
    s = sorted((float(x) for x in scores), reverse=True)
    return s[k - 1] if k >= 1 and len(s) >= k else None


@dataclass(frozen=True)
class Decision:
    review_id: str
    idea_id: int
    kept: bool
    weighted_score: Optional[float]
    pilot: bool = False


def parse_decision(rec: object, *, pilot: bool = False) -> Optional[Decision]:
    """Une ligne de decisions.jsonl -> Decision, ou None si la ligne est inexploitable."""
    if not isinstance(rec, dict):
        return None
    d = rec.get("decision")
    if d not in KEPT and d not in REJECTED:
        return None
    rid, iid = rec.get("review_id"), rec.get("idea_id")
    if not isinstance(rid, str) or not isinstance(iid, int) or isinstance(iid, bool):
        return None
    score = rec.get("score")
    score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    return Decision(rid, iid, d in KEPT, score, pilot)


def dedupe_decisions(decisions: Iterable[Decision]) -> List[Decision]:
    """Une décision par (revue, idée) : la dernière l'emporte (l'utilisateur a changé d'avis)."""
    last: Dict[Tuple[str, int], Decision] = {}
    for d in decisions:
        last.pop((d.review_id, d.idea_id), None)
        last[(d.review_id, d.idea_id)] = d
    return list(last.values())


def parse_review_scores(records: Iterable[object]) -> Dict[int, List[float]]:
    """Événements d'un journal review-*.jsonl -> {idea_id: [note de chaque reviewer valide]}."""
    out: Dict[int, List[float]] = {}
    for rec in records:
        if not isinstance(rec, dict) or rec.get("event") != "review" or rec.get("valid") is not True:
            continue
        evals = rec.get("evaluations")
        if not isinstance(evals, list):
            continue
        for e in evals:
            if not isinstance(e, dict):
                continue
            iid, sc = e.get("idea_id"), e.get("score")
            if isinstance(iid, int) and isinstance(sc, (int, float)) and not isinstance(sc, bool):
                out.setdefault(iid, []).append(float(sc))
    return out


@dataclass
class ReviewCalibration:
    separation: Separation
    source: str  # « notes des reviewers (k-ième meilleure) » ou « score pondéré »
    n_decisions: int
    n_without_score: int
    n_pilot_excluded: int


def calibrate_review(decisions: Sequence[Decision], review_scores: Dict[str, Dict[int, List[float]]], *,
                     votes_needed: int = 2, include_pilot: bool = False) -> ReviewCalibration:
    """Fonction pure. Pour chaque décision, la valeur testée est la k-ième meilleure note des
    reviewers (exactement ce que compare REVIEW_THRESHOLD) si le journal de revue est
    disponible, sinon le score pondéré de decisions.jsonl (approximation)."""
    excluded = sum(1 for d in decisions if d.pilot) if not include_pilot else 0
    used = [d for d in decisions if include_pilot or not d.pilot]
    kept, rejected = [], []
    from_reviewers = without = 0
    for d in used:
        value = kth_highest(review_scores.get(d.review_id, {}).get(d.idea_id, ()), votes_needed)
        if value is not None:
            from_reviewers += 1
        else:
            value = d.weighted_score
        if value is None:
            without += 1
            continue
        (kept if d.kept else rejected).append(value)
    if from_reviewers == 0:
        source = "score pondéré de decisions.jsonl (approximation : journaux de revue absents)"
    elif from_reviewers == len(kept) + len(rejected):
        source = f"{votes_needed}e meilleure note des reviewers (journaux de revue)"
    else:
        source = (f"{votes_needed}e meilleure note des reviewers pour {from_reviewers} idée(s), "
                  "score pondéré pour les autres (approximation)")
    sep = separate(kept, rejected, pos_label="gardées", neg_label="jetées")
    return ReviewCalibration(sep, source, len(used), without, excluded)


@dataclass(frozen=True)
class AgentCall:
    role: str
    criterion: str  # "caracteres" | "score"
    value: int
    routed_hard: bool
    current_threshold: int
    result: str

    @property
    def counted(self) -> bool:
        return self.result == "ok" or self.result in CAPABILITY_FAILURES

    @property
    def failed(self) -> bool:
        return self.result in CAPABILITY_FAILURES


def parse_agent_call(rec: object) -> Optional[AgentCall]:
    """Événement « agent_call » d'un premier essai routé par le critère simple/difficile."""
    if not isinstance(rec, dict) or rec.get("event") != "agent_call" or rec.get("attempt") != 1:
        return None
    reason, result = rec.get("reason"), rec.get("result")
    if not isinstance(reason, str) or not isinstance(result, str):
        return None
    for criterion, rx in (("caracteres", _RE_CHARS), ("score", _RE_SCORE)):
        m = rx.match(reason)
        if m:
            return AgentCall(str(rec.get("role") or "?"), criterion, int(m.group(2)),
                             m.group(1) == "difficile", int(m.group(4)), result)
    return None


@dataclass
class RoutingCalibration:
    criterion: str
    n_calls: int  # premiers essais exploitables (ok ou échec de capacité)
    n_simple: int
    n_simple_failed: int
    n_hard: int
    n_hard_failed: int
    current: Optional[int]
    separation: Optional[Separation]
    max_simple_ok: Optional[int]
    roles: Dict[str, int] = field(default_factory=dict)
    note: str = ""


def calibrate_routing(calls: Sequence[AgentCall], criterion: str) -> RoutingCalibration:
    """Fonction pure. Seuls les appels routés vers l'agent SIMPLE informent sur sa limite :
    échecs (timeout, vide, hors schéma, erreur) vs succès selon la longueur (ou le score).
    Les appels routés vers l'agent difficile n'ont jamais testé l'agent simple."""
    rel = [c for c in calls if c.criterion == criterion and c.counted]
    simple = [c for c in rel if not c.routed_hard]
    hard = [c for c in rel if c.routed_hard]
    currents = [c.current_threshold for c in rel]
    current = statistics.mode(currents) if currents else None
    roles: Dict[str, int] = {}
    for c in rel:
        roles[c.role] = roles.get(c.role, 0) + 1
    ok = [c.value for c in simple if not c.failed]
    ko = [c.value for c in simple if c.failed]
    cal = RoutingCalibration(criterion, len(rel), len(simple), len(ko), len(hard),
                             sum(1 for c in hard if c.failed), current, None,
                             max(ok) if ok else None, roles)
    if not rel:
        cal.note = ("aucun appel routé par ce critère (aucun agent « difficile » configuré, "
                    "ou journaux absents)")
    elif not simple:
        cal.note = "aucun appel routé vers l'agent simple : rien ne mesure sa limite"
    elif not ko:
        cal.note = (f"aucun échec de l'agent simple (jusqu'à {cal.max_simple_ok} observé) : "
                    "pas de raison de baisser le seuil ; le relever n'est pas démontré, car les "
                    "tâches au-dessus du seuil n'ont jamais été confiées à l'agent simple")
    elif not ok:
        cal.note = "l'agent simple échoue sur tous les appels : seuil non calculable, revoir l'agent"
    else:
        cal.separation = separate(ko, ok, pos_label="échecs de l'agent simple", neg_label="succès")
    return cal


def parse_complexity(rec: object) -> Optional[Tuple[float, bool]]:
    """Enregistrement d'escalade -> (score de complexité, escalade acceptée), si le score y figure."""
    if not isinstance(rec, dict):
        return None
    sc, acc = rec.get("complexity_score"), rec.get("accepted")
    if isinstance(sc, (int, float)) and not isinstance(sc, bool) and isinstance(acc, bool):
        return float(sc), acc
    return None


def calibrate_complexity(pairs: Sequence[Tuple[float, bool]]) -> Separation:
    """Fonction pure : sépare les escalades utiles (acceptées) des autres par le score."""
    sep = separate([s for s, a in pairs if a], [s for s, a in pairs if not a],
                   pos_label="escalades acceptées", neg_label="escalades rejetées")
    if not pairs:
        sep.reason = ("le score de complexité n'est pas journalisé par le code actuel "
                      "(escalation.py n'écrit pas « complexity_score ») : aucune donnée")
    return sep


# -------------------------------------------------------------------- rendu (pur)


def _num(x: Optional[float], digits: int = 1) -> str:
    return "-" if x is None else f"{x:.{digits}f}".replace(".", ",")


def sample_warning(n: int, sep: Optional[Separation] = None) -> List[str]:
    out = []
    if n < MIN_SAMPLE:
        out.append(f"AVERTISSEMENT : {n} observation(s) seulement (< {MIN_SAMPLE}) : résultat "
                   "anecdotique, pas statistique. Ne changez pas le seuil sur cette seule base.")
    elif sep is not None and min(sep.n_pos, sep.n_neg) < MIN_PER_GROUP:
        out.append(f"AVERTISSEMENT : un des deux groupes a moins de {MIN_PER_GROUP} observations : "
                   "séparation fragile.")
    return out


def _round_review(t: float) -> float:
    return min(10.0, max(0.0, round(t, 1)))


def _round_chars(t: float) -> int:
    return max(100, int(round(t / 100.0)) * 100)


def _int_up(t: float) -> int:
    """Seuil entier « valeur >= seuil » équivalent à un seuil réel t."""
    return int(math.ceil(t - 1e-9))


@dataclass
class Report:
    text: str
    env_lines: List[str]


def render_report(review: ReviewCalibration, complexity: Separation,
                  routing: Sequence[RoutingCalibration], *, defaults: Dict[str, float],
                  n_feedback: int = 0, feedback_by_agent: Optional[Dict[str, Tuple[int, int]]] = None,
                  sources: Sequence[str] = ()) -> Report:
    """Fonction pure : texte français et lignes .env suggérées."""
    L: List[str] = ["=== Calibration des seuils (données locales, aucun envoi) ==="]
    if sources:
        L.append("Fichiers lus : " + "; ".join(sources))
    env: List[str] = []

    # REVIEW_THRESHOLD
    s = review.separation
    L += ["", f"--- REVIEW_THRESHOLD (défaut {_num(defaults['REVIEW_THRESHOLD'])}) ---",
          f"Décisions exploitées : {s.n} ({s.n_pos} gardée(s), {s.n_neg} jetée(s))"
          + (f", {review.n_without_score} sans note ignorée(s)" if review.n_without_score else "")
          + (f", {review.n_pilot_excluded} décision(s) du pilote exclue(s) (--inclure-pilote)"
             if review.n_pilot_excluded else "") + ".",
          f"Valeur comparée : {review.source}."]
    if s.median_pos is not None or s.median_neg is not None:
        L.append(f"Médianes : gardées {_num(s.median_pos)}, jetées {_num(s.median_neg)}.")
    L += sample_warning(s.n, s)
    if s.threshold is None:
        L.append(f"Pas de suggestion : {s.reason}.")
        env.append("# REVIEW_THRESHOLD : pas de suggestion (données insuffisantes)")
    else:
        t = _round_review(s.threshold)
        lo, hi = s.interval
        L.append(f"Suggestion : {_num(t)} (sépare {round(s.accuracy * 100)} % des décisions, exactitude "
                 f"équilibrée) ; intervalle indicatif {_num(lo)} à {_num(hi)}.")
        if s.median_pos is not None and s.median_neg is not None and s.median_pos <= s.median_neg:
            L.append("Attention : les idées gardées ne sont pas mieux notées que les jetées ; "
                     "les reviewers ne prédisent pas vos choix, un seuil n'y changera rien.")
        env.append(f"REVIEW_THRESHOLD={t}")
    if n_feedback:
        L.append(f"Retours en langage libre (retours.jsonl) : {n_feedback} (déjà comptés dans decisions.jsonl).")
        for agent, (k, n) in sorted((feedback_by_agent or {}).items()):
            L.append(f"  agent producteur {agent} : {k}/{n} idée(s) gardée(s)")

    # COMPLEXITY_THRESHOLD
    c = complexity
    L += ["", f"--- COMPLEXITY_THRESHOLD (défaut {int(defaults['COMPLEXITY_THRESHOLD'])}) ---"]
    if c.threshold is None:
        L.append(f"Pas de suggestion : {c.reason}.")
        env.append("# COMPLEXITY_THRESHOLD : pas de suggestion (données insuffisantes)")
    else:
        L.append(f"Escalades exploitées : {c.n} ({c.n_pos} acceptée(s), {c.n_neg} rejetée(s)).")
        L += sample_warning(c.n, c)
        # Escalade si score > seuil : le seuil est la valeur juste sous la coupure.
        t = _int_up(c.threshold) - 1
        lo, hi = c.interval
        L.append(f"Suggestion : {t} (escalade si score > {t}) ; intervalle indicatif "
                 f"{_int_up(lo) - 1} à {_int_up(hi) - 1}.")
        env.append(f"COMPLEXITY_THRESHOLD={t}")

    # AGENT_SEUIL_*
    names = {"caracteres": ("AGENT_SEUIL_CARACTERES", "caractères"), "score": ("AGENT_SEUIL_SCORE", "score")}
    for r in routing:
        var, unit = names[r.criterion]
        L += ["", f"--- {var} (défaut {int(defaults[var])}"
              + (f", observé dans les journaux : {r.current}" if r.current is not None else "") + ") ---",
              f"Premiers appels exploitables : {r.n_calls} (agent simple : {r.n_simple}, dont "
              f"{r.n_simple_failed} échec(s) ; agent difficile : {r.n_hard}, dont {r.n_hard_failed} échec(s))."]
        if r.roles:
            L.append("Par rôle : " + ", ".join(f"{k} {v}" for k, v in sorted(r.roles.items())) + ".")
        L += sample_warning(r.n_calls, r.separation)
        if r.separation is None or r.separation.threshold is None:
            L.append(f"Pas de suggestion : {r.note or (r.separation.reason if r.separation else '')}.")
            env.append(f"# {var} : pas de suggestion")
            continue
        sp = r.separation
        t = _round_chars(sp.threshold) if r.criterion == "caracteres" else _int_up(sp.threshold)
        lo, hi = sp.interval
        fmt = _round_chars if r.criterion == "caracteres" else _int_up
        L.append(f"Échecs de l'agent simple : médiane {_num(sp.median_pos, 0)} {unit} ; succès : médiane "
                 f"{_num(sp.median_neg, 0)} {unit}.")
        L.append(f"Suggestion : {t} (au-delà, confier à l'agent difficile) ; intervalle indicatif "
                 f"{fmt(lo)} à {fmt(hi)}.")
        env.append(f"{var}={t}")

    L += ["", "Lignes à copier dans .env (À LA MAIN, ce script n'écrit jamais dans .env) :"]
    L += [f"  {e}" for e in env]
    L += ["Un profil peut surcharger le seuil de revue (revue.seuil dans profil.json) : décisions "
          "de tous les profils confondues ici. Changez une variable à la fois, puis recalibrez."]
    return Report("\n".join(L), env)


# ------------------------------------------------------------------------ I/O


def read_jsonl(path: Path) -> List[object]:
    """Lignes JSON d'un fichier ; fichier absent ou illisible = [], ligne corrompue ignorée."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _files(folder: Optional[Path], pattern: str, recursive: bool = False) -> List[Path]:
    if folder is None or not Path(folder).is_dir():
        return []
    try:
        found = Path(folder).rglob(pattern) if recursive else Path(folder).glob(pattern)
        return sorted(p for p in found if p.is_file())
    except OSError:
        return []


def _is_pilot(path: Path, state_dir: Path) -> bool:
    try:
        return "pilot" in path.relative_to(state_dir).parts[:-1]
    except ValueError:
        return False


def load_decisions(state_dir: Path) -> Tuple[List[Decision], List[Path]]:
    files = _files(state_dir, "decisions.jsonl", recursive=True)
    out: List[Decision] = []
    for p in files:
        pilot = _is_pilot(p, Path(state_dir))
        out.extend(d for d in (parse_decision(r, pilot=pilot) for r in read_jsonl(p)) if d)
    return dedupe_decisions(out), files


def load_feedback(state_dir: Path, *, include_pilot: bool = False
                  ) -> Tuple[int, Dict[str, Tuple[int, int]], List[Path]]:
    files = [p for p in _files(state_dir, "retours.jsonl", recursive=True)
             if include_pilot or not _is_pilot(p, Path(state_dir))]
    n, by_agent = 0, {}
    for p in files:
        for r in read_jsonl(p):
            if not isinstance(r, dict) or r.get("decision") not in ("garde", "jette"):
                continue
            n += 1
            agent = str(r.get("agent") or "?")
            k, t = by_agent.get(agent, (0, 0))
            by_agent[agent] = (k + (r["decision"] == "garde"), t + 1)
    return n, by_agent, files


_RE_REVIEW_FILE = re.compile(r"^review-\d{8}-\d{6}-([0-9a-f]+)\.jsonl$")


def load_review_scores(logs_dir: Path) -> Tuple[Dict[str, Dict[int, List[float]]], List[Path]]:
    out: Dict[str, Dict[int, List[float]]] = {}
    files = []
    for p in _files(logs_dir, "review-*.jsonl"):
        m = _RE_REVIEW_FILE.match(p.name)
        if m:
            files.append(p)
            out[m.group(1)] = parse_review_scores(read_jsonl(p))
    return out, files


def load_agent_calls(logs_dir: Path) -> Tuple[List[AgentCall], List[Path]]:
    calls, used = [], []
    for p in _files(logs_dir, "*.jsonl"):
        if p.name.startswith("review-"):
            continue
        found = [c for c in (parse_agent_call(r) for r in read_jsonl(p)) if c]
        if found:
            used.append(p)
            calls.extend(found)
    return calls, used


def load_complexity(logs_dir: Path) -> Tuple[List[Tuple[float, bool]], List[Path]]:
    pairs, files = [], _files(logs_dir, "escalation-*.json")
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        pair = parse_complexity(rec)
        if pair:
            pairs.append(pair)
    return pairs, files


def run(logs_dir: Path, state_dir: Path, *, votes_needed: int = 2, include_pilot: bool = False) -> Report:
    import config  # valeurs par défaut du projet ; .env n'est ni lu ni écrit
    d = config.Settings()
    defaults = {"REVIEW_THRESHOLD": d.review_threshold, "COMPLEXITY_THRESHOLD": d.complexity_threshold,
                "AGENT_SEUIL_CARACTERES": d.agent_seuil_caracteres, "AGENT_SEUIL_SCORE": d.agent_seuil_score}
    decisions, dec_files = load_decisions(state_dir)
    n_fb, fb_by_agent, fb_files = load_feedback(state_dir, include_pilot=include_pilot)
    scores, rev_files = load_review_scores(logs_dir)
    calls, call_files = load_agent_calls(logs_dir)
    pairs, esc_files = load_complexity(logs_dir)
    sources = [f"{len(dec_files)} decisions.jsonl", f"{len(fb_files)} retours.jsonl",
               f"{len(rev_files)} journal(aux) de revue", f"{len(call_files)} journal(aux) d'appels d'agents",
               f"{len(esc_files)} escalade(s)"]
    return render_report(
        calibrate_review(decisions, scores, votes_needed=votes_needed, include_pilot=include_pilot),
        calibrate_complexity(pairs),
        [calibrate_routing(calls, "caracteres"), calibrate_routing(calls, "score")],
        defaults=defaults, n_feedback=n_fb, feedback_by_agent=fb_by_agent, sources=sources)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import config
    s = config.load_settings()  # dossiers (ORCH_LOGS_DIR / ORCH_STATE_DIR si définis), sans lire .env
    ap = argparse.ArgumentParser(description="Suggère des seuils à partir de vos journaux et décisions.")
    ap.add_argument("--logs", type=Path, default=s.logs_dir, help=f"dossier des journaux (défaut : {s.logs_dir})")
    ap.add_argument("--state", type=Path, default=s.state_dir, help=f"dossier d'état (défaut : {s.state_dir})")
    ap.add_argument("--votes", type=int, default=s.review_votes_needed,
                    help="votes nécessaires à l'approbation (REVIEW_VOTES_NEEDED, défaut 2)")
    ap.add_argument("--inclure-pilote", action="store_true",
                    help="compter les décisions scriptées du pilote (state/pilot/)")
    args = ap.parse_args(argv)
    if args.votes < 1:
        ap.error("--votes doit être >= 1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    report = run(args.logs, args.state, votes_needed=args.votes, include_pilot=args.inclure_pilote)
    print(report.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

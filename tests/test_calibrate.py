"""calibrate.py : calcul pur des seuils et lecture tolérante de journaux fictifs."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import calibrate as cal
from calibrate import (
    AgentCall,
    Decision,
    best_threshold,
    calibrate_complexity,
    calibrate_review,
    calibrate_routing,
    dedupe_decisions,
    kth_highest,
    parse_agent_call,
    parse_decision,
    parse_review_scores,
    read_jsonl,
    separate,
)

DEFAULTS = {"REVIEW_THRESHOLD": 6.5, "COMPLEXITY_THRESHOLD": 5, "AGENT_SEUIL_CARACTERES": 6000,
            "AGENT_SEUIL_SCORE": 5}


def write_jsonl(path: Path, records, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for line in extra_lines:
            f.write(line + "\n")


def decision_rec(rid, iid, decision, score):
    # Même forme que qwen_ui/app.py _apply_decisions (titre fictif).
    return {"ts": "2026-01-01T00:00:00+00:00", "review_id": rid, "idea_id": iid, "titre": f"Idée fictive {iid}",
            "decision": decision, "raison": "", "score": score, "votes": 2, "idea": {}, "perso": False}


def agent_event(reason, result="ok", attempt=1, role="worker"):
    # Même forme que agents.AgentRouter._log via RunLogger.event("agent_call", ...).
    return {"ts": "2026-01-01T00:00:00.000+00:00", "event": "agent_call", "role": role, "agent": "a",
            "provider": "ollama", "model": "m", "external": False, "duration_s": 1.0, "result": result,
            "reason": reason, "attempt": attempt}


class SeparationTest(unittest.TestCase):
    def test_separation_parfaite(self):
        t, acc = best_threshold([7, 8, 9], [3, 4, 5])
        self.assertEqual(acc, 1.0)
        self.assertTrue(5 < t < 7)  # milieu du plateau : 6.0
        self.assertAlmostEqual(t, 6.0)

    def test_groupes_vides(self):
        with self.assertRaises(ValueError):
            best_threshold([], [1])
        sep = separate([], [1, 2], pos_label="gardées", neg_label="jetées")
        self.assertIsNone(sep.threshold)
        self.assertIn("gardées", sep.reason)

    def test_intervalle_reproductible_et_encadre(self):
        pos = [6.5 + (i % 7) * 0.4 for i in range(30)]
        neg = [3.0 + (i % 9) * 0.45 for i in range(30)]
        a, b = separate(pos, neg), separate(pos, neg)
        self.assertEqual(a.interval, b.interval)
        lo, hi = a.interval
        self.assertLessEqual(lo, hi)
        self.assertEqual(a.n, 60)

    def test_kth_highest(self):
        self.assertEqual(kth_highest([5, 9, 7], 2), 7)
        self.assertIsNone(kth_highest([5], 2))
        self.assertIsNone(kth_highest([], 1))


class ParseTest(unittest.TestCase):
    def test_decision_tolerante(self):
        self.assertIsNone(parse_decision("texte"))
        self.assertIsNone(parse_decision({"decision": "validée", "review_id": "ab", "idea_id": "1"}))
        self.assertIsNone(parse_decision({"decision": "peut-être", "review_id": "ab", "idea_id": 1}))
        d = parse_decision(decision_rec("ab", 1, "refusée", None))
        self.assertFalse(d.kept)
        self.assertIsNone(d.weighted_score)
        self.assertTrue(parse_decision(decision_rec("ab", 2, "garde", 7)).kept)

    def test_derniere_decision_l_emporte(self):
        ds = [Decision("r", 1, True, 7.0), Decision("r", 1, False, 7.0), Decision("r", 2, True, 8.0)]
        out = dedupe_decisions(ds)
        self.assertEqual(len(out), 2)
        self.assertFalse(next(d for d in out if d.idea_id == 1).kept)

    def test_scores_de_revue(self):
        recs = [{"event": "review", "reviewer": "a", "valid": True,
                 "evaluations": [{"idea_id": 1, "score": 8, "justification": "j", "objection": ""}]},
                {"event": "review", "reviewer": "b", "valid": False, "evaluations": [{"idea_id": 1, "score": 1}]},
                {"event": "review", "reviewer": "c", "valid": True, "evaluations": "corrompu"},
                {"event": "review_end", "status": "ok"}, 42]
        self.assertEqual(parse_review_scores(recs), {1: [8.0]})

    def test_appel_agent(self):
        c = parse_agent_call(agent_event("tâche difficile (7000 caractères >= 6000) -> b (configuration)", "timeout"))
        self.assertEqual((c.criterion, c.value, c.routed_hard, c.current_threshold), ("caracteres", 7000, True, 6000))
        self.assertTrue(c.failed)
        s = parse_agent_call(agent_event("tâche simple (score 3 < 5) -> a (défaut)"))
        self.assertEqual((s.criterion, s.value, s.routed_hard), ("score", 3, False))
        # Repli, rôle sans agent difficile, autre événement : ignorés.
        self.assertIsNone(parse_agent_call(agent_event("repli après a (timeout)", attempt=2)))
        self.assertIsNone(parse_agent_call(agent_event("rôle worker -> a (défaut)")))
        self.assertIsNone(parse_agent_call({"event": "plan", "attempt": 1}))

    def test_echec_d_infrastructure_non_compte(self):
        c = parse_agent_call(agent_event("tâche simple (100 caractères < 6000) -> a (défaut)", "http_429"))
        self.assertFalse(c.counted)

    def test_lecture_jsonl_tolerante(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jsonl"
            self.assertEqual(read_jsonl(p), [])  # absent
            p.write_text('{"a": 1}\n{corrompu\n\n[1, 2]\n', encoding="utf-8")
            self.assertEqual(read_jsonl(p), [{"a": 1}, [1, 2]])


class CalibrateReviewTest(unittest.TestCase):
    def test_notes_des_reviewers_prioritaires(self):
        decisions = [Decision("r1", 1, True, 5.0), Decision("r1", 2, False, 5.0)]
        scores = {"r1": {1: [9, 8, 2], 2: [9, 3, 2]}}  # 2e meilleure : 8 vs 3
        rc = calibrate_review(decisions, scores, votes_needed=2)
        self.assertIn("reviewers", rc.source)
        self.assertTrue(3 < rc.separation.threshold < 8)

    def test_repli_score_pondere_et_sans_note(self):
        decisions = [Decision("r", 1, True, 8.0), Decision("r", 2, False, 4.0), Decision("r", 3, False, None)]
        rc = calibrate_review(decisions, {})
        self.assertIn("pondéré", rc.source)
        self.assertEqual(rc.n_without_score, 1)
        self.assertAlmostEqual(rc.separation.threshold, 6.0)

    def test_pilote_exclu_par_defaut(self):
        decisions = [Decision("r", 1, True, 8.0, pilot=True), Decision("r", 2, False, 4.0)]
        rc = calibrate_review(decisions, {})
        self.assertEqual(rc.n_pilot_excluded, 1)
        self.assertIsNone(rc.separation.threshold)
        self.assertIsNotNone(calibrate_review(decisions, {}, include_pilot=True).separation.threshold)


class CalibrateRoutingTest(unittest.TestCase):
    def call(self, value, hard, result, crit="caracteres", role="worker"):
        return AgentCall(role, crit, value, hard, 6000, result)

    def test_echecs_au_dela_d_une_longueur(self):
        calls = ([self.call(v, False, "ok") for v in (500, 1000, 2000, 3000)]
                 + [self.call(v, False, "timeout") for v in (4500, 5000, 5500)]
                 + [self.call(9000, True, "ok"), self.call(8000, False, "http_429")])
        r = calibrate_routing(calls, "caracteres")
        self.assertEqual((r.n_calls, r.n_simple, r.n_simple_failed, r.n_hard), (8, 7, 3, 1))
        self.assertTrue(3000 < r.separation.threshold < 4500)
        self.assertEqual(r.current, 6000)

    def test_aucun_echec_pas_de_suggestion(self):
        r = calibrate_routing([self.call(v, False, "ok") for v in (100, 200)], "caracteres")
        self.assertIsNone(r.separation)
        self.assertIn("aucun échec", r.note)

    def test_aucun_appel(self):
        r = calibrate_routing([], "score")
        self.assertIsNone(r.separation)
        self.assertIn("aucun appel", r.note)


class ComplexityTest(unittest.TestCase):
    def test_pas_de_donnees(self):
        sep = calibrate_complexity([])
        self.assertIsNone(sep.threshold)
        self.assertIn("complexity_score", sep.reason)

    def test_avec_donnees(self):
        sep = calibrate_complexity([(3, False), (4, False), (8, True), (10, True)])
        self.assertTrue(4 < sep.threshold < 8)


class ReportTest(unittest.TestCase):
    def report(self, decisions):
        rc = calibrate_review(decisions, {})
        return cal.render_report(rc, calibrate_complexity([]),
                                 [calibrate_routing([], "caracteres"), calibrate_routing([], "score")],
                                 defaults=DEFAULTS)

    def test_avertissement_petit_echantillon(self):
        rep = self.report([Decision("r", i, i % 2 == 0, 8.0 if i % 2 == 0 else 4.0) for i in range(10)])
        self.assertIn("anecdotique, pas statistique", rep.text)
        self.assertIn("REVIEW_THRESHOLD=6.0", rep.env_lines)
        self.assertIn("# COMPLEXITY_THRESHOLD : pas de suggestion (données insuffisantes)", rep.env_lines)

    def test_pas_d_avertissement_grand_echantillon_equilibre(self):
        ds = [Decision("r", i, True, 7.0 + (i % 5) * 0.5) for i in range(15)]
        ds += [Decision("r", 100 + i, False, 3.0 + (i % 5) * 0.5) for i in range(15)]
        rep = self.report(ds)
        review_part = rep.text.split("--- COMPLEXITY")[0]
        self.assertNotIn("AVERTISSEMENT", review_part)
        self.assertIn("intervalle indicatif", review_part)

    def test_alerte_quand_les_notes_ne_predisent_pas(self):
        rep = self.report([Decision("r", 1, True, 3.0), Decision("r", 2, False, 8.0)])
        self.assertIn("ne prédisent pas", rep.text)


class EndToEndTest(unittest.TestCase):
    """Dossiers fictifs complets, formats identiques à ceux écrits par le code du projet."""

    def build(self, root: Path):
        logs, state = root / "logs", root / "state"
        decisions, reviews = [], {"aa11bb22": [], "cc33dd44": []}
        for n, rid in enumerate(reviews):
            for i in range(1, 13):
                kept = i <= 6
                notes = [7.5 + i % 3 * 0.5, 7.0 + i % 2, 6.0] if kept else [6.5, 4.0 + i % 3 * 0.5, 3.0]
                reviews[rid].append({"idea_id": i, "score": notes})
                decisions.append(decision_rec(rid, i, "validée" if kept else "refusée", round(sum(notes) / 3, 2)))
        write_jsonl(state / "decisions.jsonl", decisions, extra_lines=["{ligne corrompue", "", "null"])
        write_jsonl(state / "pilot" / "decisions.jsonl", [decision_rec("ee55ff66", 1, "validée", 1.0)])
        write_jsonl(state / "retours.jsonl", [
            {"date": "2026-01-01T00:00:00+00:00", "profil": "brainstorm", "perso": False, "role": "worker",
             "agent": "gemini", "decision": "garde", "idee": "x", "raison": ""},
            {"date": "2026-01-01T00:00:00+00:00", "profil": "brainstorm", "perso": False, "role": "worker",
             "agent": "gemini", "decision": "jette", "idee": "y", "raison": ""}], extra_lines=["pas du json"])
        for rid, ideas in reviews.items():
            recs = [{"ts": "t", "event": "review", "reviewer": name, "valid": True, "error": None, "missing": [],
                     "repaired": None, "attempts": 1, "raw": "{}",
                     "evaluations": [{"idea_id": d["idea_id"], "score": d["score"][k], "justification": "j",
                                      "objection": ""} for d in ideas]}
                    for k, name in enumerate(("pragmatique", "utilisateur", "critique"))]
            recs.append({"ts": "t", "event": "review_end", "status": "ok"})
            write_jsonl(logs / f"review-20260101-120000-{rid}.jsonl", recs)
        calls = [agent_event(f"tâche simple ({n} caractères < 6000) -> a (défaut)") for n in range(500, 3500, 250)]
        calls += [agent_event(f"tâche simple ({n} caractères < 6000) -> a (défaut)", "hors_schema")
                  for n in range(4000, 6000, 250)]
        calls += [agent_event("tâche difficile (7000 caractères >= 6000) -> b (défaut)")]
        write_jsonl(logs / "run-20260101-120000-agents-0a1b2c3d.jsonl", calls, extra_lines=["{cassé"])
        (logs / "escalation-0123abcd.json").write_text("{pas du json", encoding="utf-8")
        return logs, state

    def test_rapport_complet(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs, state = self.build(Path(tmp))
            env_before = dict(os.environ)
            rep = cal.run(logs, state)
            self.assertEqual(dict(os.environ), env_before)
            self.assertIn("Décisions exploitées : 24 (12 gardée(s), 12 jetée(s))", rep.text)
            self.assertIn("1 décision(s) du pilote exclue(s)", rep.text)
            self.assertIn("2e meilleure note des reviewers (journaux de revue)", rep.text)
            self.assertIn("agent producteur gemini : 1/2", rep.text)
            line = next(l for l in rep.env_lines if l.startswith("REVIEW_THRESHOLD="))
            self.assertTrue(4.0 < float(line.split("=")[1]) < 7.0)
            seuil = next(l for l in rep.env_lines if l.startswith("AGENT_SEUIL_CARACTERES="))
            self.assertTrue(3000 <= int(seuil.split("=")[1]) <= 4000)
            self.assertIn("# AGENT_SEUIL_SCORE : pas de suggestion", rep.env_lines)
            self.assertIn("# COMPLEXITY_THRESHOLD : pas de suggestion (données insuffisantes)", rep.env_lines)
            self.assertNotIn("Idée fictive", rep.text)  # jamais de titre affiché
            # Aucun fichier créé ni modifié (en particulier aucun .env).
            self.assertFalse(any(p.name == ".env" for p in Path(tmp).rglob("*")))

    def test_main_dossiers_absents(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cal.main(["--logs", str(Path(tmp) / "rien"), "--state", str(Path(tmp) / "rien")])
            self.assertEqual(code, 0)
            self.assertIn("anecdotique, pas statistique", buf.getvalue())
            self.assertIn("n'écrit jamais dans .env", buf.getvalue())
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

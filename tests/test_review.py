"""Reviewers et vote : le code valide, compte les votes et choisit la shortlist."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_client import GeminiDailyQuotaError, GeminiError
from rate_limiter import DailyQuotaExhausted
from review import (
    DEFAULT_REVIEWERS,
    Evaluation,
    IdeaVerdict,
    ReviewError,
    ReviewOutcome,
    ReviewPanel,
    ReviewerSpec,
    compute_verdicts,
    parse_review,
    render_review,
    review_schema,
    select_shortlist,
)
from verify.repair import RepairResult, repair_json
from workers.dedup import Idea


def idea(i, kind="simple", titre=None):
    return Idea(i, {"titre": titre or f"Idée {i}", "probleme": "p", "approche": "a", "effort": "faible", "type": kind})


def evals(scores):
    """scores : {id: note} -> JSON de revue."""
    return json.dumps({"evaluations": [{"id": i, "score": s, "justification": f"j{i}", "objection": ""}
                                       for i, s in scores.items()]})


class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def generate(self, prompt, *, system=None, temperature=0.7, json_output=False, max_output_tokens=None):
        self.calls.append({"prompt": prompt, "system": system, "temperature": temperature, "json": json_output})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        if isinstance(a, tuple):
            return SimpleNamespace(text=a[0], attempts=1, truncated=a[1])
        return SimpleNamespace(text=a, attempts=1, truncated=False)


class ParseReviewTest(unittest.TestCase):
    schema = review_schema(3)

    def test_valide(self):
        r = parse_review(evals({1: 7, 2: 3.5, 3: 10}), "x", [1, 2, 3], self.schema, None)
        self.assertTrue(r.valid)
        self.assertEqual(r.evaluations[2].score, 3.5)
        self.assertEqual(r.missing, [])

    def test_identifiant_inconnu_ou_double(self):
        for scores_json in [evals({1: 7, 9: 3}), json.dumps({"evaluations": [
                {"id": 1, "score": 7, "justification": "a", "objection": ""},
                {"id": 1, "score": 2, "justification": "b", "objection": ""}]})]:
            r = parse_review(scores_json, "x", [1, 2, 3], self.schema, None)
            self.assertFalse(r.valid)

    def test_idee_manquante_abstention(self):
        r = parse_review(evals({1: 7, 3: 2}), "x", [1, 2, 3], self.schema, None)
        self.assertTrue(r.valid)
        self.assertEqual(r.missing, [2])

    def test_note_hors_bornes(self):
        self.assertFalse(parse_review(evals({1: 11}), "x", [1], self.schema, None).valid)
        self.assertFalse(parse_review(evals({1: -1}), "x", [1], self.schema, None).valid)

    def test_reparation(self):
        broken = "Voici : " + evals({1: 7}) + " fin"
        r = parse_review(broken, "x", [1], self.schema, lambda t, s: repair_json(t, s, None))
        self.assertTrue(r.valid)
        self.assertEqual(r.repaired, "deterministe")
        r = parse_review("n'importe quoi", "x", [1], self.schema, lambda t, s: RepairResult(ok=False, reasons=["non"]))
        self.assertFalse(r.valid)
        self.assertFalse(parse_review("n'importe quoi", "x", [1], self.schema, None).valid)

    def test_reparation_revalidee(self):
        bad = lambda t, s: RepairResult(ok=True, text=evals({1: 99}), method="qwen")
        self.assertFalse(parse_review("cassé", "x", [1], self.schema, bad).valid)


def outcome(name, scores, valid=True):
    return ReviewOutcome(name, {i: Evaluation(i, s, "j", "") for i, s in scores.items()}, valid=valid)


class VoteTest(unittest.TestCase):
    ideas = [idea(1), idea(2), idea(3)]

    def test_seuil_et_vote_2_sur_3(self):
        reviews = [outcome("a", {1: 6.5, 2: 6.4, 3: 9}), outcome("b", {1: 6.5, 2: 9, 3: 2}),
                   outcome("c", {1: 0, 2: 6.4, 3: 1})]
        v = {x.idea.id: x for x in compute_verdicts(self.ideas, reviews, DEFAULT_REVIEWERS, 6.5, 2)}
        self.assertEqual((v[1].approvals, v[1].approved), (2, True))  # 6,5 = approbation
        self.assertEqual((v[2].approvals, v[2].approved), (1, False))  # 6,4 = non
        self.assertEqual((v[3].approvals, v[3].approved), (1, False))
        self.assertAlmostEqual(v[1].weighted_score, 4.33)

    def test_revue_invalide_et_abstention_jamais_approbation(self):
        reviews = [outcome("a", {1: 9}), outcome("b", {1: 9}, valid=False), outcome("c", {})]
        v = compute_verdicts([idea(1)], reviews, DEFAULT_REVIEWERS, 6.5, 2)[0]
        self.assertEqual(v.approvals, 1)
        self.assertFalse(v.approved)
        self.assertEqual(v.weighted_score, 9.0)  # seule la note valide compte dans le score

    def test_ponderation(self):
        specs = [ReviewerSpec("a", "p", 0.1, weight=3.0), ReviewerSpec("b", "p", 0.1, weight=1.0)]
        v = compute_verdicts([idea(1)], [outcome("a", {1: 8}), outcome("b", {1: 4})], specs, 6.5, 1)[0]
        self.assertEqual(v.weighted_score, 7.0)

    def test_classement(self):
        # L'idée 3 a le meilleur score moyen (7,33) mais un seul vote : elle passe après les approuvées.
        reviews = [outcome("a", {1: 7, 2: 9, 3: 6}), outcome("b", {1: 7, 2: 7, 3: 6}), outcome("c", {1: 1, 2: 1, 3: 10})]
        verdicts = compute_verdicts(self.ideas, reviews, DEFAULT_REVIEWERS, 6.5, 2)
        self.assertEqual([x.idea.id for x in verdicts], [2, 1, 3])
        self.assertGreater(verdicts[2].weighted_score, verdicts[0].weighted_score)


def verdict(i, score, approved, kind="simple"):
    return IdeaVerdict(idea(i, kind), {}, 2 if approved else 0, approved, score)


class ShortlistTest(unittest.TestCase):
    def sl(self, verdicts, **kw):
        params = dict(min_size=5, max_size=8, contrarian_quota=2, contrarian_floor=4.0)
        params.update(kw)
        return select_shortlist(verdicts, **params)

    def test_plafond_de_8(self):
        vs = [verdict(i, 9 - i * 0.1, True) for i in range(1, 12)]
        short = self.sl(vs)
        self.assertEqual([v.idea.id for v in short], list(range(1, 9)))

    def test_complement_jusqu_a_5(self):
        vs = [verdict(1, 8, True), verdict(2, 7, True), verdict(3, 6, False), verdict(4, 5, False),
              verdict(5, 3, False), verdict(6, 2, False)]
        short = self.sl(vs)
        self.assertEqual([v.idea.id for v in short], [1, 2, 3, 4, 5])
        self.assertEqual([v.shortlist_reason for v in short][2:], ["complément"] * 3)

    def test_contrariennes_a_score_moyen_conservees(self):
        vs = [verdict(i, 9, True) for i in range(1, 9)] + [verdict(9, 4.5, False, "contrarienne"),
                                                          verdict(10, 5, False, "contrarienne")]
        short = self.sl(vs)
        ids = [v.idea.id for v in short]
        self.assertEqual(len(short), 8)
        self.assertIn(9, ids)
        self.assertIn(10, ids)
        self.assertEqual(sum(1 for v in short if v.shortlist_reason == "contrarienne"), 2)

    def test_contrarienne_sous_le_plancher_exclue(self):
        vs = [verdict(i, 8, True) for i in range(1, 6)] + [verdict(6, 3.9, False, "contrarienne")]
        self.assertNotIn(6, [v.idea.id for v in self.sl(vs)])

    def test_remplacement_vise_d_abord_les_complements(self):
        vs = [verdict(1, 9, True), verdict(2, 8, True), verdict(3, 7, True), verdict(4, 6, False),
              verdict(5, 5.5, False), verdict(6, 4.2, False, "contrarienne")]
        short = self.sl(vs, max_size=5)
        ids = [v.idea.id for v in short]
        self.assertEqual(sorted(ids), [1, 2, 3, 4, 6])  # le complément 5 (le plus faible) remplacé

    def test_contrarienne_deja_approuvee_compte_dans_le_quota(self):
        vs = [verdict(1, 9, True, "contrarienne"), verdict(2, 9, True, "contrarienne")] + \
             [verdict(i, 8, True) for i in range(3, 10)] + [verdict(10, 5, False, "contrarienne")]
        self.assertNotIn(10, [v.idea.id for v in self.sl(vs)])

    def test_peu_d_idees(self):
        short = self.sl([verdict(1, 8, True), verdict(2, 2, False)])
        self.assertEqual(len(short), 2)

    def test_idee_sans_note_jamais_retenue(self):
        vs = [verdict(1, 8, True), IdeaVerdict(idea(2), {}, 0, False, None)]
        self.assertEqual([v.idea.id for v in self.sl(vs)], [1])


class PanelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name)
        self.ideas = [idea(1), idea(2), idea(3, "contrarienne"), idea(4), idea(5), idea(6)]

    def tearDown(self):
        self._tmp.cleanup()

    def panel(self, answers, **kw):
        self.client = FakeClient(answers)
        return ReviewPanel(self.client, logs_dir=self.logs, **kw)

    def test_trois_revues_independantes(self):
        scores = {i: 7 for i in range(1, 7)}
        report = self.panel([evals(scores)] * 3).review(self.ideas, "Objectif : test")
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(report.gemini_requests, 3)
        prompts = {c["prompt"] for c in self.client.calls}
        self.assertEqual(len(prompts), 1)  # même prompt : aucun reviewer ne voit les notes des autres
        self.assertEqual(len({c["system"] for c in self.client.calls}), 3)  # personnalités différentes
        self.assertEqual(len({c["temperature"] for c in self.client.calls}), 3)
        self.assertTrue(all(c["json"] for c in self.client.calls))
        self.assertIn('"id": 3', self.client.calls[0]["prompt"])
        self.assertIn("Objectif : test", self.client.calls[0]["prompt"])

    def test_un_reviewer_en_panne(self):
        good = evals({i: 8 for i in range(1, 7)})
        report = self.panel([good, GeminiError("HTTP 500"), good]).review(self.ideas)
        self.assertTrue(all(v.approved for v in report.verdicts))  # 2 votes sur 3 suffisent
        self.assertTrue(any("invalide" in w for w in report.warnings))

    def test_panne_compte_comme_non(self):
        good, bad = evals({i: 8 for i in range(1, 7)}), evals({i: 2 for i in range(1, 7)})
        report = self.panel([good, GeminiError("x"), bad]).review(self.ideas)
        self.assertFalse(any(v.approved for v in report.verdicts))

    def test_aucune_revue_valide(self):
        with self.assertRaises(ReviewError):
            self.panel(["cassé", "cassé", ("{}", True)]).review(self.ideas)

    def test_revue_tronquee_invalide(self):
        good = evals({i: 8 for i in range(1, 7)})
        report = self.panel([good, (good, True), good]).review(self.ideas)
        self.assertFalse([r for r in report.reviews if r.reviewer == "utilisateur"][0].valid)

    def test_quota_journalier_interrompt(self):
        for exc in (DailyQuotaExhausted("stop"), GeminiDailyQuotaError("stop")):
            with self.subTest(exc=type(exc).__name__), self.assertRaises(type(exc)):
                self.panel([exc]).review(self.ideas)

    def test_trop_d_idees_refuse_avant_tout_appel(self):
        p = self.panel([], max_ideas=5)
        with self.assertRaises(ReviewError):
            p.review(self.ideas)
        self.assertEqual(self.client.calls, [])

    def test_journal(self):
        report = self.panel([evals({i: 7 for i in range(1, 7)})] * 3).review(self.ideas)
        events = [json.loads(l) for l in report.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["review", "review", "review", "review_end"])
        self.assertEqual(events[0]["reviewer"], "pragmatique")
        self.assertIn("raw", events[0])
        self.assertEqual(len(events[-1]["verdicts"]), 6)

    def test_rendu(self):
        scores = {1: 8, 2: 7, 3: 4.5, 4: 7, 5: 7, 6: 6.5}
        report = self.panel([evals(scores)] * 3).review(self.ideas)
        text = render_review(report, remaining_quota=100)
        self.assertIn("note >= 6,5", text)
        self.assertIn("contrarienne conservée", text)
        self.assertIn("pragmatique 8,0 : j1", text)
        self.assertIn("/valider", text)
        text.encode("cp1252")  # affichable dans une console Windows

    def test_parametres_invalides(self):
        for kw in ({"votes_needed": 4}, {"votes_needed": 0}, {"threshold": 11}, {"shortlist_min": 9},
                   {"reviewers": ()}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                ReviewPanel(FakeClient([]), **kw)


if __name__ == "__main__":
    unittest.main()

"""Escalade : contrat figé, diff validé par le code, contrôle de promesse, échec fermé."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from escalation import (
    Escalator,
    build_contract,
    contract_fingerprint,
    estimate_complexity,
    render_escalation,
    structural_checks,
)
from tests.test_promise import FakeLLM
from verify.promise import ProposedVersion
from verify.repair import repair_json
from workers.dedup import Idea

IDEA = Idea(1, {"titre": "Frigo Express", "probleme": "On oublie les restes",
                "approche": "Saisir 3 ingrédients pour une recette", "effort": "faible", "type": "simple"})
CONTRACT = build_contract(IDEA, ["gratuite", "fonctionne hors ligne"], ["application Android"],
                          ["livraison", "paiement"])


def answer(promesse=None, contraintes=None, perimetre=None, diff=None, etapes=("V1",), proposition="Faire X"):
    return json.dumps({
        "version": {"promesse": promesse or CONTRACT.promesse,
                    "perimetre_inclus": list(perimetre if perimetre is not None else CONTRACT.perimetre_inclus),
                    "hors_perimetre": list(CONTRACT.hors_perimetre),
                    "contraintes": list(contraintes if contraintes is not None else CONTRACT.contraintes)},
        "diff": diff if diff is not None else [{"element": CONTRACT.promesse, "statut": "conservé", "raison": "", "impact": ""}],
        "etapes": list(etapes), "proposition": proposition}, ensure_ascii=False)


class FakeBig:
    def __init__(self, answers, truncated=False):
        self.answers = list(answers)
        self.calls = []
        self.truncated = truncated

    def generate(self, prompt, *, system=None, temperature=0.7, json_output=False, max_output_tokens=None):
        self.calls.append({"prompt": prompt, "system": system, "json": json_output})
        return SimpleNamespace(text=self.answers.pop(0), attempts=1, truncated=self.truncated)


class ContractTest(unittest.TestCase):
    def test_contrat_construit_par_le_code(self):
        self.assertEqual(CONTRACT.promesse, "Frigo Express : Saisir 3 ingrédients pour une recette")
        self.assertEqual(CONTRACT.contraintes, ("gratuite", "fonctionne hors ligne"))
        self.assertIn("Résoudre : On oublie les restes", CONTRACT.perimetre_inclus)
        self.assertEqual(CONTRACT.hors_perimetre, ("livraison", "paiement"))

    def test_empreinte_stable_et_sensible(self):
        self.assertEqual(contract_fingerprint(CONTRACT), contract_fingerprint(build_contract(
            IDEA, ["gratuite", "fonctionne hors ligne"], ["application Android"], ["livraison", "paiement"])))
        other = build_contract(IDEA, ["gratuite"], ["application Android"], ["livraison", "paiement"])
        self.assertNotEqual(contract_fingerprint(CONTRACT), contract_fingerprint(other))


class ComplexityTest(unittest.TestCase):
    def test_score_calcule_par_le_code(self):
        llm = FakeLLM([json.dumps({"fichiers_touches": 4, "dependances_nouvelles": 2,
                                   "risque_regression": "moyen", "justification": "x"})])
        cx = estimate_complexity(llm, IDEA, "Objectif : X")
        self.assertEqual(cx.score, 4 + 2 * 2 + 3)
        self.assertEqual(llm.calls[0]["temperature"], 0.0)
        self.assertIsNotNone(llm.calls[0]["schema"])


class StructuralChecksTest(unittest.TestCase):
    def v(self, **kw):
        base = dict(promesse=CONTRACT.promesse, contraintes=CONTRACT.contraintes,
                    perimetre_inclus=CONTRACT.perimetre_inclus, hors_perimetre=CONTRACT.hors_perimetre)
        base.update(kw)
        return ProposedVersion(**base)

    def test_version_conforme(self):
        self.assertEqual(structural_checks(CONTRACT, self.v(), []), [])

    def test_contrainte_disparue(self):
        r = structural_checks(CONTRACT, self.v(contraintes=("gratuite",)), [])
        self.assertIn("fonctionne hors ligne", r[0])

    def test_hors_perimetre_ajoute(self):
        r = structural_checks(CONTRACT, self.v(perimetre_inclus=("application Android", "Paiement")), [])
        self.assertIn("hors périmètre", r[0])

    def test_diff_injustifie(self):
        diff = [{"element": "x", "statut": "modifié", "raison": "plus simple", "impact": ""},
                {"element": "y", "statut": "retiré", "raison": " ", "impact": ""},
                {"element": "fonctionne hors ligne", "statut": "modifié", "raison": "r", "impact": "i"},
                {"element": CONTRACT.promesse, "statut": "retiré", "raison": "r", "impact": "i"}]
        r = structural_checks(CONTRACT, self.v(), diff)
        self.assertEqual(len(r), 4)

    def test_reduction_de_perimetre_autorisee(self):
        diff = [{"element": "application Android", "statut": "reporté", "raison": "V2", "impact": "moins d'utilisateurs"}]
        self.assertEqual(structural_checks(CONTRACT, self.v(perimetre_inclus=()), diff), [])


class EscalatorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def esc(self, big_answers, qwen_answers=(), truncated=False, repairer=None):
        self.big = FakeBig(big_answers, truncated)
        self.qwen = FakeLLM(list(qwen_answers))
        return Escalator(self.big, "test", self.qwen, repairer=repairer, logs_dir=self.dir / "logs",
                         state_dir=self.dir / "state")

    def test_version_acceptee_promesse_identique_sans_qwen(self):
        r = self.esc([answer()]).run(IDEA, CONTRACT, "fiche")
        self.assertTrue(r.accepted, r.reasons)
        self.assertEqual(self.qwen.calls, [])  # texte identique : pas besoin de demander
        self.assertTrue(self.big.calls[0]["json"])
        self.assertIn(contract_fingerprint(CONTRACT), self.big.calls[0]["prompt"])
        self.assertTrue((self.dir / "state" / "escalations" / f"{r.escalation_id}.json").exists())
        self.assertTrue((self.dir / "logs" / f"escalation-{r.escalation_id}.json").exists())

    def test_reformulation_validee_par_qwen(self):
        r = self.esc([answer(promesse="Recette instantanée à partir de 3 ingrédients saisis (Frigo Express)")],
                     ['{"identique": true, "explication": "même résultat"}']).run(IDEA, CONTRACT, "")
        self.assertTrue(r.accepted)
        self.assertTrue(r.promise.promise_identical)

    def test_promesse_changee_rejetee(self):
        r = self.esc([answer(promesse="Application de livraison de repas")],
                     ['{"identique": false, "explication": "autre résultat"}']).run(IDEA, CONTRACT, "")
        self.assertFalse(r.accepted)
        self.assertIn("promesse modifiée", r.reasons[0])

    def test_contrainte_levee_rejetee_sans_consulter_qwen(self):
        r = self.esc([answer(contraintes=["gratuite"])]).run(IDEA, CONTRACT, "")
        self.assertFalse(r.accepted)
        self.assertEqual(self.qwen.calls, [])

    def test_qwen_indisponible_echec_ferme(self):
        from local_llm import LocalLLMError
        r = self.esc([answer(promesse="Autre formulation")], [LocalLLMError("éteint")]).run(IDEA, CONTRACT, "")
        self.assertFalse(r.accepted)

    def test_reponse_invalide_rejetee(self):
        for bad in ["pas du json", json.dumps({"version": {}})]:
            with self.subTest(bad=bad[:20]):
                self.assertFalse(self.esc([bad]).run(IDEA, CONTRACT, "").accepted)

    def test_reponse_reparee(self):
        r = self.esc(["Voici : " + answer() + " fin"], repairer=lambda t, s: repair_json(t, s, None)).run(IDEA, CONTRACT, "")
        self.assertTrue(r.accepted, r.reasons)
        self.assertEqual(r.repaired, "deterministe")

    def test_reponse_tronquee_rejetee(self):
        r = self.esc([answer()], truncated=True).run(IDEA, CONTRACT, "")
        self.assertFalse(r.accepted)
        self.assertIn("tronquée", r.reasons[0])

    def test_nouvelle_tentative_repart_du_contrat(self):
        e = self.esc([answer(contraintes=["gratuite"]), answer()])
        self.assertFalse(e.run(IDEA, CONTRACT, "").accepted)
        self.assertTrue(e.run(IDEA, CONTRACT, "").accepted)
        self.assertEqual(self.big.calls[0]["prompt"], self.big.calls[1]["prompt"])  # même contrat, rien de la version rejetée

    def test_rendu(self):
        r = self.esc([answer(etapes=["V1 simple", "V2"], proposition="Code Kotlin")]).run(IDEA, CONTRACT, "")
        text = render_escalation(r)
        self.assertIn("VERSION ACCEPTÉE", text)
        self.assertIn("[conservé]", text)
        self.assertIn("Code Kotlin", text)
        text.encode("cp1252")


if __name__ == "__main__":
    unittest.main()

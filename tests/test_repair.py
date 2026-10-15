"""Réparation JSON : déterministe d'abord, Qwen 2.5 3B ensuite, jamais de contenu inventé."""

import json
import unittest

from local_llm import LocalLLMError
from orchestrator import Orchestrator
from tests.test_orchestrator import RecordingWorker, ScriptedOrganizer, plan
from tests.test_promise import FakeLLM
from verify.basic import basic_verifier
from verify.schema import parse_json
from verify.repair import MAX_REPAIR_CHARS, repair_json, unfaithful_strings
from workers.base import Reply, WorkerSpec
from workers.defaults import IDEAS_SCHEMA

IDEA = {"titre": "Scan du frigo", "probleme": "On oublie les restes", "approche": "Photo et reconnaissance",
        "effort": "moyen", "type": "simple"}
VALID = json.dumps({"idees": [IDEA]}, ensure_ascii=False)


class RepairTest(unittest.TestCase):
    def test_json_valide_aucune_reparation(self):
        llm = FakeLLM([])
        r = repair_json(VALID, IDEAS_SCHEMA, llm)
        self.assertTrue(r.ok)
        self.assertEqual((r.method, r.text), ("aucune", VALID))
        self.assertEqual(llm.calls, [])

    def test_reparations_deterministes_sans_modele(self):
        llm = FakeLLM([])
        for broken in [
            f"Voici mes idées :\n{VALID}\nBonne journée !",
            VALID.replace("}]}", "},]}"),  # virgule finale
            f"```json\n{VALID}\n```",
        ]:
            with self.subTest(broken=broken[:40]):
                r = repair_json(broken, IDEAS_SCHEMA, llm)
                self.assertTrue(r.ok, r.reasons)
                self.assertIn(r.method, ("deterministe", "aucune"))
                self.assertEqual(parse_json(r.text)["idees"][0]["titre"], "Scan du frigo")
        self.assertEqual(llm.calls, [])

    def test_reparation_par_qwen_fidele(self):
        broken = ('{"idees": [{"titre": "Scan du frigo", "probleme": "On oublie les restes", '
                  '"approche": "Photo et reconnaissance", "effort": "moyenne", "type": "simple"}]}')
        llm = FakeLLM([VALID])
        r = repair_json(broken, IDEAS_SCHEMA, llm)
        self.assertTrue(r.ok, r.reasons)
        self.assertEqual(r.method, "qwen")
        self.assertIn("moyenne", r.original_error)
        call = llm.calls[0]
        self.assertEqual(call["schema"], IDEAS_SCHEMA)  # sortie contrainte par le schéma
        self.assertEqual(call["temperature"], 0.0)
        self.assertIn("<<<DOCUMENT\n" + broken + "\nDOCUMENT>>>", call["messages"][-1].content)

    def test_reparation_qui_invente_refusee(self):
        broken = '{"idees": [{"titre": "Scan du frigo", "probleme": "On oublie les restes", "effort": "moyen"}]}'
        invented = json.dumps({"idees": [dict(IDEA, approche="Une approche inventée par le modèle")]})
        r = repair_json(broken, IDEAS_SCHEMA, FakeLLM([invented]))
        self.assertFalse(r.ok)
        self.assertIn("absent de l'original", r.reasons[0])

    def test_reparation_qui_reecrit_refusee(self):
        broken = VALID.replace('"moyen"', '"moyenne"')
        rewritten = json.dumps({"idees": [dict(IDEA, titre="Scanner intelligent du réfrigérateur")]})
        self.assertFalse(repair_json(broken, IDEAS_SCHEMA, FakeLLM([rewritten])).ok)

    def test_echec_ferme(self):
        broken = VALID.replace('"moyen"', '"moyenne"')
        for answer in [LocalLLMError("Ollama éteint"), "toujours pas du json", '{"idees": []}']:
            with self.subTest(answer=str(answer)[:30]):
                r = repair_json(broken, IDEAS_SCHEMA, FakeLLM([answer]))
                self.assertFalse(r.ok)
                self.assertTrue(r.reasons)

    def test_sans_modele(self):
        r = repair_json(VALID.replace('"moyen"', '"moyenne"'), IDEAS_SCHEMA, None)
        self.assertFalse(r.ok)
        self.assertIn("aucun modèle", r.reasons[0])

    def test_sortie_trop_longue(self):
        llm = FakeLLM([])
        r = repair_json("x" * (MAX_REPAIR_CHARS + 1), IDEAS_SCHEMA, llm)
        self.assertFalse(r.ok)
        self.assertEqual(llm.calls, [])

    def test_fidelite_normalisee(self):
        original = 'titre: "Scan  du\\nFRIGO", l’approche'
        self.assertEqual(unfaithful_strings(original, {"t": "scan du frigo", "a": "l'approche"}, {}), [])
        self.assertEqual(unfaithful_strings(original, {"t": "autre"}, {}), ["autre"])

    def test_enumerations_exemptees(self):
        schema = {"type": "object", "properties": {"niveau": {"type": "string", "enum": ["haut"]}}}
        self.assertEqual(unfaithful_strings('niveau: "élevé"', {"niveau": "haut"}, schema), [])
        self.assertEqual(unfaithful_strings('niveau: "élevé"', {"niveau": "haut"}, {}), ["haut"])


IDEAS_SPEC = WorkerSpec(name="generateur", description="idées", system_prompt="s", output_schema=IDEAS_SCHEMA)


class BusRepairTest(unittest.TestCase):
    def run_bus(self, outputs, llm, truncated=False):
        class W(RecordingWorker):
            def run(self, instruction, context):
                self.calls.append(instruction)
                return Reply(self.outputs.pop(0), attempts=1, truncated=truncated)

        org = ScriptedOrganizer([plan(*[("generateur", str(i)) for i in range(len(outputs))])])
        orch = Orchestrator(org, [W(IDEAS_SPEC, list(outputs))], basic_verifier,
                            repairer=lambda text, schema: repair_json(text, schema, llm))
        return orch.run("tâche"), org

    def test_sortie_reparee_acceptee_et_transmise(self):
        broken = VALID.replace('"moyen"', '"moyenne"')
        report, org = self.run_bus([broken], FakeLLM([VALID]))
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.accepted[0].repair, "qwen")
        self.assertEqual(json.loads(report.accepted[0].text)["idees"][0]["effort"], "moyen")
        self.assertEqual(org.synth_calls[0][0].text, report.accepted[0].text)  # la synthèse reçoit la version réparée
        self.assertEqual(report.gemini_requests, 3)  # aucune requête Gemini en plus

    def test_reparation_echouee_reste_rejetee(self):
        broken = VALID.replace('"moyen"', '"moyenne"')
        report, _ = self.run_bus([broken], FakeLLM([LocalLLMError("éteint")]))
        self.assertEqual(report.status, "failed")
        self.assertTrue(any("JSON non conforme" in i for i in report.rejected[0].issues))
        self.assertTrue(any("réparation impossible" in i for i in report.rejected[0].issues))

    def test_sortie_tronquee_jamais_reparee(self):
        llm = FakeLLM([])
        report, _ = self.run_bus([VALID[:40]], llm, truncated=True)
        self.assertEqual(report.status, "failed")
        self.assertEqual(llm.calls, [])

    def test_reparation_revalidee_par_le_code(self):
        broken = VALID.replace('"moyen"', '"moyenne"')
        bad_repairer_result = json.dumps({"idees": [dict(IDEA, effort="moyenne")]})

        from verify.repair import RepairResult
        org = ScriptedOrganizer([plan(("generateur", "x"))])
        orch = Orchestrator(org, [RecordingWorker(IDEAS_SPEC, [broken])], basic_verifier,
                            repairer=lambda t, s: RepairResult(ok=True, text=bad_repairer_result, method="qwen"))
        report = orch.run("tâche")
        self.assertEqual(report.status, "failed")
        self.assertIn("réparation non conforme", report.rejected[0].issues[-1])

    def test_worker_sans_schema_jamais_repare(self):
        calls = []
        org = ScriptedOrganizer([plan(("libre", "x"))])
        spec = WorkerSpec(name="libre", description="d", system_prompt="s")
        orch = Orchestrator(org, [RecordingWorker(spec, ["   "])], basic_verifier,
                            repairer=lambda t, s: calls.append(1))
        self.assertEqual(orch.run("tâche").status, "failed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

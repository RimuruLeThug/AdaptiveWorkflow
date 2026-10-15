"""Bus d'orchestration : l'organisateur propose, le code décide."""

import json
import tempfile
import unittest
from pathlib import Path

from config import GeminiQuota
from gemini_client import GeminiClient, GeminiDailyQuotaError, GeminiError, HttpResponse
from orchestrator import DepthExceeded, Orchestrator, PlanRejected, validate_plan
from organizers.base import Organizer
from organizers.gemini import GeminiOrganizer
from rate_limiter import DailyQuotaExhausted, RateLimiter, RetriesExhausted
from verify.basic import basic_verifier
from workers.base import Reply, Worker, WorkerSpec
from workers.gemini import GeminiWorker

IDEAS_SCHEMA = {
    "type": "object",
    "required": ["idees"],
    "properties": {"idees": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
}


def spec(name, schema=None):
    return WorkerSpec(name=name, description=f"rôle {name}", system_prompt=f"tu es {name}", output_schema=schema)


def plan(*pairs):
    return json.dumps({"tasks": [{"worker": w, "instruction": i} for w, i in pairs]})


class ScriptedOrganizer(Organizer):
    def __init__(self, plans, synthesis="synthèse"):
        self.plans = list(plans)
        self.synthesis = synthesis
        self.plan_calls = []
        self.synth_calls = []

    def propose_plan(self, task, context, workers, max_tasks, feedback=None):
        self.plan_calls.append({"max_tasks": max_tasks, "feedback": feedback, "workers": [w.name for w in workers]})
        p = self.plans.pop(0)
        if isinstance(p, Exception):
            raise p
        return Reply(p, attempts=1)

    def synthesize(self, task, context, results):
        self.synth_calls.append(list(results))
        if isinstance(self.synthesis, Exception):
            raise self.synthesis
        return Reply(self.synthesis, attempts=1)


class RecordingWorker(Worker):
    def __init__(self, spec, outputs):
        super().__init__(spec)
        self.outputs = list(outputs)
        self.calls = []

    def run(self, instruction, context):
        self.calls.append((instruction, context))
        o = self.outputs.pop(0)
        if isinstance(o, Exception):
            raise o
        return Reply(o, attempts=1)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name) / "logs"

    def tearDown(self):
        self._tmp.cleanup()

    def orch(self, organizer, workers, **kw):
        kw.setdefault("logs_dir", self.logs)
        return Orchestrator(organizer, workers, basic_verifier, **kw)

    def events(self, report):
        return [json.loads(l) for l in report.log_path.read_text(encoding="utf-8").splitlines()]


class PlanValidationTest(unittest.TestCase):
    names = ["generateur", "analyste"]

    def test_plan_valide(self):
        tasks = validate_plan(plan(("generateur", " idées "), ("analyste", "analyse")), self.names, 6)
        self.assertEqual([(t.worker, t.instruction) for t in tasks], [("generateur", "idées"), ("analyste", "analyse")])

    def test_plans_refuses(self):
        cases = {
            "worker inconnu": plan(("pirate", "x")),
            "trop de tâches": plan(*[("generateur", "x")] * 7),
            "liste vide": json.dumps({"tasks": []}),
            "pas du JSON": "Voici mon plan : générer des idées",
            "champ en trop": json.dumps({"tasks": [{"worker": "generateur", "instruction": "x", "outils": ["shell"]}]}),
            "racine en trop": json.dumps({"tasks": [{"worker": "generateur", "instruction": "x"}], "budget": 99}),
            "consigne vide": plan(("generateur", "")),
            "consigne espaces": plan(("generateur", "   ")),
            "worker non chaîne": json.dumps({"tasks": [{"worker": 1, "instruction": "x"}]}),
            "consigne trop longue": plan(("generateur", "x" * 4001)),
            "tableau racine": json.dumps([{"worker": "generateur", "instruction": "x"}]),
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(PlanRejected):
                validate_plan(raw, self.names, 6)

    def test_budget_nul(self):
        with self.assertRaises(PlanRejected):
            validate_plan(plan(("generateur", "x")), self.names, 0)

    def test_message_worker_inconnu_utile(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_plan(plan(("pirate", "x")), self.names, 6)
        self.assertIn("generateur, analyste", str(cm.exception))


class RunTest(Base):
    def test_run_nominal(self):
        org = ScriptedOrganizer([plan(("gen", "idées A"), ("ana", "analyse B"))])
        gen, ana = RecordingWorker(spec("gen"), ["sortie gen"]), RecordingWorker(spec("ana"), ["sortie ana"])
        r = self.orch(org, [gen, ana]).run("tâche", "fiche")
        self.assertEqual(r.status, "ok")
        self.assertEqual([o.text for o in r.accepted], ["sortie gen", "sortie ana"])
        self.assertEqual(r.synthesis, "synthèse")
        self.assertEqual(r.delegations_used, 2)
        self.assertEqual(r.gemini_requests, 4)  # plan + 2 workers + synthèse
        self.assertEqual(org.plan_calls[0]["max_tasks"], 6)
        self.assertEqual(org.plan_calls[0]["workers"], ["gen", "ana"])

    def test_isolement_des_workers(self):
        org = ScriptedOrganizer([plan(("gen", "un"), ("gen", "deux"), ("gen", "trois"))])
        gen = RecordingWorker(spec("gen"), ["SECRET_1", "SECRET_2", "SECRET_3"])
        self.orch(org, [gen]).run("tâche", "fiche")
        self.assertEqual(gen.calls, [("un", "fiche"), ("deux", "fiche"), ("trois", "fiche")])
        for instruction, context in gen.calls:
            self.assertNotIn("SECRET", instruction + context)

    def test_synthese_recoit_tout_et_seulement_le_verifie(self):
        org = ScriptedOrganizer([plan(("gen", "a"), ("js", "b"), ("js", "c"))])
        gen = RecordingWorker(spec("gen"), ["texte libre"])
        js = RecordingWorker(spec("js", IDEAS_SCHEMA), ['{"idees": ["x"]}', "pas du json"])
        r = self.orch(org, [gen, js]).run("tâche")
        self.assertEqual(r.status, "partial")
        self.assertEqual([res.text for res in org.synth_calls[0]], ["texte libre", '{"idees": ["x"]}'])
        self.assertEqual(len(r.rejected), 1)
        self.assertIn("JSON non conforme", r.rejected[0].issues[0])

    def test_json_valide_mais_hors_schema_rejete(self):
        org = ScriptedOrganizer([plan(("js", "a"), ("js", "b"))])
        js = RecordingWorker(spec("js", IDEAS_SCHEMA), ['{"idees": []}', '{"autre": 1}'])
        r = self.orch(org, [js]).run("tâche")
        self.assertEqual(r.status, "failed")
        self.assertEqual(len(r.rejected), 2)
        self.assertEqual(org.synth_calls, [])

    def test_sortie_tronquee_rejetee(self):
        class Truncating(RecordingWorker):
            def run(self, instruction, context):
                return Reply("début de réponse", attempts=1, truncated=True)

        org = ScriptedOrganizer([plan(("gen", "a"))])
        r = self.orch(org, [Truncating(spec("gen"), [])]).run("tâche")
        self.assertEqual(r.status, "failed")
        self.assertIn("tronquée", r.rejected[0].issues[0])

    def test_plan_corrige_apres_refus(self):
        org = ScriptedOrganizer([plan(("pirate", "x")), plan(("gen", "x"))])
        gen = RecordingWorker(spec("gen"), ["ok"])
        r = self.orch(org, [gen]).run("tâche")
        self.assertEqual(r.plan_attempts, 2)
        self.assertIsNone(org.plan_calls[0]["feedback"])
        self.assertIn("worker inconnu", org.plan_calls[1]["feedback"])
        self.assertEqual(r.gemini_requests, 4)  # 2 plans + 1 worker + synthèse
        kinds = [e["event"] for e in self.events(r)]
        self.assertIn("plan_rejected", kinds)

    def test_plan_refuse_jamais_execute(self):
        org = ScriptedOrganizer([plan(("gen", "a"), ("pirate", "b")), plan(("gen", "a"), ("pirate", "b"))])
        gen = RecordingWorker(spec("gen"), ["ok"])
        with self.assertRaises(PlanRejected):
            self.orch(org, [gen]).run("tâche")
        self.assertEqual(gen.calls, [])  # aucune exécution partielle

    def test_plan_trop_long_refuse_pas_tronque(self):
        org = ScriptedOrganizer([plan(*[("gen", str(i)) for i in range(4)])])
        gen = RecordingWorker(spec("gen"), ["ok"] * 4)
        with self.assertRaises(PlanRejected):
            self.orch(org, [gen], delegation_budget=3, max_plan_attempts=1).run("tâche")
        self.assertEqual(gen.calls, [])

    def test_budget_communique_et_respecte(self):
        org = ScriptedOrganizer([plan(*[("gen", str(i)) for i in range(3)])])
        gen = RecordingWorker(spec("gen"), ["ok"] * 3)
        r = self.orch(org, [gen], delegation_budget=3).run("tâche")
        self.assertEqual(org.plan_calls[0]["max_tasks"], 3)
        self.assertEqual(r.delegations_used, 3)

    def test_profondeur_maximale(self):
        org = ScriptedOrganizer([])
        o = self.orch(org, [RecordingWorker(spec("gen"), [])])
        with self.assertRaises(DepthExceeded):
            o.run("tâche", depth=1)
        self.assertEqual(org.plan_calls, [])  # refusé avant tout appel

    def test_profondeur_configurable(self):
        org = ScriptedOrganizer([plan(("gen", "x"))])
        o = self.orch(org, [RecordingWorker(spec("gen"), ["ok"])], max_depth=2)
        self.assertEqual(o.run("tâche", depth=1).status, "ok")

    def test_echec_worker_isole(self):
        org = ScriptedOrganizer([plan(("gen", "a"), ("gen", "b"))])
        gen = RecordingWorker(spec("gen"), [GeminiError("HTTP 500"), "ok"])
        r = self.orch(org, [gen]).run("tâche")
        self.assertEqual(r.status, "partial")
        self.assertIn("HTTP 500", r.rejected[0].error)

    def test_429_persistant_sur_un_worker(self):
        org = ScriptedOrganizer([plan(("gen", "a"))])
        gen = RecordingWorker(spec("gen"), [RetriesExhausted("429")])
        r = self.orch(org, [gen]).run("tâche")
        self.assertEqual(r.status, "failed")
        self.assertEqual(org.synth_calls, [])  # rien de vérifié : pas de synthèse

    def test_quota_journalier_interrompt_le_run(self):
        for exc in (DailyQuotaExhausted("stop"), GeminiDailyQuotaError("stop")):
            with self.subTest(exc=type(exc).__name__):
                org = ScriptedOrganizer([plan(("gen", "a"), ("gen", "b"))])
                gen = RecordingWorker(spec("gen"), [exc, "jamais"])
                with self.assertRaises(type(exc)):
                    self.orch(org, [gen]).run("tâche")
                self.assertEqual(len(gen.calls), 1)

    def test_quota_journalier_pendant_le_plan(self):
        org = ScriptedOrganizer([DailyQuotaExhausted("stop")])
        with self.assertRaises(DailyQuotaExhausted):
            self.orch(org, [RecordingWorker(spec("gen"), [])]).run("tâche")

    def test_organisateur_en_panne(self):
        org = ScriptedOrganizer([GeminiError("HTTP 503")])
        with self.assertRaises(PlanRejected):
            self.orch(org, [RecordingWorker(spec("gen"), [])]).run("tâche")

    def test_synthese_optionnelle(self):
        org = ScriptedOrganizer([plan(("gen", "a"))])
        r = self.orch(org, [RecordingWorker(spec("gen"), ["ok"])]).run("tâche", synthesize=False)
        self.assertIsNone(r.synthesis)
        self.assertEqual(org.synth_calls, [])

    def test_synthese_vide_non_retournee(self):
        org = ScriptedOrganizer([plan(("gen", "a"))], synthesis="   ")
        r = self.orch(org, [RecordingWorker(spec("gen"), ["ok"])]).run("tâche")
        self.assertIsNone(r.synthesis)

    def test_construction_refusee(self):
        org = ScriptedOrganizer([])
        w = RecordingWorker(spec("gen"), [])
        with self.assertRaises(ValueError):
            Orchestrator(org, [w], None)
        with self.assertRaises(ValueError):
            Orchestrator(org, [], basic_verifier)
        with self.assertRaises(ValueError):
            Orchestrator(org, [w, RecordingWorker(spec("gen"), [])], basic_verifier)
        with self.assertRaises(ValueError):
            Orchestrator(org, [w], basic_verifier, delegation_budget=0)

    def test_nom_de_worker_invalide(self):
        for bad in ["Gen", "gen-1", "", "1gen", "x" * 41]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                spec(bad)


class LoggingTest(Base):
    def test_journal_complet(self):
        org = ScriptedOrganizer([plan(("gen", "a"))])
        r = self.orch(org, [RecordingWorker(spec("gen"), ["sortie"])]).run("tâche", "fiche")
        ev = self.events(r)
        self.assertEqual([e["event"] for e in ev], ["run_start", "plan_accepted", "delegation", "synthesis", "run_end"])
        self.assertEqual(ev[2]["output"], "sortie")
        self.assertEqual(ev[-1]["status"], "ok")
        self.assertEqual(ev[-1]["gemini_requests"], 3)

    def test_journal_sans_contenu(self):
        org = ScriptedOrganizer([plan(("gen", "CONSIGNE_PRIVEE"))])
        r = self.orch(org, [RecordingWorker(spec("gen"), ["SORTIE_PRIVEE"])], log_content=False).run("TACHE_PRIVEE")
        text = r.log_path.read_text(encoding="utf-8")
        for secret in ("CONSIGNE_PRIVEE", "SORTIE_PRIVEE", "TACHE_PRIVEE"):
            self.assertNotIn(secret, text)

    def test_journal_ecrit_meme_en_echec(self):
        org = ScriptedOrganizer([plan(("pirate", "x"))] * 2)
        o = self.orch(org, [RecordingWorker(spec("gen"), [])])
        with self.assertRaises(PlanRejected):
            o.run("tâche")
        files = list(self.logs.glob("run-*.jsonl"))
        self.assertEqual(len(files), 1)
        ev = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(ev[-1]["event"], "run_end")
        self.assertEqual(ev[-1]["status"], "failed")


class EndToEndTest(Base):
    """Vraie chaîne GeminiOrganizer → GeminiWorker → GeminiClient → limiteur, transport simulé."""

    def test_chaine_complete(self):
        clock = {"t": 1_800_000_000.0}
        sleeps = []

        def sleep(dt):
            sleeps.append(dt)
            clock["t"] += dt

        limiter = RateLimiter(GeminiQuota(), Path(self._tmp.name) / "q.json",
                              wall_clock=lambda: clock["t"], sleep=sleep)
        sent = []

        def body(text):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                               "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}).encode()

        def transport(url, headers, data, timeout):
            req = json.loads(data)
            sent.append(req)
            system = req.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
            if system.startswith("Tu es l'organisateur d'une équipe"):
                return HttpResponse(200, {}, body(plan(("generateur", "propose des idées"),
                                                        ("generateur", "propose des idées contrariennes"))))
            if system.startswith("Tu es l'organisateur. Tu reçois"):
                return HttpResponse(200, {}, body("SYNTHESE"))
            n = sum(1 for s in sent if s.get("systemInstruction", {}).get("parts", [{}])[0].get("text") == "tu es generateur")
            if n == 1:
                return HttpResponse(429, {}, b"")  # un 429 sur le premier worker
            return HttpResponse(200, {}, body(json.dumps({"idees": [f"IDEE_{n}"]})))

        client = GeminiClient("k" * 20, "gemini-3.5-flash-lite", limiter, transport=transport)
        orch = Orchestrator(
            GeminiOrganizer(client),
            [GeminiWorker(spec("generateur", IDEAS_SCHEMA), client)],
            basic_verifier,
            logs_dir=self.logs,
            request_counter=lambda: client.requests_sent,
        )
        r = orch.run("Trouver des idées d'application", "Objectif : test")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.synthesis, "SYNTHESE")
        self.assertEqual(r.gemini_requests, 5)  # plan + (429 + retry) + worker 2 + synthèse
        self.assertEqual(limiter.used_today, 5)
        self.assertGreaterEqual(sum(sleeps), 4 * 5.0)  # espacement appliqué entre les 5 requêtes
        # Isolement : la requête du second worker ne contient pas l'idée du premier.
        worker_reqs = [s for s in sent if s["systemInstruction"]["parts"][0]["text"] == "tu es generateur"]
        self.assertNotIn("IDEE_2", json.dumps(worker_reqs[-1], ensure_ascii=False))
        # La synthèse reçoit les deux résultats.
        synth = json.dumps(sent[-1], ensure_ascii=False)
        self.assertIn("IDEE_2", synth)
        self.assertIn("IDEE_3", synth)
        # Sortie JSON demandée au modèle pour le worker à schéma.
        self.assertEqual(worker_reqs[0]["generationConfig"]["responseMimeType"], "application/json")


if __name__ == "__main__":
    unittest.main()

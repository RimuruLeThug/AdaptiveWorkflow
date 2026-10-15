"""Branchement des agents dans l'assemblage (qwen_ui/bootstrap.py).

Doubles uniquement : aucun modèle réel, aucun appel réseau, jamais .env.
"""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import config
from agents import AgentRouter, RoleClient, RoleLLM
from config import GeminiQuota, Settings
from gemini_client import GeminiClient, HttpResponse
from local_llm import OllamaBackend
from organizers.claude import ClaudeOrganizer
from privacy.masker import Masker
from privacy.policy import PolicyError
from privacy.vault import Vault
from profiles.loader import load_profile
from qwen_ui import bootstrap
from qwen_ui.bootstrap import Components, agents_configured, build_components, make_app
from rate_limiter import RateLimiter
from tests.test_gemini_client import KEY, Clock, FakeTransport, ok_body
from tests.test_profiles import MINIMAL, write_profile
from tests.test_promise import FakeLLM

TEST_MODEL = "modele-test-factice"  # nom factice : aucun vrai modèle n'est désigné ni appelé
ROUTE_OK = json.dumps({"action": "tache", "argument": "trouver des idées", "reponse": ""})
FICHE_OK = json.dumps({"objectif": "Un outil de devis", "contraintes": [], "perimetre": [], "hors_perimetre": []})


class FakeClaude:
    model = "sonnet"

    def check_available(self):
        return "abonnement"

    def calls_today(self):
        return 0

    def generate(self, *a, **kw):
        raise AssertionError("Claude ne doit pas être appelé")


class Logger:
    def __init__(self):
        self.events = []

    def event(self, kind, **meta):
        self.events.append(dict(meta, event=kind))


class WiringFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()
        self.limiter = RateLimiter(GeminiQuota(), self.tmp / "gemini_quota.json", wall_clock=self.clock.time,
                                   sleep=self.clock.sleep)
        self.transport = FakeTransport([])
        self.settings = replace(Settings(), state_dir=self.tmp, logs_dir=self.tmp / "logs")
        self.client = GeminiClient(KEY, self.settings.gemini_model, self.limiter, transport=self.transport)
        self.claude = FakeClaude()
        self.llm = FakeLLM([])
        self.format_llm = FakeLLM([])
        self.logger = Logger()
        self.inputs = []
        self.outputs = []
        self.extra_clients = []

    def tearDown(self):
        self._tmp.cleanup()

    def gemini_factory(self, model, limiter):
        c = GeminiClient(KEY, model, limiter, transport=self.transport)
        self.extra_clients.append(c)
        return c

    def input_fn(self, prompt):
        self.outputs.append(prompt)
        if not self.inputs:
            raise AssertionError(f"question inattendue : {prompt}")
        return self.inputs.pop(0)

    def components(self, s=None, profile=None) -> Components:
        return build_components(
            s or self.settings, profile, llm=self.llm, format_llm=self.format_llm, limiter=self.limiter,
            client=self.client, claude=self.claude, input_fn=self.input_fn, output_fn=self.outputs.append,
            app_state_dir=self.tmp / "app", masker_factory=lambda: Masker(Vault()),
            gemini_client_factory=self.gemini_factory, logger=self.logger)

    def app(self, c):
        return make_app(c, input_fn=self.input_fn, output_fn=self.outputs.append)

    def answer(self, *texts):
        self.transport.responses.extend(HttpResponse(200, {}, ok_body(t)) for t in texts)

    def sent_texts(self):
        return [r["body"]["contents"][0]["parts"][0]["text"] for r in self.transport.requests]

    def configured(self, *roles, **kw):
        return replace(self.settings, agent_roles=tuple(roles), gemini_test_model=TEST_MODEL, **kw)


class DefaultUnchangedTest(WiringFixture):
    def test_sans_configuration_rien_n_est_detecte(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = config.load_settings()
        self.assertFalse(agents_configured(s, load_profile("brainstorm")))
        self.assertFalse(agents_configured(s, load_profile("bureautique_fictive")))
        self.assertTrue(agents_configured(replace(s, gemini_test_model=TEST_MODEL), None))
        self.assertTrue(agents_configured(replace(s, agent_roles=(("routage", "gemini"),)), None))

    def test_defaut_memes_objets(self):
        profile = load_profile("brainstorm")
        c = self.components(profile=profile)
        self.assertIsNone(c.router)
        self.assertEqual(c.routed_roles, ())
        self.assertNotIn("role_llms", c.app_kwargs)
        self.assertIs(c.llm, self.llm)
        self.assertIs(c.app_kwargs["format_llm"], self.format_llm)
        self.assertEqual(self.logger.events, [])
        self.assertEqual(self.outputs, [])

        orch = c.orchestrator_factory("gemini", profile)
        self.assertIs(orch.organizer._client, self.client)
        self.assertTrue(all(w._client is self.client for w in orch._workers.values()))
        self.assertEqual(orch.request_counter(), self.client.requests_sent)
        claude_orch = c.orchestrator_factory("claude", profile)
        self.assertIsInstance(claude_orch.organizer, ClaudeOrganizer)
        self.assertIs(claude_orch.organizer._client, self.claude)
        self.assertIs(c.app_kwargs["review_factory"](profile).client, self.client)
        esc = c.app_kwargs["escalator_factory"]("gemini")
        self.assertIs(esc.big_model, self.client)
        self.assertIs(esc.promise_llm, self.llm)
        self.assertIs(c.app_kwargs["escalator_factory"]("claude").big_model, self.claude)

        with mock.patch.object(bootstrap, "repair_json") as repair:
            orch.repairer("{", {})
            c.app_kwargs["review_factory"](profile).repairer("{", {})
        self.assertTrue(all(call.args[2] is self.format_llm for call in repair.call_args_list))

        app = self.app(c)
        self.assertIs(app.llm, self.llm)
        for role in ("routage", "cadrage", "interface"):
            self.assertIs(app._llm(role), self.llm)

    def test_defaut_cadrage_et_routage_par_qwen(self):
        self.llm.answers.extend([json.dumps({"action": "discussion", "argument": "", "reponse": "bonjour"}),
                                 FICHE_OK])
        self.inputs.append("n")
        app = self.app(self.components(profile=load_profile("brainstorm")))
        app.handle("salut")
        app.handle("/fiche un outil de devis")
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.transport.requests, [])
        self.assertFalse((self.tmp / "logs").exists())  # aucun journal d'agents créé

    def test_build_app_passe_par_l_assemblage_sans_reseau(self):
        env = {"ORCH_STATE_DIR": str(self.tmp), "ORCH_LOGS_DIR": str(self.tmp / "logs")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, "load_dotenv"), \
                mock.patch.object(config, "gemini_api_key", return_value=KEY), \
                mock.patch.object(OllamaBackend, "check_available", return_value=None), \
                mock.patch.object(bootstrap, "build_components", wraps=bootstrap.build_components) as spy:
            app = bootstrap.build_app(input_fn=self.input_fn, output_fn=self.outputs.append,
                                      app_state_dir=self.tmp / "app")
        kw = spy.call_args.kwargs
        self.assertIsInstance(kw["llm"], OllamaBackend)
        self.assertIs(app.llm, kw["llm"])
        self.assertIs(app.format_llm, kw["format_llm"])
        self.assertIs(kw["client"].limiter, kw["limiter"])
        self.assertEqual(kw["limiter"].state_path, self.tmp / "gemini_quota.json")
        self.assertEqual(app._role_llms, {})


class ConfiguredRoleTest(WiringFixture):
    def test_role_remplace_par_configuration(self):
        s = self.configured(("routage", "gemini_test"), ("worker", "qwen_interface"))
        c = self.components(s, load_profile("brainstorm"))
        self.assertIsInstance(c.router, AgentRouter)
        self.assertEqual(c.routed_roles, ("worker", "routage"))
        self.assertEqual(c.app_kwargs["role_llms"].keys(), {"routage"})
        self.assertIs(c.app_kwargs["format_llm"], self.format_llm)  # dédup non réaffectée
        orch = c.orchestrator_factory("gemini", None)
        self.assertIs(orch.organizer._client, self.client)  # organisateur non réaffecté
        self.assertTrue(all(isinstance(w._client, RoleClient) and w._client.role == "worker"
                            for w in orch._workers.values()))
        self.assertIn("Agents configurés : worker -> qwen_interface (configuration), "
                      "routage -> gemini_test (configuration)", self.outputs)

        app = self.app(c)
        self.assertIs(app._llm("cadrage"), self.llm)
        self.assertIsInstance(app._llm("routage"), RoleLLM)
        self.answer(ROUTE_OK)
        self.inputs.extend(["o", "n"])  # envoi externe confirmé, commande proposée refusée
        app.handle("écrire à jean.martin@exemple.fr")
        sent = self.sent_texts()
        self.assertEqual(len(sent), 1)
        self.assertNotIn("jean.martin@exemple.fr", sent[0])  # masqué par le masqueur de l'application
        self.assertIn(f"/models/{TEST_MODEL}:", self.transport.requests[0]["url"])
        self.assertIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        self.assertIn("Commande proposée : /tache trouver des idées", self.outputs)
        self.assertEqual(self.llm.calls, [])

        calls = [e for e in self.logger.events if e["event"] == "agent_call"]
        self.assertEqual([(e["role"], e["agent"], e["result"]) for e in calls], [("routage", "gemini_test", "ok")])
        routes = next(e for e in self.logger.events if e["event"] == "agent_routes")
        self.assertEqual(routes["routes"]["routage"]["source"], "configuration")
        self.assertNotIn("jean.martin", json.dumps(self.logger.events, ensure_ascii=False))

    def test_refus_de_confirmation_rien_n_est_envoye(self):
        c = self.components(self.configured(("routage", "gemini_test")))
        self.inputs.append("n")
        self.app(c).handle("bonjour")
        self.assertEqual(self.transport.requests, [])
        self.assertTrue(any("non confirmé" in o for o in self.outputs))

    def test_role_remplace_par_profil(self):
        write_profile(self.tmp, "perso_agents", dict(MINIMAL, nom="perso_agents",
                                                     agents={"roles": {"cadrage": "qwen_format"}}))
        profile = load_profile("perso_agents", self.tmp)
        c = self.components(profile=profile)
        self.assertEqual(c.routed_roles, ("cadrage",))
        self.format_llm.answers.append(FICHE_OK)
        self.inputs.append("n")
        self.app(c).handle("/fiche un outil de devis")
        self.assertEqual(len(self.format_llm.calls), 1)  # Qwen 2 Go déjà vérifié, réutilisé
        self.assertEqual(self.llm.calls, [])
        self.assertTrue(any("Un outil de devis" in o for o in self.outputs))
        call = next(e for e in self.logger.events if e["event"] == "agent_call")
        self.assertEqual((call["role"], call["agent"], call["result"]), ("cadrage", "qwen_format", "ok"))
        self.assertIn("cadrage -> qwen_format (profil)", self.outputs[0])

    def test_roles_client_et_promesse(self):
        s = self.configured(("organisateur", "gemini_test"), ("relecteur", "qwen_format"),
                            ("grand_modele", "gemini_test"), ("promesse", "qwen_format"),
                            ("dedup", "qwen_format"), ("reparation", "qwen_format"))
        c = self.components(s)
        orch = c.orchestrator_factory("gemini", None)
        self.assertEqual(orch.organizer._client.role, "organisateur")
        self.assertTrue(all(w._client is self.client for w in orch._workers.values()))
        self.assertEqual(c.app_kwargs["review_factory"](None).client.role, "relecteur")
        self.assertEqual(c.app_kwargs["format_llm"].role, "dedup")
        esc = c.app_kwargs["escalator_factory"]("gemini")
        self.assertEqual((esc.big_model.role, esc.promise_llm.role), ("grand_modele", "promesse"))
        self.assertIn("gemini_test", esc.provider)
        self.assertIs(c.app_kwargs["escalator_factory"]("claude").big_model, self.claude)  # choix explicite
        with mock.patch.object(bootstrap, "repair_json") as repair:
            orch.repairer("{", {})
        self.assertEqual(repair.call_args.args[2].role, "reparation")

    def test_configuration_invalide_refusee(self):
        with self.assertRaises(ValueError):
            self.components(self.configured(("routage", "agent_inconnu")))


class SingleQueueTest(WiringFixture):
    def test_une_seule_file_et_un_seul_fichier_d_etat(self):
        s = self.configured(("organisateur", "gemini_test"), ("relecteur", "gemini"))
        c = self.components(s)
        reg = c.router.registry
        self.assertIs(reg.get("gemini").client, self.client)  # le GeminiClient de l'application
        self.assertIs(reg.get("gemini_test").client.limiter, self.limiter)
        self.assertIs(reg.gemini_limiter, self.limiter)
        self.answer("plan", "note")
        c.router.call("organisateur", "p")
        c.router.call("relecteur", "p")
        self.assertEqual(self.limiter.used_today, 2)
        self.assertEqual(self.clock.sleeps, [GeminiQuota().min_interval_s])  # espacement commun
        self.assertEqual(c.orchestrator_factory("gemini", None).request_counter(), 2)
        self.assertEqual(len(self.extra_clients), 1)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*quota*.json")), ["gemini_quota.json"])
        self.assertEqual(c.app_kwargs["quota_info"]()[0], 2)

    def test_gemini_test_seul_ne_change_aucun_role(self):
        c = self.components(replace(self.settings, gemini_test_model=TEST_MODEL))
        self.assertEqual(c.routed_roles, ())
        self.assertNotIn("role_llms", c.app_kwargs)
        self.assertIs(c.orchestrator_factory("gemini", None).organizer._client, self.client)


class PersonalDataTest(WiringFixture):
    def test_profil_donnees_perso_refuse_tout_externe(self):
        s = self.configured(("routage", "gemini_test"), ("organisateur", "gemini_test"),
                            ("cadrage", "qwen_interface,gemini"))
        c = self.components(s, load_profile("bureautique_fictive"))
        app = self.app(c)
        app.handle("bonjour")  # aucune question : refus avant tout aperçu
        self.assertTrue(any(o.startswith("Refusé par la politique de données") for o in self.outputs))
        for role in ("organisateur", "cadrage"):
            with self.assertRaises(PolicyError):
                c.router.call(role, "p")
        app.handle("/tache une consigne")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.llm.calls, [])
        refused = [e for e in self.logger.events if e.get("result") == "refuse_politique"]
        self.assertTrue(refused and all(e["external"] for e in refused))

    def test_passage_a_un_profil_perso_en_cours_de_session(self):
        c = self.components(self.configured(("routage", "gemini_test")), load_profile("brainstorm"))
        app = self.app(c)
        app.handle("/profil bureautique_fictive")
        app.handle("bonjour")
        self.assertTrue(any(o.startswith("Refusé par la politique de données") for o in self.outputs))
        self.assertEqual(self.transport.requests, [])


if __name__ == "__main__":
    unittest.main()

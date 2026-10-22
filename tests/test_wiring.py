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
from agents import ROLES, AgentError, AgentRouter, RoleClient, RoleLLM
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
from tests.test_gemini_client import KEY, Clock, FakeTransport, err_body, ok_body
from tests.test_profiles import MINIMAL, write_profile
from tests.test_promise import FakeLLM

TEST_MODEL = "modele-test-factice"  # nom factice : aucun vrai modèle n'est désigné ni appelé
ROUTE_OK = json.dumps({"action": "tache", "argument": "trouver des idées", "reponse": ""})
FICHE_OK = json.dumps({"objectif": "Un outil de devis", "contraintes": [], "perimetre": [], "hors_perimetre": []})
# Langage libre : reformulation locale par Qwen (accord Gemini refusé ou profil perso).
REFORM_OK = json.dumps({"demande": "demande reformulée", "intention": "", "objet": "", "contraintes": [],
                        "ambiguites": []})
# Données fictives. Le code postal seul (sans ville) est masqué avant tout envoi externe mais ne
# déclenche pas le profil restrictif : il permet de couvrir le masquage du routage de bout en bout.
EMAIL = "jean.martin@exemple.fr"
CP = "75011"
ACCORD = ("Autoriser Claude pour cette session : reformulation de vos messages et autres rôles en texte libre "
          "(routage, cadrage, résumés...) ; texte masqué, contrôle de fuite avant chaque envoi. Sinon, "
          "reformulation locale et confirmation de chaque envoi ? (o/n) ")
DISCUSSION = json.dumps({"action": "discussion", "argument": "", "reponse": "bonjour"})


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
        # Ces tests couvrent le langage libre à commandes proposées (MODE_CONVERSATION=0) ; le mode
        # conversation, actif par défaut, a ses propres tests (tests/test_conversation.py).
        self.settings = replace(Settings(), state_dir=self.tmp, logs_dir=self.tmp / "logs", mode_conversation=False)
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


class DefaultV3Test(WiringFixture):
    def test_sans_configuration_rien_n_est_detecte(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = config.load_settings()
        self.assertFalse(agents_configured(s, load_profile("brainstorm")))
        self.assertFalse(agents_configured(s, load_profile("bureautique_fictive")))
        self.assertTrue(agents_configured(replace(s, gemini_test_model=TEST_MODEL), None))
        self.assertTrue(agents_configured(replace(s, agent_roles=(("routage", "gemini"),)), None))

    def test_defaut_v3_tous_les_roles_routes(self):
        profile = load_profile("brainstorm")
        c = self.components(profile=profile)
        self.assertIsInstance(c.router, AgentRouter)
        self.assertEqual(c.routed_roles, tuple(ROLES))
        for role, r in c.router.routes.items():
            self.assertEqual((r.agents(), r.source), (["claude_haiku", "qwen"], "défaut"), role)
        self.assertEqual(c.app_kwargs["role_llms"].keys(), {"interface", "cadrage", "routage"})
        self.assertEqual(c.app_kwargs["format_llm"].role, "dedup")
        self.assertEqual((c.app_kwargs["organizer"], c.app_kwargs["big_model"]), ("agents", "agents"))
        self.assertFalse(any(o.startswith("Agents configurés") for o in self.outputs))

        orch = c.orchestrator_factory("agents", profile)
        self.assertEqual(orch.organizer._client.role, "organisateur")
        self.assertTrue(all(w._client.role == "worker" for w in orch._workers.values()))
        self.assertIs(c.orchestrator_factory("gemini", profile).organizer._client, self.client)  # choix explicite
        claude_orch = c.orchestrator_factory("claude", profile)
        self.assertIsInstance(claude_orch.organizer, ClaudeOrganizer)
        self.assertIs(claude_orch.organizer._client, self.claude)
        self.assertEqual(c.app_kwargs["review_factory"](profile).client.role, "relecteur")
        esc = c.app_kwargs["escalator_factory"]("agents")
        self.assertEqual((esc.big_model.role, esc.promise_llm.role), ("grand_modele", "promesse"))
        self.assertIn("claude_haiku", esc.provider)
        self.assertIs(c.app_kwargs["escalator_factory"]("gemini").big_model, self.client)
        self.assertIs(c.app_kwargs["escalator_factory"]("claude").big_model, self.claude)

        with mock.patch.object(bootstrap, "repair_json") as repair:
            orch.repairer("{", {})
            c.app_kwargs["review_factory"](profile).repairer("{", {})
        self.assertTrue(all(call.args[2].role == "reparation" for call in repair.call_args_list))

        app = self.app(c)
        self.assertEqual((app.organizer, app.big_model), ("agents", "agents"))
        for role in ("routage", "cadrage", "interface"):
            self.assertEqual(app._llm(role).role, role)

    def test_un_seul_modele_qwen_pour_tous_les_usages_locaux(self):
        c = self.components()
        reg = c.router.registry
        self.assertIs(reg.get("qwen").llm, self.llm)
        self.assertIs(reg.get("qwen_interface").llm, self.llm)
        self.assertIs(reg.get("qwen_format").llm, self.format_llm)  # backend fourni par l'appelant
        c2 = build_components(self.settings, None, llm=self.llm, format_llm=None, limiter=self.limiter,
                              client=self.client, claude=self.claude, output_fn=self.outputs.append,
                              app_state_dir=self.tmp / "app", logger=self.logger)
        self.assertIs(c2.router.registry.get("qwen_format").llm, self.llm)  # V3 : None = le même modèle

    def test_claude_absent_repli_qwen_sans_confirmation_inutile(self):
        # Claude Code introuvable (garde-fou des tests) : claude_haiku est sauté AVANT tout aperçu,
        # chaque rôle passe à son repli local ; l'accord de session est demandé une fois.
        self.llm.answers.extend([REFORM_OK, DISCUSSION, FICHE_OK])
        self.inputs.extend(["n", "n"])  # accord de session refusé, fiche non validée
        app = self.app(self.components(profile=load_profile("brainstorm")))
        app.handle("salut")
        app.handle("/fiche un outil de devis")
        self.assertEqual(len(self.llm.calls), 3)  # reformulation locale, routage, cadrage
        self.assertEqual(self.outputs.count(ACCORD), 1)
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        self.assertEqual(self.transport.requests, [])
        self.assertIn("Cadrage par claude_haiku (Claude via Claude Code, abonnement)…", self.outputs)
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertIn(("routage", "claude_haiku", "indisponible"), calls)
        self.assertIn(("routage", "qwen", "ok"), calls)

    def test_claude_haiku_avec_accord_de_session_aucune_confirmation_par_envoi(self):
        sent = []

        class Haiku:
            model = "haiku"

            def executable(self):
                return "claude"

            def generate(self, prompt, **kw):
                sent.append(prompt)
                from claude_cli import ClaudeResult
                text = REFORM_OK if "Message de l'utilisateur" in prompt else DISCUSSION
                return ClaudeResult(text=text)

        c = build_components(
            self.settings, load_profile("brainstorm"), llm=self.llm, format_llm=None, limiter=self.limiter,
            client=self.client, claude=self.claude, input_fn=self.input_fn, output_fn=self.outputs.append,
            app_state_dir=self.tmp / "app", masker_factory=lambda: Masker(Vault()),
            claude_client_factory=lambda model: Haiku(), logger=self.logger)
        self.inputs.append("o")  # accord de session : une seule question
        app = self.app(c)
        app.handle(f"salut, une idée pour le devis du {CP}")
        self.assertEqual(self.outputs.count(ACCORD), 1)
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)  # couvert par l'accord
        self.assertEqual(len(sent), 2)  # reformulation + routage
        self.assertTrue(all(CP not in p and "[CODE_POSTAL_1]" in p for p in sent))  # masqué avant envoi
        self.assertEqual(self.llm.calls, [])
        self.assertIn("bonjour", self.outputs)
        app.handle("/accord non")
        self.assertFalse(c.router.has_session_consent("routage", "claude"))
        app.handle("/accord oui")  # accord explicite : tous les rôles en texte libre confiés à Claude
        for role in ("reformulation", "routage", "cadrage", "dedup", "reparation"):
            self.assertTrue(c.router.has_session_consent(role, "claude"), role)
        self.assertFalse(c.router.has_session_consent("worker", "claude"))

    def test_tache_complete_sans_gemini_ni_claude_tout_en_local(self):
        # Claude absent : organisateur, worker et synthèse passent au Qwen local ; aucun message de
        # quota Gemini, aucune requête Gemini, aucune confirmation (rien ne part).
        plan = json.dumps({"tasks": [{"worker": "analyste", "instruction": "analyse le marché"}]})
        self.llm.answers.extend([plan, "idées du worker", "synthèse finale"])
        app = self.app(self.components(profile=load_profile("brainstorm")))
        app.handle("/tache trouve 3 idées")
        text = "\n".join(self.outputs)
        self.assertIn("Envoi à l'organisateur claude_haiku (Claude via Claude Code, abonnement)…", self.outputs)
        self.assertNotIn("requêtes Gemini aujourd'hui", text)
        self.assertEqual(self.transport.requests, [])
        self.assertIsNotNone(app.last_report)
        self.assertEqual(app.last_report.accepted[0].text, "idées du worker")
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertIn(("organisateur", "qwen", "ok"), calls)
        self.assertIn(("worker", "qwen", "ok"), calls)

    def test_sans_cle_gemini_agent_saute_avant_tout_apercu(self):
        s = self.configured(("routage", "gemini,qwen"))
        c = build_components(s, None, llm=self.llm, format_llm=None, limiter=self.limiter, client=None,
                             claude=self.claude, input_fn=self.input_fn, output_fn=self.outputs.append,
                             app_state_dir=self.tmp / "app", logger=self.logger)
        self.llm.answers.append(ROUTE_OK)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(config, "load_dotenv"):
            res = c.router.call("routage", "salut")
        self.assertEqual(res.agent, "qwen")
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)  # pas d'aperçu inutile
        self.assertEqual(c.router.journal[0]["result"], "indisponible")
        self.assertIn("GEMINI_API_KEY", c.router.journal[0]["detail"])

    def test_quota_gemini_annonce_seulement_si_un_role_utilise_gemini(self):
        c = self.components(self.configured(("relecteur", "gemini")))
        self.assertTrue(c.app_kwargs["role_uses_gemini"]("relecteur"))
        self.assertFalse(c.app_kwargs["role_uses_gemini"]("organisateur"))
        self.assertIn("gemini (Gemini, quota commun)", c.app_kwargs["role_label"]("relecteur"))

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
        self.assertEqual(kw["llm"].model, "qwen3.5:9b-q4_K_M")
        self.assertIs(app.llm, kw["llm"])
        self.assertIsNone(kw["format_llm"])  # un seul modèle local
        self.assertEqual(app.format_llm.role, "dedup")
        self.assertIs(kw["client"].limiter, kw["limiter"])
        self.assertEqual(kw["limiter"].state_path, self.tmp / "gemini_quota.json")
        self.assertEqual(set(app._role_llms), {"interface", "cadrage", "routage"})
        self.assertTrue(any(o.startswith("Attention : Claude indisponible") for o in self.outputs))

    def test_build_app_sans_cle_gemini_ni_ollama(self):
        from local_llm import LocalLLMError
        env = {"ORCH_STATE_DIR": str(self.tmp), "ORCH_LOGS_DIR": str(self.tmp / "logs"),
               "QWEN_INTERFACE_MODEL": "ancien:tag"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, "load_dotenv"), \
                mock.patch.object(OllamaBackend, "check_available", side_effect=LocalLLMError("Ollama éteint")), \
                mock.patch.object(bootstrap, "build_components", wraps=bootstrap.build_components) as spy:
            app = bootstrap.build_app(input_fn=self.input_fn, output_fn=self.outputs.append,
                                      app_state_dir=self.tmp / "app")
        self.assertIsNone(spy.call_args.kwargs["client"])
        self.assertTrue(any(o.startswith("Attention : Qwen local indisponible") for o in self.outputs))
        self.assertTrue(any("QWEN_INTERFACE_MODEL" in o and "QWEN_MODEL" in o for o in self.outputs))
        self.assertEqual(app.organizer, "agents")
        app.handle("/organisateur gemini")  # client direct impossible sans clé : message clair, pas de plantage
        app.handle("/tache une consigne")
        self.assertTrue(any("GEMINI_API_KEY" in o for o in self.outputs))


class ConfiguredRoleTest(WiringFixture):
    def test_role_remplace_par_configuration(self):
        s = self.configured(("routage", "gemini_test"), ("worker", "qwen_interface"))
        c = self.components(s, load_profile("brainstorm"))
        self.assertIsInstance(c.router, AgentRouter)
        self.assertEqual((c.router.routes["routage"].agents(), c.router.routes["routage"].source),
                         (["gemini_test"], "configuration"))
        self.assertEqual(c.router.routes["dedup"].agents(), ["claude_haiku", "qwen"])  # non réaffectée
        orch = c.orchestrator_factory("agents", None)
        self.assertTrue(all(isinstance(w._client, RoleClient) and w._client.role == "worker"
                            for w in orch._workers.values()))
        self.assertIn("Agents configurés : worker -> qwen_interface (configuration), "
                      "routage -> gemini_test (configuration)", self.outputs)

        app = self.app(c)
        self.assertIsInstance(app._llm("routage"), RoleLLM)
        self.answer(ROUTE_OK)
        self.llm.answers.append(REFORM_OK)
        # accord de reformulation refusé, envoi externe du routage confirmé, commande proposée refusée
        self.inputs.extend(["n", "o", "n"])
        app.handle(f"écrire une relance pour le devis du {CP}")
        self.assertFalse(app._personal())  # donnée non déclencheuse : le routage externe a bien lieu
        sent = self.sent_texts()
        self.assertEqual(len(sent), 1)
        self.assertNotIn(CP, sent[0])  # masqué par le masqueur de l'application
        self.assertIn("[CODE_POSTAL_1]", sent[0])
        preview = next(o for o in self.outputs if "Destinataire" in o)
        self.assertNotIn(CP, preview)
        self.assertIn(f"/models/{TEST_MODEL}:", self.transport.requests[0]["url"])
        self.assertIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        self.assertIn("Commande proposée : /tache trouver des idées", self.outputs)
        self.assertEqual(len(self.llm.calls), 1)  # reformulation locale seulement

        calls = [e for e in self.logger.events if e["event"] == "agent_call" and e["role"] == "routage"]
        self.assertEqual([(e["role"], e["agent"], e["result"]) for e in calls], [("routage", "gemini_test", "ok")])
        routes = next(e for e in self.logger.events if e["event"] == "agent_routes")
        self.assertEqual(routes["routes"]["routage"]["source"], "configuration")
        logged = json.dumps(self.logger.events, ensure_ascii=False)
        self.assertNotIn("relance", logged)  # métadonnées seulement
        self.assertNotIn(CP, logged)

    def test_routeur_masque_l_email_avant_envoi_apercu_et_journal(self):
        # Donnée déclencheuse (email) : testée directement sur le routeur d'agents, sans passer par le
        # texte libre (qui imposerait le profil restrictif et bloquerait tout envoi).
        c = self.components(self.configured(("routage", "gemini_test")))
        self.answer(ROUTE_OK)
        self.inputs.append("o")  # envoi externe confirmé après aperçu
        res = c.router.call("routage", f"écrire à {EMAIL}")
        sent = self.sent_texts()
        self.assertEqual(len(sent), 1)
        self.assertNotIn(EMAIL, sent[0])
        self.assertIn("[EMAIL_1]", sent[0])
        preview = next(o for o in self.outputs if "Destinataire" in o)
        self.assertNotIn(EMAIL, preview)
        self.assertIn("[EMAIL_1]", preview)
        self.assertNotIn(EMAIL, json.dumps(self.logger.events, ensure_ascii=False))
        self.assertNotIn(EMAIL, json.dumps(c.router.journal, ensure_ascii=False))
        self.assertEqual(res.agent, "gemini_test")

    def test_refus_de_confirmation_rien_n_est_envoye(self):
        c = self.components(self.configured(("routage", "gemini_test")))
        self.llm.answers.append(REFORM_OK)
        self.inputs.extend(["n", "n"])  # accord de reformulation refusé, envoi du routage refusé
        self.app(c).handle("bonjour")
        self.assertEqual(self.transport.requests, [])
        self.assertTrue(any("non confirmé" in o for o in self.outputs))

    def test_role_remplace_par_profil(self):
        write_profile(self.tmp, "perso_agents", dict(MINIMAL, nom="perso_agents",
                                                     agents={"roles": {"cadrage": "qwen_format"}}))
        profile = load_profile("perso_agents", self.tmp)
        c = self.components(profile=profile)
        self.assertEqual(c.router.routes["cadrage"].source, "profil")
        self.format_llm.answers.append(FICHE_OK)
        self.inputs.append("n")
        self.app(c).handle("/fiche un outil de devis")
        self.assertEqual(len(self.format_llm.calls), 1)  # Qwen 2.5 3B déjà vérifié, réutilisé
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
        orch = c.orchestrator_factory("agents", None)
        self.assertEqual(orch.organizer._client.role, "organisateur")
        self.assertTrue(all(w._client.role == "worker" for w in orch._workers.values()))
        self.assertEqual(c.app_kwargs["review_factory"](None).client.role, "relecteur")
        self.assertEqual(c.app_kwargs["format_llm"].role, "dedup")
        esc = c.app_kwargs["escalator_factory"]("agents")
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
        self.assertTrue(all(r.source == "défaut" for r in c.router.routes.values()))
        self.assertFalse(any(o.startswith("Agents configurés") for o in self.outputs))
        self.assertIs(c.orchestrator_factory("gemini", None).organizer._client, self.client)


class PersonalDataTest(WiringFixture):
    def test_profil_donnees_perso_tout_en_local_meme_avec_configuration_externe(self):
        s = self.configured(("routage", "gemini_test"), ("organisateur", "gemini_test"),
                            ("cadrage", "qwen_interface,gemini"))
        c = self.components(s, load_profile("bureautique_fictive"))
        app = self.app(c)
        self.llm.answers.extend([REFORM_OK, DISCUSSION])  # reformulation et routage locaux
        app.handle("bonjour")  # aucune question : rien ne peut partir
        self.assertIn("bonjour", self.outputs)
        self.llm.answers.extend(["plan local", "fiche locale"])
        self.assertEqual(c.router.call("organisateur", "p").agent, "qwen")
        self.assertEqual(c.router.call("cadrage", "p").agent, "qwen_interface")
        self.assertEqual(self.transport.requests, [])
        self.assertNotIn(ACCORD, self.outputs)  # jamais d'accord demandé en profil perso
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        refused = [e for e in self.logger.events if e.get("result") == "refuse_politique"]
        self.assertTrue(refused and all(e["external"] for e in refused))
        self.assertIn("qwen (local)", c.app_kwargs["role_label"]("organisateur"))

    def test_passage_a_un_profil_perso_en_cours_de_session(self):
        c = self.components(self.configured(("routage", "gemini_test")), load_profile("brainstorm"))
        app = self.app(c)
        app.handle("/profil bureautique_fictive")
        self.assertTrue(any("traités par le Qwen local" in o for o in self.outputs))
        self.llm.answers.extend([REFORM_OK, DISCUSSION])
        app.handle("bonjour")
        self.assertEqual(self.transport.requests, [])
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertIn(("routage", "gemini_test", "refuse_politique"), calls)
        self.assertIn(("routage", "qwen", "ok"), calls)

    def test_donnee_personnelle_dans_le_texte_libre_bascule_tout_en_local(self):
        self.llm.answers.extend([REFORM_OK, DISCUSSION])
        app = self.app(self.components(profile=load_profile("brainstorm")))
        app.handle("relance M. Dupont pour la facture")  # nom avec civilité : déclencheur V3
        self.assertTrue(app._personal())
        self.assertEqual(app.profile.name, "bureautique_fictive")
        self.assertEqual(len(self.llm.calls), 2)
        self.assertNotIn(ACCORD, self.outputs)


class ReviewFixesTest(WiringFixture):
    def test_reparation_externe_du_texte_libre_demande_confirmation(self):
        # Qwen (routage par défaut) renvoie un JSON invalide reprenant le texte libre ;
        # le rôle « reparation » confié à Gemini ne l'envoie qu'après confirmation.
        # (Une donnée personnelle dans le texte libre imposerait le profil restrictif : voir
        # tests/test_langage_libre.py ; ici le texte n'en contient pas.)
        c = self.components(self.configured(("routage", "qwen_interface"), ("reparation", "gemini_test")))
        self.llm.answers.extend([REFORM_OK, f"Commande : tache, écrire une relance pour le devis du {CP}"])
        self.inputs.extend(["n", "n"])  # accord de reformulation refusé, envoi externe refusé
        app = self.app(c)
        app.handle(f"écrire une relance pour le devis du {CP}")
        self.assertFalse(app._personal())
        self.assertEqual(self.transport.requests, [])
        self.assertIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        preview = next(o for o in self.outputs if "Destinataire" in o)
        self.assertIn("« gemini_test »", preview)
        self.assertIn(TEST_MODEL, preview)
        self.assertNotIn(CP, preview)  # masqué dans l'aperçu de la réparation
        self.assertNotIn(CP, json.dumps(self.logger.events, ensure_ascii=False))
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertIn(("reparation", "gemini_test", "apercu_refuse"), calls)

    def test_reparation_externe_masque_l_email_dans_l_apercu(self):
        # Même scénario, donnée déclencheuse testée sur le routeur : Qwen (routage) renvoie un JSON
        # invalide contenant l'email ; la réparation par gemini_test n'en montre que la version masquée.
        c = self.components(self.configured(("routage", "qwen_interface"), ("reparation", "gemini_test")))
        self.llm.answers.append(f"Commande : tache, écrire à {EMAIL}")
        self.inputs.append("n")  # envoi externe de la réparation refusé
        with self.assertRaises(AgentError):  # réparation refusée : rien n'est envoyé
            c.router.call("routage", f"écrire à {EMAIL}", schema={"type": "object", "required": ["action"]})
        self.assertEqual(self.transport.requests, [])
        preview = next(o for o in self.outputs if "Destinataire" in o)
        self.assertIn("« gemini_test »", preview)
        self.assertNotIn(EMAIL, preview)
        self.assertNotIn(EMAIL, json.dumps(self.logger.events, ensure_ascii=False))
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertIn(("reparation", "gemini_test", "apercu_refuse"), calls)

    def test_quota_journalier_message_dedie(self):
        c = self.components(self.configured(("routage", "gemini_test,gemini")))
        details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
        self.transport.responses.append(HttpResponse(429, {}, err_body(429, "RESOURCE_EXHAUSTED", details=details)))
        self.llm.answers.append(REFORM_OK)
        self.inputs.extend(["n", "o"])  # accord de reformulation refusé, envoi du routage confirmé
        self.app(c).handle("bonjour")
        self.assertTrue(any(o.startswith("Quota Gemini épuisé pour aujourd'hui") for o in self.outputs))
        self.assertEqual(len(self.transport.requests), 1)  # aucun repli vers l'autre agent Gemini
        self.assertEqual(len(self.llm.calls), 1)  # la seule reformulation, locale


if __name__ == "__main__":
    unittest.main()

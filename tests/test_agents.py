"""Agents interchangeables : doubles de test uniquement, aucun modèle réel, aucun réseau."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import config
from agents import (ROLES, Agent, AgentDailyQuota, AgentEmptyResponse, AgentRateLimited, AgentRegistry,
                    AgentRouter,
                    AgentSchemaError, AgentSpec, AgentTimeout, AgentUnavailable, ClaudeAgent,
                    ExternalSendBlocked, GeminiAgent, LocalAgent, LocalOpenAIBackend, NoAgentSucceeded,
                    RawReply, RoleCallError, RoleClient, RoleDailyQuotaError, RoleLLM, RoleRoute,
                    build_registry, build_router,
                    parse_declarations, resolve_routes, validate_profile_agents)
from claude_cli import ClaudeCodeClient
from config import GeminiQuota, Settings
from gemini_client import GeminiClient, GeminiDailyQuotaError, GeminiError, HttpResponse
from local_llm import LocalLLMError
from orchestrator import Orchestrator, RunLogger
from organizers.gemini import GeminiOrganizer
from privacy.detectors import Finding
from privacy.masker import LeakBlocked, Masker, PreviewRefused
from privacy.policy import PolicyError
from privacy.vault import Vault
from profiles.loader import ProfileError, load_profile
from qwen_ui.fiche import FICHE_SCHEMA, cadrer
from qwen_ui.router import ROUTE_SCHEMA, route
from rate_limiter import DailyQuotaExhausted, RateLimiter
from review import ReviewPanel
from tests.test_gemini_client import KEY, Clock, FakeTransport, err_body, ok_body
from tests.test_profiles import MINIMAL, write_profile
from tests.test_promise import CONTRACT, FakeLLM, version
from verify.basic import basic_verifier
from verify.promise import PROMISE_SCHEMA, check_promise
from verify.repair import repair_json
from workers.base import WorkerSpec
from workers.dedup import GROUPS_SCHEMA, Idea, deduplicate
from workers.gemini import GeminiWorker

TEST_MODEL = "modele-test-factice"  # nom factice : aucun vrai modèle n'est désigné ni appelé
ROOT = Path(__file__).resolve().parent.parent


class FakeAgent(Agent):
    """Agent scripté : chaîne = réponse, exception = échec."""

    def __init__(self, name, provider="ollama", model="factice", answers=()):
        super().__init__(AgentSpec(name, provider, model))
        self.answers = list(answers)
        self.prompts = []

    def _generate(self, prompt, *, system, temperature, schema, json_output):
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return RawReply(a)


def fake_registry(**answers):
    """Registre où chaque agent intégré est remplacé par un double (même nom, même fournisseur)."""
    providers = {"qwen_interface": "ollama", "qwen_format": "ollama", "gemini": "gemini",
                 "gemini_test": "gemini", "claude": "claude"}
    reg = AgentRegistry()
    fakes = {}
    for name, provider in providers.items():
        fakes[name] = FakeAgent(name, provider, answers=answers.get(name, ()))
        reg.register(fakes[name])
    return reg, fakes


def settings(**kw):
    return replace(Settings(), **kw)


def router_for(reg, s=None, profile=None, **kw):
    s = s or settings()
    return AgentRouter(reg, resolve_routes(s, profile, reg.names()), **kw)


ROUTE_OK = json.dumps({"action": "tache", "argument": "trouver des idées d'application", "reponse": ""})


class GeminiFixture(unittest.TestCase):
    """Vrais GeminiClient / GeminiAgent / RateLimiter, transport simulé."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()
        self.limiter = RateLimiter(GeminiQuota(), self.tmp / "q.json", wall_clock=self.clock.time,
                                   sleep=self.clock.sleep)
        self.calls_through_queue = 0
        real_call = self.limiter.call

        def spy(fn):
            self.calls_through_queue += 1
            return real_call(fn)

        self.limiter.call = spy
        self.transport = FakeTransport([])
        self.settings = settings(state_dir=self.tmp, logs_dir=self.tmp, gemini_test_model=TEST_MODEL)

    def tearDown(self):
        self._tmp.cleanup()

    def factory(self, model, limiter):
        return GeminiClient(KEY, model, limiter, transport=self.transport)

    def registry(self, s=None, **kw):
        return build_registry(s or self.settings, gemini_limiter=self.limiter, gemini_client_factory=self.factory,
                              **kw)

    def answer(self, *texts):
        self.transport.responses.extend(t if isinstance(t, (HttpResponse, Exception)) else
                                        HttpResponse(200, {}, ok_body(t)) for t in texts)

    def sent_texts(self):
        return [r["body"]["contents"][0]["parts"][0]["text"] for r in self.transport.requests]


# --------------------------------------------------------------------- contrat


class ContractTest(GeminiFixture):
    def test_reponse_valide_normalisee(self):
        a = FakeAgent("qwen_interface", answers=['{"identique": true, "explication": "même résultat"}'])
        r = a.call("p", schema=PROMISE_SCHEMA)
        self.assertEqual(r.data, {"identique": True, "explication": "même résultat"})
        self.assertIsNone(r.repair)
        self.assertEqual((r.agent, r.provider, r.model), ("qwen_interface", "ollama", "factice"))
        self.assertGreaterEqual(r.latency_s, 0.0)

    def test_json_invalide_repare_par_qwen_25_3b_factice(self):
        qwen_format = FakeLLM(['{"identique": false, "explication": "périmètre réduit"}'])
        a = FakeAgent("gemini_test", "gemini", answers=['{"identique": false, "explication": "périmètre réduit"'])
        r = a.call("p", schema=PROMISE_SCHEMA, repair_llm=qwen_format)
        self.assertEqual(r.repair, "qwen")
        self.assertEqual(r.data["identique"], False)
        self.assertEqual(qwen_format.calls[0]["schema"], PROMISE_SCHEMA)

    def test_reparation_deterministe_sans_modele(self):
        a = FakeAgent("x", answers=['Voici : {"identique": true, "explication": "ok",}'])
        self.assertEqual(a.call("p", schema=PROMISE_SCHEMA).repair, "deterministe")

    def test_hors_schema(self):
        a = FakeAgent("x", answers=['{"autre": 1}'])
        with self.assertRaises(AgentSchemaError):
            a.call("p", schema=PROMISE_SCHEMA, repair_llm=FakeLLM(['{"identique": "peut-être"}']))

    def test_reponse_vide(self):
        with self.assertRaises(AgentEmptyResponse):
            FakeAgent("x", answers=["   "]).call("p")

    def test_local_timeout(self):
        llm = FakeLLM([LocalLLMError("Ollama ne répond pas (TimeoutError: timed out).")])
        with self.assertRaises(AgentTimeout):
            LocalAgent(AgentSpec("qwen_interface", "ollama", "q"), llm).call("p")

    def test_local_modele_absent_indisponible(self):
        llm = FakeLLM([LocalLLMError("Modèle « q » absent d'Ollama.")])
        with self.assertRaises(AgentUnavailable):
            LocalAgent(AgentSpec("qwen_interface", "ollama", "q"), llm).call("p")

    def test_local_agent_jamais_externe(self):
        with self.assertRaises(ValueError):
            LocalAgent(AgentSpec("x", "gemini", "m"), FakeLLM([]))

    def test_gemini_timeout(self):
        self.answer(GeminiError("Réseau : impossible de joindre l'API Gemini (TimeoutError: timed out)"))
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL), self.factory, self.limiter)
        with self.assertRaises(AgentTimeout):
            agent.call("p")

    def test_gemini_http_429_persistant(self):
        self.answer(*[HttpResponse(429, {}, err_body(429, "RESOURCE_EXHAUSTED"))] * 3)
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL), self.factory, self.limiter)
        with self.assertRaises(AgentRateLimited):
            agent.call("p")
        self.assertEqual(self.limiter.used_today, 3)  # 1 + 2 retries, tous dans le budget

    def test_gemini_reponse_vide(self):
        self.answer(HttpResponse(200, {}, json.dumps({"candidates": []}).encode()))
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL), self.factory, self.limiter)
        with self.assertRaises(AgentEmptyResponse):
            agent.call("p")

    def test_gemini_schema_rappele_dans_la_consigne(self):
        self.answer('{"identique": true, "explication": "x"}')
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL), self.factory, self.limiter)
        agent.call("p", system="sys", schema=PROMISE_SCHEMA)
        body = self.transport.requests[0]["body"]
        self.assertIn(json.dumps(PROMISE_SCHEMA, ensure_ascii=False), body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_claude_adaptateur(self):
        def runner(cmd, **kw):
            if cmd[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"authMethod": "claude.ai"}), "")
            raise subprocess.TimeoutExpired(cmd, 1)

        client = ClaudeCodeClient(executable="claude", runner=runner, timeout_s=1)
        with self.assertRaises(AgentTimeout):
            ClaudeAgent(AgentSpec("claude", "claude", "sonnet"), client).call("p")

    def test_claude_reponse_ok(self):
        def runner(cmd, **kw):
            if cmd[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"authMethod": "claude.ai"}), "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": '{"identique": true, "explication": "x"}'}), "")

        client = ClaudeCodeClient(executable="claude", runner=runner)
        r = ClaudeAgent(AgentSpec("claude", "claude", "sonnet"), client).call("p", schema=PROMISE_SCHEMA)
        self.assertTrue(r.data["identique"])


class LocalOpenAIBackendTest(unittest.TestCase):
    """Backend local alternatif : jamais lancé, transport simulé."""

    def transport(self, responses):
        self.requests = []

        def t(method, url, body, timeout):
            self.requests.append((method, url, json.loads(body) if body else None))
            return responses.pop(0)
        return t

    def test_chat_et_schema(self):
        body = json.dumps({"choices": [{"message": {"content": '<think>x</think>{"identique": true, "explication": "x"}'}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode()
        llm = LocalOpenAIBackend("modele-local", host="http://127.0.0.1:8080", transport=self.transport([(200, body)]))
        doc, reply = llm.ask_json("p", PROMISE_SCHEMA)
        self.assertTrue(doc["identique"])
        method, url, payload = self.requests[0]
        self.assertEqual(url, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], PROMISE_SCHEMA)
        self.assertEqual(reply.input_tokens, 5)

    def test_erreur_http(self):
        llm = LocalOpenAIBackend("m", transport=self.transport([(500, b'{"error": {"message": "panne"}}')]))
        with self.assertRaises(LocalLLMError):
            llm.ask("p")

    def test_liste_des_modeles(self):
        llm = LocalOpenAIBackend("m", host="http://localhost:1234",
                                 transport=self.transport([(200, b'{"data": [{"id": "m"}]}')]))
        self.assertEqual(llm.list_models(), ["m"])

    def test_hote_distant_refuse(self):
        for host in ("http://192.168.1.10:8080", "https://api.exemple.fr"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                LocalOpenAIBackend("m", host=host)

    def test_remplace_qwen_par_declaration(self):
        body = json.dumps({"choices": [{"message": {"content": ROUTE_OK}}]}).encode()
        s = settings(agent_declarations=json.dumps({"alt_local": {"fournisseur": "local_openai", "modele": "m",
                                                                  "hote": "http://127.0.0.1:8080"}}),
                     agent_roles=(("routage", "alt_local"),))
        reg = build_registry(s, local_transport=self.transport([(200, body)]))
        router = router_for(reg, s)
        self.assertEqual(route(router.role_llm("routage"), "idées ?").command.name, "tache")
        self.assertEqual(router.journal[-1]["provider"], "local_openai")


# ------------------------------------------------------------- configuration


class ConfigurationTest(GeminiFixture):
    def test_defauts_du_projet_inchanges(self):
        routes = resolve_routes(settings())
        for role in ("interface", "cadrage", "routage", "promesse"):
            self.assertEqual(routes[role].agents(), ["qwen_interface"], role)
        for role in ("format", "dedup", "reparation"):
            self.assertEqual(routes[role].agents(), ["qwen_format"], role)
        for role in ("organisateur", "worker", "relecteur", "grand_modele"):
            self.assertEqual(routes[role].agents(), ["gemini"], role)
        self.assertEqual(resolve_routes(settings(big_model_provider="claude"))["grand_modele"].agent, "claude")
        self.assertEqual(set(ROLES), set(config.AGENT_ROLES))

    def test_gemini_test_vide_par_defaut_et_indisponible(self):
        self.assertEqual(Settings().gemini_test_model, "")
        with mock.patch.dict(os.environ, {"GEMINI_TEST_MODEL": ""}):
            s = replace(config.load_settings(), state_dir=self.tmp)
        self.assertEqual(s.gemini_test_model, "")
        agent = self.registry(s).get("gemini_test")
        self.assertIn("GEMINI_TEST_MODEL", agent.unavailable_reason())
        with self.assertRaises(AgentUnavailable):
            agent.call("p")
        self.assertEqual(self.transport.requests, [])

    def test_env_example_nom_sans_valeur(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        self.assertIn("GEMINI_TEST_MODEL=", lines)

    def test_variables_d_environnement(self):
        env = {"GEMINI_TEST_MODEL": TEST_MODEL, "AGENT_ROUTAGE": "gemini_test, qwen_interface",
               "AGENT_WORKER_DIFFICILE": "claude", "AGENT_SEUIL_CARACTERES": "100"}
        with mock.patch.dict(os.environ, env):
            s = config.load_settings()
        self.assertEqual(s.gemini_test_model, TEST_MODEL)
        routes = resolve_routes(s)
        self.assertEqual(routes["routage"].agents(), ["gemini_test", "qwen_interface"])
        self.assertEqual(routes["routage"].source, "configuration")
        self.assertEqual((routes["worker"].agent, routes["worker"].difficile), ("gemini", "claude"))
        self.assertEqual(routes["worker"].seuil_caracteres, 100)

    def test_remplacement_d_un_role_par_configuration_seule(self):
        reg, fakes = fake_registry(qwen_interface=[ROUTE_OK], gemini_test=[ROUTE_OK])
        self.assertEqual(route(router_for(reg).role_llm("routage"), "salut").command.name, "tache")
        self.assertEqual(len(fakes["qwen_interface"].prompts), 1)
        with mock.patch.dict(os.environ, {"AGENT_ROUTAGE": "gemini_test"}):
            s = config.load_settings()
        router = router_for(reg, s, confirm=lambda preview: True)
        self.assertEqual(route(router.role_llm("routage"), "salut").command.name, "tache")
        self.assertEqual(len(fakes["gemini_test"].prompts), 1)
        self.assertEqual(len(fakes["qwen_interface"].prompts), 1)  # inchangé : l'autre agent a répondu

    def test_configurations_invalides(self):
        with self.assertRaises(ValueError):
            resolve_routes(settings(agent_roles=(("routage", "inconnu"),)), known_agents=["qwen_interface"])
        with self.assertRaises(ValueError):
            resolve_routes(settings(agent_roles=(("role_inexistant", "gemini"),)))
        with self.assertRaises(ValueError):
            parse_declarations({"gemini_test": {"fournisseur": "gemini", "modele": "x"}}, source="t")
        with self.assertRaises(ValueError):
            parse_declarations({"a": {"fournisseur": "ollama", "modele": "x", "hote": "http://10.0.0.2:11434"}},
                               source="t")
        with self.assertRaises(ValueError):
            parse_declarations({"a": {"fournisseur": "ollama"}}, source="t")  # pas de modèle inventé
        with self.assertRaises(ValueError):
            parse_declarations("{pas du json", source="t")
        reg, _ = fake_registry()
        with self.assertRaises(AgentUnavailable):
            AgentRouter(reg, {"routage": RoleRoute("routage", "absent")})

    def test_profil_affecte_les_roles(self):
        doc = dict(MINIMAL, agents={
            "declarations": {"qwen_rapide": {"fournisseur": "ollama", "modele": "modele-local-factice"}},
            "roles": {"dedup": "qwen_rapide", "routage": {"agent": "gemini_test", "repli": ["qwen_interface"]}}})
        with tempfile.TemporaryDirectory() as d:
            write_profile(Path(d), "essai", doc)
            profile = load_profile("essai", Path(d))
        routes = resolve_routes(self.settings, profile)
        self.assertEqual(routes["dedup"].agents(), ["qwen_rapide"])
        self.assertEqual(routes["routage"].agents(), ["gemini_test", "qwen_interface"])
        self.assertEqual(routes["routage"].source, "profil")
        reg = self.registry(profile=profile)
        self.assertEqual(reg.spec("qwen_rapide").model, "modele-local-factice")

    def test_profils_livres_inchanges(self):
        self.assertEqual(load_profile("brainstorm").agents, {})
        self.assertEqual(resolve_routes(settings(), load_profile("brainstorm")), resolve_routes(settings()))


# ------------------------------------------------------------------- routage


class RoutingTest(GeminiFixture):
    def test_simple_economique_difficile_capable(self):
        reg, fakes = fake_registry(qwen_interface=["court"], gemini=["long"])
        routes = resolve_routes(settings(), None, reg.names())
        routes["interface"] = replace(routes["interface"], difficile="gemini", seuil_caracteres=50)
        router = AgentRouter(reg, routes, confirm=lambda p: True)
        self.assertEqual(router.call("interface", "question courte").agent, "qwen_interface")
        r = router.call("interface", "x" * 60)
        self.assertEqual(r.agent, "gemini")
        self.assertIn("tâche difficile", r.reason)
        self.assertIn("tâche simple", router.journal[0]["reason"])

    def test_critere_par_score(self):
        reg, _ = fake_registry(gemini=["a"], claude=["b"])
        routes = resolve_routes(settings(agent_roles_difficile=(("grand_modele", "claude"),),
                                         agent_seuil_score=5), None, reg.names())
        router = AgentRouter(reg, routes)
        self.assertEqual(router.call("grand_modele", "p", score=3).agent, "gemini")
        self.assertEqual(router.call("grand_modele", "p", score=7).agent, "claude")
        self.assertIn("score 7 >= 5", router.journal[-1]["reason"])

    def test_repli_sur_chaque_type_d_echec(self):
        cases = {
            "timeout": AgentTimeout("t"), "http_429": AgentRateLimited("429"),
            "vide": "   ", "hors_schema": '{"autre": 1}',
        }
        for kind, failure in cases.items():
            with self.subTest(kind=kind):
                reg, fakes = fake_registry(gemini_test=[failure],
                                           qwen_interface=['{"identique": true, "explication": "x"}'],
                                           qwen_format=['{"identique": "?"}'])
                s = settings(agent_roles=(("promesse", "gemini_test,qwen_interface"),))
                router = router_for(reg, s, confirm=lambda preview: True)  # promesse : rôle Qwen
                r = router.call("promesse", "p", schema=PROMISE_SCHEMA)
                self.assertEqual(r.agent, "qwen_interface")
                self.assertIn(f"repli après gemini_test ({kind})", r.reason)
                results = [(e["agent"], e["result"]) for e in router.journal if e["role"] == "promesse"]
                self.assertEqual(results, [("gemini_test", kind), ("qwen_interface", "ok")])

    def test_repli_si_agent_indisponible(self):
        reg = self.registry(settings(state_dir=self.tmp, agent_roles=(("routage", "gemini_test,qwen_interface"),)))
        reg.register(FakeAgent("qwen_interface", answers=[ROUTE_OK]))
        router = router_for(reg, settings(agent_roles=(("routage", "gemini_test,qwen_interface"),)))
        self.assertEqual(route(router.role_llm("routage"), "x").command.name, "tache")
        self.assertEqual(router.journal[0]["result"], "indisponible")
        self.assertEqual(self.transport.requests, [])

    def test_tous_les_agents_echouent(self):
        reg, _ = fake_registry(qwen_interface=[AgentTimeout("t"), AgentTimeout("t")])
        router = router_for(reg)
        with self.assertRaises(NoAgentSucceeded) as ctx:
            router.call("routage", "p")
        self.assertEqual(ctx.exception.failures, [("qwen_interface", "timeout")])
        with self.assertRaises(LocalLLMError):  # via RoleLLM : les appelants existants échouent fermé
            router.role_llm("routage").ask("p")

    def test_journal_du_bus(self):
        reg, _ = fake_registry(qwen_interface=[AgentRateLimited("429")], qwen_format=["ok"])
        s = settings(agent_roles=(("interface", "qwen_interface,qwen_format"),))
        logger = RunLogger(self.tmp / "logs", "agents")
        router = router_for(reg, s, logger=logger)
        router.call("interface", "secret à ne pas journaliser")
        events = [json.loads(l) for l in logger.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["agent_call", "agent_call"])
        for key in ("role", "provider", "model", "duration_s", "result", "reason"):
            self.assertIn(key, events[0])
        self.assertEqual((events[0]["result"], events[1]["result"]), ("http_429", "ok"))
        self.assertEqual(events[1]["model"], "factice")
        self.assertNotIn("secret", logger.path.read_text(encoding="utf-8"))

    def test_role_client_dans_le_bus_sans_modification(self):
        plan = json.dumps({"tasks": [{"worker": "redacteur", "instruction": "a"},
                                     {"worker": "redacteur", "instruction": "b"}]})
        reg, fakes = fake_registry(gemini=[plan], qwen_interface=["texte a", AgentTimeout("t")])
        s = settings(agent_roles=(("worker", "qwen_interface"),))
        router = router_for(reg, s)
        spec = WorkerSpec("redacteur", "rédige", "Tu rédiges.")
        orch = Orchestrator(GeminiOrganizer(router.role_client("organisateur")),
                            [GeminiWorker(spec, router.role_client("worker"))], basic_verifier)
        report = orch.run("tâche", synthesize=False)
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.accepted[0].text, "texte a")
        self.assertIn("RoleCallError", report.rejected[0].error)
        self.assertTrue(issubclass(RoleCallError, GeminiError))


# -------------------------------------------------------------- limiteur


class RateLimiterQueueTest(GeminiFixture):
    def test_tous_les_agents_gemini_par_la_meme_file(self):
        self.answer("un", "deux")
        reg = self.registry()
        reg.get("gemini").call("p")
        reg.get("gemini_test").call("p")
        self.assertIs(reg.gemini_limiter, self.limiter)
        self.assertIs(reg.get("gemini_test").client.limiter, self.limiter)
        self.assertEqual(self.calls_through_queue, 2)
        self.assertEqual(self.limiter.used_today, 2)
        self.assertEqual([r["url"].split("/models/")[1] for r in self.transport.requests],
                         [f"{Settings().gemini_model}:generateContent", f"{TEST_MODEL}:generateContent"])

    def test_deuxieme_file_refusee(self):
        reg = self.registry()
        reg.get("gemini")
        other = RateLimiter(GeminiQuota(), self.tmp / "autre.json", wall_clock=self.clock.time, sleep=self.clock.sleep)
        reg.declare(AgentSpec("gemini_bis", "gemini", "m"), lambda spec: GeminiAgent(spec, self.factory, other))
        with self.assertRaises(ValueError):
            reg.get("gemini_bis")

    def test_client_hors_file_refuse(self):
        other = RateLimiter(GeminiQuota(), self.tmp / "autre.json", wall_clock=self.clock.time, sleep=self.clock.sleep)
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL),
                            lambda m, lim: GeminiClient(KEY, m, other, transport=self.transport), self.limiter)
        with self.assertRaises(ValueError):
            agent.call("p")
        self.assertEqual(self.transport.requests, [])

    def test_espacement_applique_a_gemini_test(self):
        self.answer("un", "deux")
        reg = self.registry()
        reg.get("gemini_test").call("p")
        reg.get("gemini_test").call("p")
        self.assertEqual(self.clock.sleeps, [GeminiQuota().min_interval_s])


# ---------------------------------------------- gemini_test dans les rôles Qwen


class GeminiTestInQwenRolesTest(GeminiFixture):
    """Chaque rôle Qwen accepte gemini_test avec la même forme de sortie."""

    def router(self, role, **kw):
        s = replace(self.settings, agent_roles=((role, "gemini_test"),))
        reg = self.registry(s)
        self.qwen_format = FakeLLM([])
        reg.register(LocalAgent(AgentSpec("qwen_format", "ollama", "qwen-format-factice"), self.qwen_format))
        kw.setdefault("confirm", lambda preview: True)
        return router_for(reg, s, **kw)

    def test_meme_schema_et_meme_forme_pour_chaque_role(self):
        promise = '{"identique": true, "explication": "x"}'
        groups = '{"groupes": []}'
        cases = [("interface", ROUTE_SCHEMA, ROUTE_OK), ("routage", ROUTE_SCHEMA, ROUTE_OK),
                 ("cadrage", FICHE_SCHEMA, '{"objectif": "o", "contraintes": [], "perimetre": [], '
                                           '"hors_perimetre": []}'),
                 ("promesse", PROMISE_SCHEMA, promise), ("format", GROUPS_SCHEMA, groups),
                 ("dedup", GROUPS_SCHEMA, groups), ("reparation", PROMISE_SCHEMA, promise)]
        self.assertEqual({c[0] for c in cases}, {r for r, i in ROLES.items() if i.default_agent.startswith("qwen")})
        for role, schema, answer in cases:
            with self.subTest(role=role):
                qwen = FakeLLM([answer])
                local = router_for(*self._local(role, qwen))
                doc_qwen, _ = local.role_llm(role).ask_json("demande", schema)
                self.answer(answer)
                doc_gemini, reply = self.router(role).role_llm(role).ask_json("demande", schema)
                self.assertEqual(doc_gemini, doc_qwen)
                self.assertEqual(reply.model, f"gemini_test:{TEST_MODEL}")
                self.assertIs(qwen.calls[0]["schema"], schema)  # même schéma des deux côtés
                system = self.transport.requests[-1]["body"]["systemInstruction"]["parts"][0]["text"]
                self.assertIn(json.dumps(schema, ensure_ascii=False), system)

    def _local(self, role, qwen):
        reg = AgentRegistry()
        default = ROLES[role].default_agent
        reg.register(LocalAgent(AgentSpec(default, "ollama", "qwen-factice"), qwen))
        reg.register(LocalAgent(AgentSpec("qwen_format" if default != "qwen_format" else "qwen_interface",
                                          "ollama", "qwen-factice"), FakeLLM([])))
        for name in ("gemini", "gemini_test", "claude"):
            reg.register(FakeAgent(name, "claude" if name == "claude" else "gemini"))
        return reg, settings()

    def test_routage(self):
        self.answer(ROUTE_OK)
        proposal = route(self.router("routage").role_llm("routage"), "j'aimerais des idées d'application")
        self.assertEqual((proposal.command.name, proposal.command.argument),
                         ("tache", "trouver des idées d'application"))

    def test_cadrage(self):
        self.answer('{"objectif": "Un outil de devis", "contraintes": ["hors ligne"], "perimetre": [], '
                    '"hors_perimetre": []}')
        fiche = cadrer(self.router("cadrage").role_llm("cadrage"), "un outil de devis hors ligne")
        self.assertEqual((fiche.objectif, fiche.contraintes), ("Un outil de devis", ["hors ligne"]))

    def test_promesse_reponse_fermee_oui_non(self):
        for answer, accepted in (('{"identique": true, "explication": "même résultat"}', True),
                                 ('{"identique": false, "explication": "résultat réduit"}', False)):
            with self.subTest(accepted=accepted):
                self.answer(answer)
                r = check_promise(CONTRACT, version(promesse="Produire un devis PDF rapidement"),
                                  self.router("promesse").role_llm("promesse"))
                self.assertIs(r.accepted, accepted)
                self.assertIs(r.promise_identical, accepted)

    def test_promesse_json_invalide_repare_par_qwen_25_3b(self):
        self.answer('{"identique": false, "explication": "résultat réduit"')  # accolade manquante
        router = self.router("promesse")
        self.qwen_format.answers.append('{"identique": false, "explication": "résultat réduit"}')
        r = check_promise(CONTRACT, version(promesse="Produire un devis"), router.role_llm("promesse"))
        self.assertFalse(r.accepted)
        self.assertFalse(r.promise_identical)
        ok = [e for e in router.journal if e["result"] == "ok"]
        self.assertEqual([(e["role"], e["agent"]) for e in ok], [("reparation", "qwen_format"),
                                                                 ("promesse", "gemini_test")])
        self.assertEqual(ok[-1]["repair"], "qwen")

    def test_promesse_echoue_ferme_si_hors_schema(self):
        self.answer('{"identique": "peut-être", "explication": "x"}')
        router = self.router("promesse")
        self.qwen_format.answers.append('{"identique": "oui"}')
        r = check_promise(CONTRACT, version(promesse="Produire un devis"), router.role_llm("promesse"))
        self.assertFalse(r.accepted)
        self.assertIsNone(r.promise_identical)

    def test_dedup(self):
        self.answer('{"groupes": [{"ids": [1, 2], "raison": "même approche"}]}')
        ideas = [Idea(1, {"titre": "Photo du frigo"}), Idea(2, {"titre": "Scanner le réfrigérateur"}),
                 Idea(3, {"titre": "Liste de courses"})]
        res = deduplicate(ideas, self.router("dedup").role_llm("dedup"))
        self.assertEqual([i.id for i in res.kept], [1, 3])
        self.assertTrue(res.model_used)

    def test_reparation(self):
        self.answer('{"identique": true, "explication": "même résultat"}')
        res = repair_json('identique: oui, explication: "même résultat"', PROMISE_SCHEMA,
                          self.router("reparation").role_llm("reparation"))
        self.assertTrue(res.ok)
        self.assertEqual(json.loads(res.text), {"identique": True, "explication": "même résultat"})


# ------------------------------------------------------------ confidentialité


class LeakyMasker(Masker):
    def leak_check(self, masked, *, declared=()):
        return [Finding("EMAIL", 0, 3, "abc")]


class PrivacyTest(GeminiFixture):
    def test_a_profil_personnel_refuse_gemini_test(self):
        self.answer(ROUTE_OK)
        s = replace(self.settings, agent_roles=(("routage", "gemini_test"),))
        router = router_for(self.registry(s), s, personal_data=True, confirm=lambda p: True)
        with self.assertRaises(PolicyError):
            route(router.role_llm("routage"), "salut")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(router.journal[0]["result"], "refuse_politique")

    def test_a_profil_personnel_refuse_tout_externe_meme_en_repli(self):
        reg, fakes = fake_registry(qwen_interface=["ok"], claude=["ok"])
        for agents in ("qwen_interface,claude", "qwen_interface,gemini"):
            with self.subTest(agents=agents):
                router = router_for(reg, settings(agent_roles=(("interface", agents),)), personal_data=True)
                with self.assertRaises(PolicyError):
                    router.call("interface", "p")
        self.assertEqual(fakes["qwen_interface"].prompts, [])  # refus avant tout appel
        self.assertEqual(fakes["claude"].prompts, [])

    def test_a_profil_personnel_local_autorise(self):
        reg, fakes = fake_registry(qwen_interface=[ROUTE_OK])
        router = router_for(reg, personal_data=True)
        self.assertEqual(route(router.role_llm("routage"), "salut").command.name, "tache")

    def test_a_profil_personnel_livre_avec_configuration_externe(self):
        office = load_profile("bureautique_fictive")
        s = replace(self.settings, agent_roles=(("cadrage", "gemini_test"),))
        router = build_router(s, profile=office, registry=self.registry(s), confirm=lambda p: True)
        with self.assertRaises(PolicyError):
            router.call("cadrage", "description")
        self.assertEqual(self.transport.requests, [])

    def test_a_profil_personnel_ne_peut_pas_declarer_d_externe(self):
        base = dict(MINIMAL, donnees_personnelles=True, champs_utiles=["montant"])
        cases = {
            "role_gemini_test": {"roles": {"routage": "gemini_test"}},
            "repli_claude": {"roles": {"routage": {"agent": "qwen_interface", "repli": ["claude"]}}},
            "difficile_gemini": {"roles": {"worker": {"agent": "qwen_interface", "difficile": "gemini"}}},
            "declaration_externe": {"declarations": {"g2": {"fournisseur": "gemini", "modele": "x"}}},
        }
        with tempfile.TemporaryDirectory() as d:
            for name, agents in cases.items():
                write_profile(Path(d), name, dict(base, agents=agents))
                with self.subTest(name=name), self.assertRaises(ProfileError):
                    load_profile(name, Path(d))
            write_profile(Path(d), "local_ok", dict(base, agents={"roles": {"dedup": "qwen_interface"}}))
            self.assertTrue(load_profile("local_ok", Path(d)).personal_data)
        self.assertEqual(validate_profile_agents({"roles": {"routage": "gemini_test"}}, personal_data=False),
                         {"roles": {"routage": "gemini_test"}})

    def test_b_masquage_avant_envoi_et_reinjection_locale(self):
        self.answer(json.dumps({"identique": False, "explication": "écrire à [EMAIL_1]"}))
        s = replace(self.settings, agent_roles=(("promesse", "gemini_test"),))
        router = router_for(self.registry(s), s, masker=Masker(Vault()), confirm=lambda p: True)
        r = router.call("promesse", "Contact : jean.martin@exemple.fr", schema=PROMISE_SCHEMA)
        sent = self.sent_texts()[0]
        self.assertNotIn("jean.martin@exemple.fr", sent)
        self.assertIn("[EMAIL_1]", sent)
        self.assertEqual(r.data["explication"], "écrire à jean.martin@exemple.fr")  # réinjecté en local

    def test_b_fuite_detectee_envoi_bloque(self):
        s = replace(self.settings, agent_roles=(("promesse", "gemini_test"),))
        router = router_for(self.registry(s), s, masker=LeakyMasker(Vault()))
        with self.assertRaises(LeakBlocked):
            router.call("promesse", "texte")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(router.journal[-1]["result"], "fuite_bloquee")

    def test_b_sans_masqueur_controle_de_fuite_quand_meme(self):
        s = replace(self.settings, agent_roles=(("promesse", "gemini_test"),))
        router = router_for(self.registry(s), s)
        with self.assertRaises(ExternalSendBlocked):  # via RoleLLM : erreur locale, échec fermé
            router.role_llm("promesse").ask("écrire à jean.martin@exemple.fr")
        self.assertEqual(self.transport.requests, [])

    def test_b_local_rien_n_est_masque(self):
        reg, fakes = fake_registry(qwen_interface=["ok"])
        router = router_for(reg, masker=LeakyMasker(Vault()))
        router.call("interface", "jean.martin@exemple.fr")
        self.assertEqual(fakes["qwen_interface"].prompts, ["jean.martin@exemple.fr"])

    def test_c_texte_libre_confirmation_obligatoire(self):
        s = replace(self.settings, agent_roles=(("routage", "gemini_test"),))
        for confirm in (None, lambda preview: False):
            with self.subTest(confirm=confirm):
                router = router_for(self.registry(s), s, confirm=confirm)
                with self.assertRaises(ExternalSendBlocked):
                    route(router.role_llm("routage"), "salut")
                with self.assertRaises(PreviewRefused):
                    router.call("routage", "salut")
        self.assertEqual(self.transport.requests, [])

    def test_c_confirmation_avec_apercu_masque(self):
        self.answer(ROUTE_OK)
        previews = []
        s = replace(self.settings, agent_roles=(("routage", "gemini_test"),))
        router = router_for(self.registry(s), s, masker=Masker(Vault()),
                            confirm=lambda p: previews.append(p) or True)
        route(router.role_llm("routage"), "relancer jean.martin@exemple.fr")
        self.assertEqual(len(previews), 1)
        self.assertIn("[EMAIL_1]", previews[0])
        self.assertNotIn("jean.martin@exemple.fr", previews[0])

    def test_c_pas_de_confirmation_hors_role_qwen_ni_en_local(self):
        self.answer("texte")
        s = replace(self.settings, agent_roles=(("worker", "gemini_test"),))
        router_for(self.registry(s), s, confirm=lambda p: self.fail("aucune confirmation attendue")).call(
            "worker", "p")
        reg, _ = fake_registry(qwen_interface=[ROUTE_OK])
        route(router_for(reg, confirm=lambda p: self.fail("local : pas de confirmation")).role_llm("routage"), "x")


# ------------------------------------- texte libre : confirmation sur tout rôle Qwen

QWEN_INVALIDE = "Commande : tache, relancer jean.martin@exemple.fr pour le devis"  # non réparable par le code


class FreeTextDerivedTest(GeminiFixture):
    """Tout envoi externe dont le contenu peut dériver du texte libre exige la confirmation."""

    def router(self, *roles, qwen_answers=(), **kw):
        s = replace(self.settings, agent_roles=tuple(roles))
        reg = self.registry(s)
        self.qwen = FakeLLM(list(qwen_answers))
        reg.register(LocalAgent(AgentSpec("qwen_interface", "ollama", "qwen-factice"), self.qwen))
        reg.register(LocalAgent(AgentSpec("qwen_format", "ollama", "qwen-format-factice"), FakeLLM([])))
        return router_for(reg, s, **kw)

    def test_reparation_externe_du_json_de_qwen_exige_confirmation(self):
        cases = [("routage", lambda llm: route(llm, "relancer jean.martin@exemple.fr")),
                 ("cadrage", lambda llm: cadrer(llm, "devis pour jean.martin@exemple.fr"))]
        for role, use in cases:
            for confirm in (None, lambda preview: False):
                with self.subTest(role=role, confirm=confirm):
                    router = self.router(("reparation", "gemini_test"), qwen_answers=[QWEN_INVALIDE],
                                         masker=Masker(Vault()), confirm=confirm)
                    with self.assertRaises(LocalLLMError):  # échec fermé, rien n'est parti
                        use(router.role_llm(role))
                    self.assertEqual(self.transport.requests, [])
                    self.assertIn(("reparation", "gemini_test", "apercu_refuse"),
                                  [(e["role"], e["agent"], e["result"]) for e in router.journal])

    def test_reparation_externe_confirmee_apercu_masque_et_destinataire_nomme(self):
        # Gemini ne voit que le jeton ; la réparation doit rester fidèle à la sortie de Qwen.
        self.answer(json.dumps({"action": "tache", "argument": "relancer [EMAIL_1] pour le devis", "reponse": ""}))
        previews = []
        router = self.router(("reparation", "gemini_test"), qwen_answers=[QWEN_INVALIDE],
                             masker=Masker(Vault()), confirm=lambda p: previews.append(p) or True)
        cmd = route(router.role_llm("routage"), "relancer jean.martin@exemple.fr").command
        self.assertEqual((cmd.name, cmd.argument), ("tache", "relancer jean.martin@exemple.fr pour le devis"))
        self.assertEqual(len(previews), 1)
        self.assertIn("« gemini_test »", previews[0])
        self.assertIn(TEST_MODEL, previews[0])
        self.assertIn("fournisseur gemini", previews[0])
        self.assertIn("[EMAIL_1]", previews[0])
        self.assertNotIn("jean.martin@exemple.fr", previews[0])
        self.assertNotIn("jean.martin@exemple.fr", self.sent_texts()[0])

    def test_tout_role_qwen_exige_confirmation(self):
        qwen_roles = [r for r, i in ROLES.items() if i.default_agent.startswith("qwen")]
        self.assertEqual(set(qwen_roles), {"interface", "cadrage", "routage", "promesse", "format", "dedup",
                                           "reparation"})
        for role in qwen_roles:
            with self.subTest(role=role):
                self.assertTrue(ROLES[role].free_text)
                router = self.router((role, "gemini_test"))
                with self.assertRaises(PreviewRefused):
                    router.call(role, "texte")
        self.assertEqual(self.transport.requests, [])

    def test_free_text_explicite_ne_peut_pas_assouplir(self):
        router = self.router(("routage", "gemini_test"))
        with self.assertRaises(PreviewRefused):
            router.call("routage", "salut", free_text=False)
        self.assertEqual(self.transport.requests, [])

    def test_confirmation_redemandee_si_le_repli_change_de_fournisseur(self):
        reg, fakes = fake_registry(gemini_test=[AgentRateLimited("429")], claude=[ROUTE_OK])
        s = settings(agent_roles=(("routage", "gemini_test,claude"),))
        previews = []
        router = router_for(reg, s, confirm=lambda p: previews.append(p) or True)
        self.assertEqual(router.call("routage", "salut", schema=ROUTE_SCHEMA).agent, "claude")
        self.assertEqual(len(previews), 2)
        self.assertIn("« gemini_test »", previews[0])
        self.assertIn("« claude »", previews[1])
        self.assertIn("fournisseur claude", previews[1])

    def test_refus_au_repli_rien_n_est_envoye_au_second_fournisseur(self):
        reg, fakes = fake_registry(gemini_test=[AgentRateLimited("429")], claude=[ROUTE_OK])
        answers = iter([True, False])
        router = router_for(reg, settings(agent_roles=(("routage", "gemini_test,claude"),)),
                            confirm=lambda p: next(answers))
        with self.assertRaises(PreviewRefused):
            router.call("routage", "salut")
        self.assertEqual(len(fakes["gemini_test"].prompts), 1)
        self.assertEqual(fakes["claude"].prompts, [])
        self.assertEqual(router.journal[-1]["result"], "apercu_refuse")

    def test_profil_personnel_reparation_externe_refusee(self):
        router = self.router(("reparation", "gemini_test"), qwen_answers=[QWEN_INVALIDE],
                             personal_data=True, confirm=lambda p: True)
        with self.assertRaises(PolicyError):
            route(router.role_llm("routage"), "salut")
        self.assertEqual(self.transport.requests, [])


# ------------------------------------------------ quota journalier Gemini


DAILY_429 = HttpResponse(429, {}, err_body(429, "RESOURCE_EXHAUSTED", details=[
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]))


class DailyQuotaTest(GeminiFixture):
    def test_adaptateur_gemini_quota_journalier(self):
        self.answer(DAILY_429)
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL), self.factory, self.limiter)
        with self.assertRaises(AgentDailyQuota) as ctx:
            agent.call("p")
        self.assertIsInstance(ctx.exception.original, GeminiDailyQuotaError)
        self.assertEqual(len(self.transport.requests), 1)  # aucun retry

    def test_adaptateur_gemini_arret_dur_du_limiteur(self):
        limiter = RateLimiter(GeminiQuota(daily_alert=1, daily_hard_stop=1), self.tmp / "q2.json",
                              wall_clock=self.clock.time, sleep=self.clock.sleep)
        self.answer("un")
        agent = GeminiAgent(AgentSpec("gemini_test", "gemini", TEST_MODEL), self.factory, limiter)
        agent.call("p")
        with self.assertRaises(AgentDailyQuota) as ctx:
            agent.call("p")
        self.assertIsInstance(ctx.exception.original, DailyQuotaExhausted)

    def test_aucun_repli_vers_un_autre_agent_gemini_et_type_d_arret_conserve(self):
        reg, fakes = fake_registry(gemini=[AgentDailyQuota("stop")], gemini_test=["ne doit pas servir"])
        router = router_for(reg, settings(agent_roles=(("worker", "gemini,gemini_test"),)))
        with self.assertRaises(RoleDailyQuotaError) as ctx:
            router.role_client("worker").generate("p")
        self.assertIsInstance(ctx.exception, GeminiDailyQuotaError)
        self.assertIsInstance(ctx.exception, RoleCallError)
        self.assertEqual(fakes["gemini_test"].prompts, [])
        self.assertEqual([(e["agent"], e["result"]) for e in router.journal],
                         [("gemini", "quota_journalier"), ("gemini_test", "quota_journalier")])

    def test_repli_non_gemini_autorise(self):
        reg, fakes = fake_registry(gemini=[AgentDailyQuota("stop")], qwen_interface=["texte"])
        router = router_for(reg, settings(agent_roles=(("worker", "gemini,gemini_test,qwen_interface"),)))
        self.assertEqual(router.role_client("worker").generate("p").agent, "qwen_interface")
        self.assertEqual(fakes["gemini_test"].prompts, [])

    def test_role_llm_propage_l_arret_au_lieu_d_echouer_en_local(self):
        reg, _ = fake_registry(gemini=[AgentDailyQuota("stop")])
        router = router_for(reg, settings(agent_roles=(("routage", "gemini"),)), confirm=lambda p: True)
        with self.assertRaises(GeminiDailyQuotaError) as ctx:
            route(router.role_llm("routage"), "salut")
        self.assertNotIsInstance(ctx.exception, LocalLLMError)

    def test_bus_s_arrete_comme_sur_le_chemin_par_defaut(self):
        plan = json.dumps({"tasks": [{"worker": "redacteur", "instruction": "a"},
                                     {"worker": "redacteur", "instruction": "b"}]})
        spec = WorkerSpec("redacteur", "rédige", "Tu rédiges.")
        for where in ("organisateur", "worker"):
            with self.subTest(where=where):
                answers = [AgentDailyQuota("stop")] if where == "organisateur" else [plan, AgentDailyQuota("stop")]
                reg, fakes = fake_registry(gemini=answers, gemini_test=["jamais"])
                router = router_for(reg, settings(agent_roles=((where, "gemini,gemini_test"),)))
                orch = Orchestrator(GeminiOrganizer(router.role_client("organisateur")),
                                    [GeminiWorker(spec, router.role_client("worker"))], basic_verifier)
                with self.assertRaises(GeminiDailyQuotaError):
                    orch.run("tâche", synthesize=False)
                self.assertEqual(fakes["gemini_test"].prompts, [])
                self.assertEqual(len(fakes["gemini"].prompts), len(answers))  # 2e délégation jamais tentée

    def test_revue_s_arrete(self):
        reg, fakes = fake_registry(gemini=[AgentDailyQuota("stop")])
        panel = ReviewPanel(router_for(reg).role_client("relecteur"))
        with self.assertRaises(GeminiDailyQuotaError):
            panel.review([Idea(1, {"titre": "Photo du frigo"})])
        self.assertEqual(len(fakes["gemini"].prompts), 1)  # pas de 2e relecteur après l'arrêt


if __name__ == "__main__":
    unittest.main()

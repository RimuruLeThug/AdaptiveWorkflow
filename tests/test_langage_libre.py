"""Langage libre : reformulation sous accord de session, profil implicite, retours libres.

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
from agents import ROLES, AgentRateLimited, AgentRouter, AgentSpec, LocalAgent, build_registry, resolve_routes
from config import GeminiQuota, Settings
from gemini_client import GeminiClient, HttpResponse
from local_llm import LocalLLMError
from orchestrator import Orchestrator
from privacy.masker import Masker, PreviewRefused
from privacy.policy import PolicyError
from privacy.vault import Vault
from profiles.loader import ProfileError, list_profiles, load_profile
from qwen_ui.app import App
from qwen_ui.langage_libre import (REFORMULATION_SYSTEM, FeedbackJournal, LangageLibre, Reformulator, choose_profile,
                                   clean_reason, explicit_profile, feedback_examples, feedback_record,
                                   is_skip_reason, parse_feedback, personal_kinds)
from qwen_ui.router import CONFIRMED_ACTIONS, FREE_TEXT_ACTIONS, parse_command, route_libre
from rate_limiter import RateLimiter
from review import IdeaVerdict, ReviewReport
from tests.test_agents import DAILY_429, LeakyMasker, fake_registry, router_for, settings
from tests.test_gemini_client import KEY, Clock, FakeTransport, err_body, ok_body
from tests.test_orchestrator import RecordingWorker, ScriptedOrganizer, plan, spec
from tests.test_profiles import MINIMAL, write_profile
from tests.test_promise import FakeLLM
from tests.test_wiring import WiringFixture
from verify.basic import basic_verifier
from verify.schema import SchemaError
from workers.dedup import Idea

TEST_MODEL = "gemini_test_remplace"  # valeur fictive de GEMINI_TEST_MODEL : aucun vrai modèle désigné
ROOT = Path(__file__).resolve().parent.parent
ACCORD = "Autoriser Gemini à reformuler vos messages (texte masqué, contrôle de fuite avant chaque envoi) ? (o/n) "
EMAIL = "jean.martin@exemple.fr"  # adresse fictive


def reform(demande="demande reformulée", **kw):
    doc = {"demande": demande, "intention": "", "objet": "", "contraintes": [], "ambiguites": []}
    doc.update(kw)
    return json.dumps(doc, ensure_ascii=False)


def route_json(action, argument="", reponse="", profil=""):
    return json.dumps({"action": action, "argument": argument, "reponse": reponse, "profil": profil},
                      ensure_ascii=False)


def feedback_json(decisions=(), tout="", raison_tout="", ambigu=False):
    return json.dumps({"decisions": [dict(numero=n, decision=d, raison=r) for n, d, r in decisions],
                       "tout": tout, "raison_tout": raison_tout, "ambigu": ambigu}, ensure_ascii=False)


def review(*titles):
    verdicts = [IdeaVerdict(Idea(i, {"titre": t}), {}, 2, True, 7.0) for i, t in enumerate(titles, 1)]
    return ReviewReport("rev-1", verdicts, verdicts, [], 6.5, 2)


class Logger:
    def __init__(self):
        self.events = []

    def event(self, kind, **meta):
        self.events.append(dict(meta, event=kind))


class Fixture(unittest.TestCase):
    """App réelle + routeur réel ; Gemini (gemini_test) sur transport simulé, Qwen = FakeLLM."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()
        self.limiter = RateLimiter(GeminiQuota(), self.tmp / "gemini_quota.json", wall_clock=self.clock.time,
                                   sleep=self.clock.sleep)
        self.transport = FakeTransport([])
        self.logger = Logger()
        self.inputs, self.outputs, self.factory_calls = [], [], []
        self.settings = replace(Settings(), state_dir=self.tmp, logs_dir=self.tmp / "logs",
                                gemini_test_model=TEST_MODEL, agent_roles=(("reformulation", "gemini_test"),))

    def tearDown(self):
        self._tmp.cleanup()

    def gemini_factory(self, model, limiter):
        return GeminiClient(KEY, model, limiter, transport=self.transport)

    def input_fn(self, prompt):
        self.outputs.append(prompt)
        if not self.inputs:
            raise AssertionError(f"question inattendue : {prompt}")
        return self.inputs.pop(0)

    def confirm(self, preview):
        self.outputs.append(preview)
        return self.input_fn("Envoyer ce texte à un agent externe ? (o/n) ").strip().lower() in ("o", "oui")

    def make(self, llm_answers=(), *, roles=None, profile="brainstorm", masker=None, plans=(), worker_outputs=(),
             n_exemples=5):
        self.llm = FakeLLM(list(llm_answers))
        s = replace(self.settings, agent_roles=self.settings.agent_roles if roles is None else roles)
        reg = build_registry(s, gemini_limiter=self.limiter, gemini_client_factory=self.gemini_factory)
        reg.register(LocalAgent(AgentSpec("qwen_interface", "ollama", "qwen-factice"), self.llm))
        reg.register(LocalAgent(AgentSpec("qwen_format", "ollama", "qwen-format-factice"), FakeLLM([])))
        box = []
        self.masker = masker or Masker(Vault())
        self.router = AgentRouter(reg, resolve_routes(s, None, reg.names()), logger=self.logger,
                                  personal_data=lambda: bool(box and box[0]._personal()), masker=self.masker,
                                  declared=lambda: box[0]._declared() if box else [], confirm=self.confirm)
        self.journal = FeedbackJournal(self.tmp / "app" / "retours.jsonl")
        ll = LangageLibre(Reformulator(self.router, self.llm), self.journal, n_exemples=n_exemples)
        self.org = ScriptedOrganizer(list(plans))
        self.worker = RecordingWorker(spec("generateur"), list(worker_outputs))

        def factory(name, prof=None):
            self.factory_calls.append(name)
            return Orchestrator(self.org, [self.worker], basic_verifier, logs_dir=self.tmp / "logs")

        app = App(self.llm, factory, quota_info=lambda: (self.limiter.used_today, 1400), state_dir=self.tmp / "app",
                  input_fn=self.input_fn, output_fn=self.outputs.append,
                  profile=load_profile(profile) if profile else None, profile_loader=load_profile,
                  profile_names=list_profiles, masker_factory=lambda: self.masker, langage_libre=ll)
        box.append(app)
        self.app = app
        return app

    def answer(self, *items):
        self.transport.responses.extend(i if isinstance(i, (HttpResponse, Exception)) else
                                        HttpResponse(200, {}, ok_body(i)) for i in items)

    def sent_texts(self):
        return [r["body"]["contents"][0]["parts"][0]["text"] for r in self.transport.requests]

    def text(self):
        return "\n".join(self.outputs)

    def calls(self, role="reformulation"):
        return [(e["agent"], e["result"]) for e in self.logger.events
                if e["event"] == "agent_call" and e["role"] == role]


# ------------------------------------------------------------- rôle et configuration


class RoleConfigurationTest(unittest.TestCase):
    def test_role_reformulation_gemini_par_defaut(self):
        self.assertIn("reformulation", config.AGENT_ROLES)
        self.assertEqual(tuple(ROLES), tuple(config.AGENT_ROLES))
        info = ROLES["reformulation"]
        self.assertEqual(info.default_agent, "gemini")
        self.assertTrue(info.free_text and info.accord_session)
        self.assertEqual([r for r, i in ROLES.items() if i.accord_session], ["reformulation"])
        self.assertEqual(resolve_routes(settings())["reformulation"].agents(), ["gemini"])

    def test_remplacable_par_configuration_et_par_profil(self):
        with mock.patch.dict(os.environ, {"AGENT_REFORMULATION": "qwen_interface", "GEMINI_TEST_MODEL": TEST_MODEL}):
            s = config.load_settings()
        self.assertEqual(s.gemini_test_model, TEST_MODEL)
        self.assertEqual(resolve_routes(s)["reformulation"].agents(), ["qwen_interface"])
        with tempfile.TemporaryDirectory() as d:
            write_profile(Path(d), "essai", dict(MINIMAL, agents={"roles": {"reformulation": "gemini_test"}}))
            prof = load_profile("essai", Path(d))
            self.assertEqual(resolve_routes(settings(), prof)["reformulation"].agent, "gemini_test")
            perso = dict(MINIMAL, donnees_personnelles=True, champs_utiles=["montant"],
                         agents={"roles": {"reformulation": "gemini"}})
            write_profile(Path(d), "perso", perso)
            with self.assertRaises(ProfileError):  # profil perso : aucun agent externe
                load_profile("perso", Path(d))

    def test_variables_ajoutees(self):
        self.assertEqual(Settings().feedback_exemples, 5)
        with mock.patch.dict(os.environ, {"FEEDBACK_EXEMPLES": "2", "PROFIL_DONNEES_PERSO": "autre"}):
            s = config.load_settings()
        self.assertEqual((s.feedback_exemples, s.profil_donnees_perso), (2, "autre"))
        with mock.patch.dict(os.environ, {"FEEDBACK_EXEMPLES": "-3"}):
            self.assertEqual(config.load_settings().feedback_exemples, 0)
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in ("# FEEDBACK_EXEMPLES=5", "# AGENT_REFORMULATION=", "# PROFIL_DONNEES_PERSO="):
            self.assertIn(name, env)
        self.assertNotIn(TEST_MODEL, env)
        self.assertIn("state/", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())


# ------------------------------------------------------------------ accord unique


class ConsentTest(Fixture):
    def test_une_seule_question_puis_envois_masques(self):
        app = self.make([route_json("discussion", reponse="ok"), route_json("discussion", reponse="ok")])
        self.answer(reform("première"), reform("seconde"))
        self.inputs.append("o")
        app.handle("salut, que sais-tu faire ?")
        app.handle("et sinon ?")
        self.assertEqual(self.outputs.count(ACCORD), 1)
        self.assertEqual(len(self.transport.requests), 2)
        self.assertTrue(all(f"/models/{TEST_MODEL}:" in r["url"] for r in self.transport.requests))
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)  # accord = pas d'aperçu
        self.assertIn("Demande comprise (gemini_test) : première", self.outputs)
        self.assertEqual(self.calls(), [("gemini_test", "accord_session_donne"), ("gemini_test", "ok"),
                                        ("gemini_test", "ok")])
        # La demande reformulée est transmise au routage de Qwen.
        self.assertIn("Demande reformulée : première", self.llm.calls[0]["messages"][-1].content)

    def test_refus_reformulation_locale_rien_n_est_envoye(self):
        app = self.make([reform("locale"), route_json("discussion", reponse="ok"),
                         reform("locale 2"), route_json("discussion", reponse="ok")])
        self.inputs.append("n")
        app.handle("salut")
        app.handle("encore")
        self.assertEqual(self.outputs.count(ACCORD), 1)  # jamais redemandé
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.llm.calls[0]["messages"][0].content, REFORMULATION_SYSTEM)
        self.assertIn(("gemini_test", "accord_session_refuse"), self.calls())
        self.assertIn(("qwen_interface", "ok"), self.calls())

    def test_sans_callback_locale(self):
        self.make([reform("locale")])
        r = Reformulator(self.router, self.llm)
        res = r.reformulate("salut", personal=False, ask=None, out=self.outputs.append)
        self.assertEqual((res.agent, res.external), ("qwen_interface", False))
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(r.has_consent())

    def test_revocation_par_commande(self):
        app = self.make([route_json("discussion", reponse="ok"), reform("locale"),
                         route_json("discussion", reponse="ok")])
        self.answer(reform("distante"))
        self.inputs.append("o")
        app.handle("salut")
        app.handle("/accord")
        self.assertIn("Accord de session donné", self.outputs[-1])
        app.handle("/accord non")
        self.assertIn("Accord révoqué", self.outputs[-1])
        app.handle("encore")
        self.assertEqual(len(self.transport.requests), 1)  # plus rien n'est envoyé
        self.assertEqual(self.outputs.count(ACCORD), 1)
        self.assertIn(("gemini_test", "accord_session_revoque"), self.calls())
        app.handle("/accord oui")  # accord explicite redonné
        self.assertTrue(app._ll.reformulator.has_consent())

    def test_profil_perso_revoque_et_refuse_l_accord(self):
        app = self.make([route_json("discussion", reponse="ok"), reform("locale"),
                         route_json("discussion", reponse="ok")])
        self.answer(reform("distante"))
        self.inputs.append("o")
        app.handle("salut")
        app.handle("/profil bureautique_fictive")
        self.assertFalse(app._ll.reformulator.has_consent())
        app.handle("bonjour")
        self.assertEqual(len(self.transport.requests), 1)
        app.handle("/accord oui")
        self.assertIn("Refusé : profil « données personnelles »", self.outputs[-1])
        with self.assertRaises(PolicyError):
            self.router.grant_session_consent("reformulation", "gemini")
        self.assertFalse(app._ll.reformulator.has_consent())

    def test_accord_jamais_pour_un_autre_role_ni_un_autre_fournisseur(self):
        reg, fakes = fake_registry(gemini_test=[AgentRateLimited("429")], claude=[reform("par claude")])
        previews = []
        router = router_for(reg, settings(agent_roles=(("reformulation", "gemini_test,claude"),
                                                       ("routage", "gemini_test"))),
                            confirm=lambda p: previews.append(p) or False)
        for role in ("routage", "worker", "cadrage"):
            with self.assertRaises(ValueError):
                router.grant_session_consent(role, "gemini")
        router.grant_session_consent("reformulation", "gemini")
        self.assertFalse(router.has_session_consent("reformulation", "claude"))
        self.assertFalse(router.has_session_consent("routage", "gemini"))
        r = Reformulator(router, FakeLLM([reform("locale")]))
        res = r.reformulate("salut", personal=False, ask=lambda q: self.fail("déjà accordé"), out=self.outputs.append)
        # gemini_test (accord) en 429 ; repli claude : confirmation par envoi exigée, refusée -> local.
        self.assertEqual(len(previews), 1)
        self.assertIn("« claude »", previews[0])
        self.assertEqual(fakes["claude"].prompts, [])
        self.assertEqual(res.agent, "qwen_interface")
        # L'accord de reformulation ne dispense pas le routage externe de sa confirmation.
        with self.assertRaises(PreviewRefused):
            router.call("routage", "salut")


# ---------------------------------------------------------------- replis


class FallbackTest(Fixture):
    def run_failure(self, *responses):
        app = self.make([reform("locale"), route_json("discussion", reponse="réponse")])
        self.answer(*responses)
        self.inputs.append("o")
        app.handle("salut")
        self.assertIn("réponse", self.outputs)  # le tour n'est pas cassé
        self.assertIn("Demande comprise (qwen_interface) : locale", self.outputs)
        return app

    def test_repli_sur_429(self):
        self.run_failure(*[HttpResponse(429, {}, err_body(429, "RESOURCE_EXHAUSTED"))] * 3)
        self.assertIn("Reformulation externe indisponible (http_429)", self.text())
        self.assertEqual(self.calls()[-2:], [("gemini_test", "http_429"), ("qwen_interface", "ok")])

    def test_repli_sur_reponse_vide(self):
        self.run_failure(HttpResponse(200, {}, json.dumps({"candidates": []}).encode()))
        self.assertIn("(vide)", self.text())

    def test_quota_journalier_affiche_comme_aujourd_hui(self):
        self.run_failure(DAILY_429)
        self.assertTrue(any(o.startswith("Quota Gemini épuisé pour aujourd'hui") for o in self.outputs))
        self.assertIn(("gemini_test", "quota_journalier"), self.calls())

    def test_echec_local_le_tour_continue_avec_le_texte_d_origine(self):
        app = self.make([SchemaError("cassé"), route_json("discussion", reponse="ok")])
        self.inputs.append("n")
        app.handle("salut")
        self.assertIn("ok", self.outputs)
        self.assertNotIn("Demande reformulée", self.llm.calls[1]["messages"][-1].content)


# ---------------------------------------------------------------- confidentialité


class PrivacyTest(Fixture):
    def test_texte_masque_avant_envoi_et_reinjecte_en_local(self):
        self.make()
        self.answer(reform("relancer [EMAIL_1] pour le devis"))
        r = self.app._ll.reformulator
        res = r.reformulate(f"relance {EMAIL} pour le devis", personal=False, ask=lambda q: True,
                            out=self.outputs.append)
        sent = self.sent_texts()[0]
        self.assertNotIn(EMAIL, sent)
        self.assertIn("[EMAIL_1]", sent)
        self.assertEqual(res.demande, f"relancer {EMAIL} pour le devis")  # réinjection locale

    def test_fuite_detectee_rien_n_est_envoye_repli_local(self):
        self.make([reform("locale")], masker=LeakyMasker(Vault()))
        res = self.app._ll.reformulator.reformulate("texte", personal=False, ask=lambda q: True,
                                                    out=self.outputs.append)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(res.agent, "qwen_interface")
        self.assertIn("Reformulation externe bloquée", self.text())

    def test_donnee_perso_detectee_profil_restrictif_prioritaire(self):
        app = self.make([route_json("discussion", reponse="ok"),
                         reform("locale"), route_json("tache", "relancer le client", profil="brainstorm"),
                         reform("locale"), route_json("discussion", reponse="ok")])
        self.answer(reform("distante"))
        self.inputs.extend(["o", "o"])  # accord donné ; puis exécution de la tâche proposée
        app.handle("salut")
        app.handle(f"relancer {EMAIL} pour le devis")
        self.assertEqual(app.profile.name, "bureautique_fictive")
        self.assertTrue(app.personal_lock)
        self.assertIn("Donnée personnelle détectée (EMAIL)", self.text())
        self.assertFalse(app._ll.reformulator.has_consent())
        self.assertTrue(any(o.startswith("Refusé par la politique de données") for o in self.outputs))
        self.assertEqual(self.factory_calls, [])
        app.handle("une autre question")  # texte sans donnée perso : toujours local, aucune question
        self.assertEqual(len(self.transport.requests), 1)  # seul le « salut » initial est parti
        self.assertNotIn(EMAIL, json.dumps(self.sent_texts()))
        self.assertEqual(app.profile.name, "bureautique_fictive")

    def test_verrou_si_profil_restrictif_introuvable(self):
        app = self.make([reform("locale"), route_json("discussion", reponse="ok")])
        app._ll.profil_perso = "inexistant"
        app.handle(f"écrire à {EMAIL}")
        self.assertEqual(app.profile.name, "brainstorm")
        self.assertTrue(app._personal())
        self.assertTrue(self.router.personal_data())
        self.assertIn("tout envoi externe est bloqué", self.text())
        with self.assertRaises(PolicyError):
            app._check_policy("gemini")
        self.assertEqual(self.transport.requests, [])
        app.handle("/profil brainstorm")  # choix explicite : verrou levé, accord non rétabli
        self.assertFalse(app.personal_lock)
        self.assertFalse(app._ll.reformulator.has_consent())

    def test_detection_par_le_code(self):
        self.assertEqual(personal_kinds(f"écrire à {EMAIL}"), ["EMAIL"])
        self.assertEqual(personal_kinds("un budget de 15000 euros"), [])  # nombre seul : pas de bascule
        self.assertEqual(personal_kinds("fais-moi 10 idées d'appli de recettes"), [])
        self.assertIn("TELEPHONE", personal_kinds("appeler le 06 12 34 56 78"))


# --------------------------------------------------------- commandes et liste blanche


class WhitelistTest(Fixture):
    def test_liste_blanche(self):
        for forbidden in ("escalader", "grand", "profil", "masquer", "quitter", "accord", "valider", "refuser"):
            self.assertNotIn(forbidden, FREE_TEXT_ACTIONS)
        self.assertTrue(CONFIRMED_ACTIONS <= set(FREE_TEXT_ACTIONS))
        for action in ("escalader", "supprimer_tout"):
            with self.subTest(action=action), self.assertRaises(SchemaError):
                route_libre(FakeLLM([route_json(action, "1")]), "escalade l'idée 1")

    def test_arguments_valides_par_le_code(self):
        self.assertIsNone(route_libre(FakeLLM([route_json("detail", "deux")]), "x").command)
        self.assertIsNone(route_libre(FakeLLM([route_json("organisateur", "openai")]), "x").command)
        self.assertIsNone(route_libre(FakeLLM([route_json("tache", " ")]), "x").command)
        p = route_libre(FakeLLM([route_json("revue", "ignoré", profil="auto_projet")]), "x")
        self.assertEqual((p.command.name, p.command.argument, p.profile), ("revue", "", ""))

    def test_phrase_libre_declenche_tache_apres_confirmation(self):
        app = self.make([reform("locale"), route_json("tache", "Propose 10 idées d'appli de recettes")],
                        plans=[plan(("generateur", "idées"))], worker_outputs=["IDEES"])
        self.inputs.extend(["n", "o"])  # pas d'accord ; exécution confirmée
        app.handle("fais-moi 10 idées d'appli de recettes")
        self.assertIn("Commande proposée : /tache Propose 10 idées d'appli de recettes", self.outputs)
        self.assertIn("Exécuter cette commande ? (o/n) ", self.outputs)
        self.assertEqual(len(self.org.plan_calls), 1)

    def test_tache_refusee_rien_n_est_lance(self):
        app = self.make([reform("locale"), route_json("tache", "x")])
        self.inputs.extend(["n", "n"])
        app.handle("des idées ?")
        self.assertIn("Annulé.", self.outputs)
        self.assertEqual(self.factory_calls, [])

    def test_action_gratuite_sans_confirmation(self):
        app = self.make([reform("locale"), route_json("dedup")])
        self.inputs.append("n")
        app.handle("regroupe les doublons")
        self.assertIn("Aucun run pour l'instant.", self.outputs)  # /dedup exécuté, sans question
        self.assertNotIn("Exécuter cette commande ? (o/n) ", self.outputs)

    def test_commandes_slash_inchangees(self):
        app = self.make()
        self.assertEqual(parse_command("/accord non").name, "accord")
        app.handle("/quota")
        self.assertIn("0/1400", self.text())
        app.handle("/aide")
        self.assertIn("/tache", self.text())
        self.assertIn("parlez librement", self.text())
        app.last_review = review("Scan", "Partage")
        app.handle("/refuser 2 trop cher")
        recs = [json.loads(l) for l in (self.tmp / "app" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["idea_id"], r["decision"], r["raison"]) for r in recs], [(2, "refusée", "trop cher")])
        app.handle("/profil")
        self.assertIn("Profil actuel : brainstorm", self.text())
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.transport.requests, [])


# ------------------------------------------------------------------ profil implicite


class ImplicitProfileTest(Fixture):
    def test_fonction_pure(self):
        names = ["auto_projet", "brainstorm", "bureautique_fictive"]
        self.assertEqual(choose_profile("auto_projet", names, current="brainstorm", current_personal=False),
                         "auto_projet")
        self.assertIsNone(choose_profile("profil_invente", names, current="brainstorm", current_personal=False))
        self.assertIsNone(choose_profile("brainstorm", names, current="brainstorm", current_personal=False))
        self.assertEqual(choose_profile("", names, current=None, current_personal=False), "brainstorm")
        self.assertEqual(choose_profile("invente", names, current=None, current_personal=False), "brainstorm")
        self.assertIsNone(choose_profile("brainstorm", names, current="bureautique_fictive", current_personal=True))
        self.assertEqual(explicit_profile("passe en bureautique fictive", names), "bureautique_fictive")
        self.assertIsNone(explicit_profile("des idées de recettes", names))

    def test_profil_propose_par_qwen_et_verifie_par_le_code(self):
        app = self.make([reform("locale"), route_json("tache", "Analyse le projet", profil="auto_projet")],
                        plans=[plan(("generateur", "a"))], worker_outputs=["SORTIE"])
        self.inputs.extend(["n", "o"])
        app.handle("analyse le projet lui-même")
        self.assertEqual(app.profile.name, "auto_projet")
        self.assertIn("Commande proposée : /tache Analyse le projet (profil : auto_projet)", self.outputs)
        line = next(o for o in self.outputs if o.startswith("Profil choisi automatiquement"))
        self.assertEqual(line.count("\n"), 0)  # une seule ligne d'état
        self.assertIn("profil auto_projet", line)
        system = self.llm.calls[1]["messages"][0].content
        self.assertIn("- auto_projet :", system)  # Qwen choisit parmi list_profiles()

    def test_nom_invente_refuse(self):
        app = self.make([reform("locale"), route_json("tache", "x", profil="profil_invente")],
                        plans=[plan(("generateur", "a"))], worker_outputs=["S"])
        self.inputs.extend(["n", "o"])
        app.handle("fais un truc")
        self.assertEqual(app.profile.name, "brainstorm")
        self.assertIn("Commande proposée : /tache x", self.outputs)

    def test_profil_par_defaut_brainstorm(self):
        app = self.make([reform("locale"), route_json("tache", "x")], profile=None,
                        plans=[plan(("generateur", "a"))], worker_outputs=["S"])
        self.inputs.extend(["n", "o"])
        app.handle("fais un truc")
        self.assertEqual(app.profile.name, "brainstorm")

    def test_profil_perso_explicite_avant_tout_envoi(self):
        app = self.make([reform("locale"), route_json("discussion", reponse="ok")])
        app.handle("passe en bureautique_fictive et liste les dossiers")
        self.assertEqual(app.profile.name, "bureautique_fictive")
        self.assertNotIn(ACCORD, self.outputs)
        self.assertEqual(self.transport.requests, [])

    def test_qwen_ne_fait_jamais_sortir_du_profil_perso(self):
        app = self.make([reform("locale"), route_json("tache", "x", profil="brainstorm")],
                        profile="bureautique_fictive")
        self.inputs.append("o")
        app.handle("fais un truc")
        self.assertEqual(app.profile.name, "bureautique_fictive")
        self.assertTrue(any(o.startswith("Refusé par la politique de données") for o in self.outputs))

    def test_aide_ne_rend_plus_profil_obligatoire(self):
        app = self.make()
        app.handle("/aide")
        self.assertIn("Le profil est choisi automatiquement", self.text())


# ------------------------------------------------------------------- retours libres


class FeedbackParsingTest(unittest.TestCase):
    def test_numeros_valides_par_le_code(self):
        doc = json.loads(feedback_json([(2, "garde", ""), (1, "jette", "trop générique"), (9, "garde", "")]))
        p = parse_feedback(doc, [1, 2, 3])
        self.assertEqual([(i.id, i.decision, i.raison) for i in p.items],
                         [(1, "jette", "trop générique"), (2, "garde", "")])
        self.assertEqual(p.unknown, [9])
        self.assertTrue(p.ambiguous)

    def test_tout_et_conflit(self):
        p = parse_feedback(json.loads(feedback_json(tout="jette", raison_tout="hors sujet")), [1, 2])
        self.assertEqual([(i.id, i.decision, i.raison) for i in p.items], [(1, "jette", "hors sujet"),
                                                                           (2, "jette", "hors sujet")])
        self.assertFalse(p.ambiguous)
        p = parse_feedback(json.loads(feedback_json([(1, "garde", ""), (1, "jette", "")])), [1])
        self.assertTrue(p.ambiguous)
        self.assertTrue(parse_feedback(json.loads(feedback_json()), [1]).ambiguous)

    def test_raison_facultative(self):
        for skip in ("", "  ", "sans raison", "Sans raison.", "passer", "-"):
            self.assertTrue(is_skip_reason(skip), skip)
            self.assertEqual(clean_reason(skip), "")
        self.assertEqual(clean_reason("  trop   cher "), "trop cher")
        self.assertEqual(len(clean_reason("x" * 1000)), 300)


class FeedbackAppTest(Fixture):
    LOCAL = (("reformulation", "qwen_interface"),)  # reformulation locale : pas de question d'accord

    def make_review(self, *answers):
        app = self.make([reform("avis"), route_json("retour"), *answers], roles=self.LOCAL)
        app.last_review = review("Scan du frigo", "Partage", "Compost")
        return app

    def decisions(self):
        path = self.tmp / "app" / "decisions.jsonl"
        return [(r["idea_id"], r["decision"], r["raison"]) for r in
                map(json.loads, path.read_text(encoding="utf-8").splitlines())] if path.exists() else []

    def test_garde_et_jette_raison_donnee_dans_la_phrase(self):
        app = self.make_review(feedback_json([(2, "garde", ""), (1, "jette", "trop générique")]))
        self.inputs.extend(["o", ""])  # décisions confirmées ; raison du « garde » : sautée
        app.handle("je garde la 2, trop générique la 1")
        self.assertIn("J'ai compris : garder [2] Partage ; jeter [1] Scan du frigo (raison : trop générique)",
                      self.outputs)
        prompts = [o for o in self.outputs if o.startswith("Pourquoi")]
        self.assertEqual(len(prompts), 1)  # une seule question, jamais pour la raison déjà donnée
        self.assertTrue(prompts[0].startswith("Pourquoi garder [2] Partage ?"))
        self.assertEqual(sorted(self.decisions()), [(1, "refusée", "trop générique"), (2, "validée", "")])
        self.assertEqual([r["titre"] for r in app.refusals], ["Scan du frigo"])  # remonté à l'organisateur

    def test_on_jette_tout_confirme_sans_question_de_raison(self):
        app = self.make_review(feedback_json(tout="jette", raison_tout="hors sujet"))
        self.inputs.append("o")
        app.handle("on jette tout, hors sujet")
        self.assertIn("Appliquer ces décisions ? (o/n) ", self.outputs)
        self.assertIn("« jeter tout » s'applique à toutes les idées non citées", self.text())
        self.assertEqual(self.decisions(), [(1, "refusée", "hors sujet"), (2, "refusée", "hors sujet"),
                                            (3, "refusée", "hors sujet")])
        self.assertFalse(any(o.startswith("Pourquoi") for o in self.outputs))

    def test_raison_demandee_une_fois_et_sautable(self):
        app = self.make_review(feedback_json([(1, "jette", ""), (3, "jette", "")]))
        self.inputs.extend(["o", "sans raison"])
        app.handle("vire la 1 et la 3")
        self.assertEqual(sum(o.startswith("Pourquoi") for o in self.outputs), 1)
        self.assertEqual(self.decisions(), [(1, "refusée", ""), (3, "refusée", "")])

    def test_ambigu_confirmation_demandee(self):
        app = self.make_review(feedback_json([(2, "garde", "")], ambigu=True))
        self.inputs.append("n")
        app.handle("bof la 2 peut-être")
        self.assertIn("Est-ce bien cela ? (o/n) ", self.outputs)
        self.assertIn("Annulé : aucune décision enregistrée.", self.outputs)
        self.assertEqual(self.decisions(), [])
        self.assertFalse(self.journal.path.exists())

    def test_numero_inconnu_rien_n_est_applique(self):
        app = self.make_review(feedback_json([(9, "garde", "")]))
        app.handle("je garde la 9")
        self.assertIn("Je n'ai pas compris", self.text())
        self.assertEqual(self.decisions(), [])

    def test_sans_revue(self):
        app = self.make([reform("avis"), route_json("retour")], roles=self.LOCAL)
        app.handle("je garde la 2")
        self.assertIn("Aucune revue en cours", self.text())


# ------------------------------------------------------- correctifs de relecture


class PersonalReasonTest(Fixture):
    """Raison saisie à « Pourquoi garder/jeter ? » : jamais en clair, jamais vers l'organisateur."""

    def decisions_raw(self):
        return (self.tmp / "app" / "decisions.jsonl").read_text(encoding="utf-8")

    def test_raison_perso_detectee_masquee_et_jamais_transmise(self):
        app = self.make([reform("avis"), route_json("retour"), feedback_json([(1, "jette", "")])],
                        roles=FeedbackAppTest.LOCAL)
        app.last_review = review("Scan du frigo", "Partage")
        self.inputs.extend(["o", f"le client {EMAIL} n'en veut pas"])
        app.handle("vire la 1")
        self.assertIn("Donnée personnelle détectée (EMAIL)", self.text())
        self.assertTrue(app._personal())
        self.assertEqual(app.profile.name, "bureautique_fictive")
        self.assertIsNotNone(app.last_review)  # passage au profil restrictif sans perte des résultats
        raw = self.decisions_raw()
        self.assertNotIn(EMAIL, raw)
        rec = json.loads(raw.splitlines()[0])
        self.assertTrue(rec["perso"])
        self.assertEqual(app.refusals, [])
        self.assertNotIn(EMAIL, app._context())
        self.assertNotIn("Scan du frigo", app._context())
        self.assertIn("ce refus reste local", self.text())
        # Sortie explicite du profil restrictif, puis rechargement : rien n'est réinjecté.
        app.handle("/profil brainstorm")
        self.assertEqual(app.refusals, [])
        again = self.make()
        self.assertEqual(again.refusals, [])
        self.assertEqual(again._context(), "")

    def test_refuser_explicite_avec_donnee_perso(self):
        app = self.make()
        app.last_review = review("Scan", "Partage")
        app.handle(f"/refuser 2 voir avec {EMAIL}")
        self.assertNotIn(EMAIL, self.decisions_raw())
        self.assertTrue(app.personal_lock or app._personal())
        self.assertEqual(app.refusals, [])
        self.assertNotIn(EMAIL, app._context())

    def test_refus_herites_filtres_au_rechargement_et_au_contexte(self):
        path = self.tmp / "app" / "decisions.jsonl"
        path.parent.mkdir(parents=True)
        lines = [{"decision": "refusée", "titre": "Ancien", "raison": f"écrire à {EMAIL}"},  # ancien format
                 {"decision": "refusée", "titre": "Marqué", "raison": "x", "perso": True},
                 {"decision": "refusée", "titre": "Propre", "raison": "trop cher"}]
        path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
        app = self.make()
        self.assertEqual([r["titre"] for r in app.refusals], ["Propre"])
        app.refusals.append({"titre": "Injecté", "raison": f"tel {EMAIL}"})  # défense en profondeur
        ctx = app._context()
        self.assertIn("Propre (raison : trop cher)", ctx)
        self.assertNotIn(EMAIL, ctx)
        self.assertNotIn("Injecté", ctx)

    def test_changement_vers_profil_perso_filtre_les_refus(self):
        app = self.make()
        app.refusals = [{"titre": "A", "raison": ""}, {"titre": "B", "raison": "", "perso": True}]
        app.handle("/profil bureautique_fictive")
        self.assertEqual([r["titre"] for r in app.refusals], ["A"])


class FeedbackConfirmationTest(Fixture):
    def test_jette_refuse_rien_n_est_enregistre(self):
        app = self.make([reform("avis"), route_json("retour"), feedback_json([(1, "jette", "trop cher")])],
                        roles=FeedbackAppTest.LOCAL)
        app.last_review = review("Scan", "Partage")
        self.inputs.append("n")
        app.handle("on jette la 1, trop cher")
        self.assertIn("Appliquer ces décisions ? (o/n) ", self.outputs)
        self.assertIn("Annulé : aucune décision enregistrée.", self.outputs)
        self.assertFalse((self.tmp / "app" / "decisions.jsonl").exists())
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(app.refusals, [])

    def test_garde_seule_aussi_confirmee(self):
        app = self.make([reform("avis"), route_json("retour"), feedback_json([(2, "garde", "utile")])],
                        roles=FeedbackAppTest.LOCAL)
        app.last_review = review("Scan", "Partage")
        self.inputs.append("")  # pas de réponse : non confirmé
        app.handle("je garde la 2, utile")
        self.assertFalse((self.tmp / "app" / "decisions.jsonl").exists())


class ExplicitProfileTest(Fixture):
    NAMES = ["auto_projet", "brainstorm", "bureautique_fictive"]

    def test_mots_banals_ignores(self):
        for text in ("fais un brainstorm sur les recettes", "brainstorm rapide stp",
                     "un auto projet ?", "les profils sont nombreux"):
            self.assertIsNone(explicit_profile(text, self.NAMES), text)
        for text, name in (("passe en profil brainstorm", "brainstorm"), ("utilise auto_projet", "auto_projet"),
                           ("profil : « auto_projet »", "auto_projet"), ("bascule en mode brainstorm", "brainstorm"),
                           ("passe en bureautique fictive", "bureautique_fictive")):
            self.assertEqual(explicit_profile(text, self.NAMES), name, text)

    def test_mot_banal_ne_change_pas_de_profil_ni_les_resultats(self):
        app = self.make([reform("locale"), route_json("discussion", reponse="ok")], roles=FeedbackAppTest.LOCAL,
                        profile="auto_projet")
        app.last_review = review("Scan")
        app.handle("fais un brainstorm sur les recettes")
        self.assertEqual(app.profile.name, "auto_projet")
        self.assertIsNotNone(app.last_review)

    def test_retour_ne_change_jamais_de_profil(self):
        app = self.make([reform("avis"), route_json("retour"), feedback_json([(1, "garde", "")])],
                        roles=FeedbackAppTest.LOCAL, profile="auto_projet")
        app.last_review = review("Scan", "Partage")
        self.inputs.extend(["n"])
        app.handle("je garde la 1, et passe en profil brainstorm")
        self.assertEqual(app.profile.name, "auto_projet")
        self.assertIsNotNone(app.last_review)

    def test_demande_explicite_conserve_les_resultats(self):
        app = self.make([reform("locale"), route_json("discussion", reponse="ok")], roles=FeedbackAppTest.LOCAL)
        sentinel = object()
        app.last_report, app.last_review = sentinel, review("Scan")
        app.handle("passe en profil auto_projet")
        self.assertEqual(app.profile.name, "auto_projet")
        self.assertIs(app.last_report, sentinel)
        self.assertIsNotNone(app.last_review)
        self.assertIn("Résultats en cours conservés.", self.text())


class AccordFallbackTest(Fixture):
    ROLES = (("reformulation", "qwen_interface,gemini_test"),)  # principal local, repli externe

    def test_accord_oui_sans_effet_si_principal_local(self):
        app = self.make(roles=self.ROLES)
        app.handle("/accord oui")
        r = app._ll.reformulator
        self.assertFalse(r.has_consent())
        self.assertFalse(self.router.has_session_consent("reformulation", "gemini"))
        self.assertIn("repli externe possible vers Gemini", self.outputs[-1])
        self.assertIn("confirmé (o/n)", self.outputs[-1])
        self.assertNotIn("Accord de session donné", self.text())
        # Le repli externe reste soumis à la confirmation par envoi : refusée, rien ne part.
        self.llm.answers.extend([LocalLLMError("hors ligne"), reform("locale")])
        self.inputs.append("n")
        r.reformulate("salut", personal=False, ask=lambda q: self.fail("pas de question d'accord"),
                      out=self.outputs.append)
        self.assertIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        self.assertEqual(self.transport.requests, [])


class SessionConsentReadOnlyTest(unittest.TestCase):
    def test_lecture_sans_effet_de_bord_revocation_explicite(self):
        flag = [False]
        reg, fakes = fake_registry(gemini=[reform("x")])
        router = router_for(reg, settings(agent_roles=(("reformulation", "gemini"),)), personal_data=lambda: flag[0])
        router.grant_session_consent("reformulation", "gemini")
        flag[0] = True
        self.assertFalse(router.has_session_consent("reformulation", "gemini"))
        self.assertEqual(router._session_consent, {("reformulation", "gemini")})  # lecture : état intact
        with self.assertRaises(PolicyError):
            router.call("reformulation", "p")
        self.assertEqual(router._session_consent, set())  # révocation explicite dans le chemin d'envoi
        flag[0] = False
        self.assertFalse(router.has_session_consent("reformulation", "gemini"))
        self.assertEqual(fakes["gemini"].prompts, [])


# --------------------------------------------------------------- journal et réinjection


class JournalTest(Fixture):
    def test_ecriture_lecture_et_masquage(self):
        app = self.make([reform("avis"), route_json("retour"), feedback_json([(1, "jette", "")])],
                        roles=FeedbackAppTest.LOCAL)
        app.last_review = review(f"Relancer {EMAIL}")
        self.inputs.extend(["o", f"le client {EMAIL} n'en veut pas"])  # raison contenant une donnée fictive
        app.handle("vire la 1")
        raw = self.journal.path.read_text(encoding="utf-8")
        self.assertNotIn(EMAIL, raw)
        recs = self.journal.read()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(set(r), {"date", "profil", "perso", "role", "agent", "decision", "idee", "raison"})
        # La donnée détectée dans la raison impose le profil restrictif : retour marqué perso.
        self.assertEqual((r["profil"], r["perso"], r["role"], r["agent"], r["decision"]),
                         ("bureautique_fictive", True, "worker", "gemini", "jette"))
        self.assertIn("[EMAIL_1]", r["raison"])  # masqué par le Masker de l'application
        self.assertNotIn(KEY, raw)
        self.assertEqual(self.journal.path, self.tmp / "app" / "retours.jsonl")

    def test_lecture_tolerante(self):
        j = FeedbackJournal(self.tmp / "r.jsonl")
        self.assertEqual(j.read(), [])
        j.append([{"a": 1}])
        with open(j.path, "a", encoding="utf-8") as f:
            f.write("pas du json\n[1, 2]\n")
        j.append([{"b": 2}])
        self.assertEqual(j.read(), [{"a": 1}, {"b": 2}])
        self.assertEqual(j.read(limit=1), [{"b": 2}])

    def test_enregistrement_sans_masker_masquage_deterministe(self):
        rec = feedback_record(profile="brainstorm", personal=False, role="worker", agent="gemini", decision="garde",
                              idea=f"Écrire à {EMAIL}", reason="appeler le 06 12 34 56 78")
        self.assertNotIn(EMAIL, json.dumps(rec))
        self.assertIn("[EMAIL]", rec["idee"])
        self.assertIn("[TELEPHONE]", rec["raison"])


def rec(i, profil="brainstorm", perso=False, decision="jette", raison="trop cher"):
    return {"profil": profil, "perso": perso, "decision": decision, "idee": f"Idée {i}", "raison": raison}


class ReinjectionTest(Fixture):
    def test_fonction_pure_bornee(self):
        records = [rec(i) for i in range(1, 11)] + [rec(99, profil="auto_projet"), rec(98, perso=True),
                                                    "pas un objet", {"decision": "garde"}]
        text = feedback_examples(records, profile="brainstorm", personal=False, n=3)
        self.assertEqual(text.splitlines(), ["- idée jetée : Idée 8 (raison : trop cher)",
                                             "- idée jetée : Idée 9 (raison : trop cher)",
                                             "- idée jetée : Idée 10 (raison : trop cher)"])
        self.assertEqual(feedback_examples(records, profile="brainstorm", personal=True, n=3), "")
        self.assertEqual(feedback_examples(records, profile="brainstorm", personal=False, n=0), "")
        long = [rec(i, raison="x" * 500) for i in range(50)]
        bounded = feedback_examples(long, profile="brainstorm", personal=False, n=50, max_chars=400)
        self.assertLessEqual(len(bounded), 400)
        self.assertTrue(all(len(l) <= 160 for l in bounded.splitlines()))
        self.assertIn("[EMAIL]", feedback_examples([rec(1, raison=f"voir {EMAIL}")], profile="brainstorm",
                                                   personal=False, n=5))

    def test_injectee_dans_les_prompts_de_qwen(self):
        app = self.make([reform("locale"), route_json("fiche", "un outil de devis"),
                         json.dumps({"objectif": "o", "contraintes": [], "perimetre": [], "hors_perimetre": []})],
                        roles=FeedbackAppTest.LOCAL, n_exemples=2)
        self.journal.append([rec(1), rec(2, decision="garde", raison=""), rec(3)])
        self.inputs.extend(["o", "n"])  # exécuter /fiche ; fiche non validée
        app.handle("je veux un outil de devis")
        for call in self.llm.calls:  # reformulation, routage, cadrage
            prompt = call["messages"][-1].content
            self.assertIn("<<<RETOURS", prompt)
            self.assertIn("Idée 3", prompt)
            self.assertNotIn("Idée 1", prompt)  # N = 2 derniers seulement
        self.assertEqual(len(self.llm.calls), 3)

    def test_desactivee_en_profil_perso(self):
        app = self.make([reform("locale"), route_json("discussion", reponse="ok")],
                        profile="bureautique_fictive")
        self.journal.append([rec(1, profil="bureautique_fictive"), rec(2, profil="bureautique_fictive", perso=True)])
        app.handle("bonjour")
        self.assertTrue(all("RETOURS" not in c["messages"][-1].content for c in self.llm.calls))


# ------------------------------------------------------------ file unique du limiteur


class SingleQueueTest(WiringFixture):
    REFORM = reform("demande")

    def test_reformulation_par_defaut_via_le_client_de_l_application(self):
        self.llm.answers.append(json.dumps({"action": "discussion", "argument": "", "reponse": "ok"}))
        self.answer(self.REFORM)
        self.inputs.append("o")
        app = self.app(self.components(profile=load_profile("brainstorm")))
        app.handle("salut")
        self.assertEqual(self.limiter.used_today, 1)
        self.assertIn(f"/models/{self.settings.gemini_model}:", self.transport.requests[0]["url"])
        self.assertEqual(self.extra_clients, [])  # le GeminiClient de l'application, pas une copie
        self.assertEqual(self.client.requests_sent, 1)
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertEqual(calls, [("reformulation", "gemini", "accord_session_donne"),
                                 ("reformulation", "gemini", "ok")])

    def test_gemini_test_meme_file_et_meme_compteur(self):
        s = replace(self.settings, gemini_test_model=TEST_MODEL, agent_roles=(("reformulation", "gemini_test"),))
        c = self.components(s, load_profile("brainstorm"))
        self.assertIs(c.router.registry.get("gemini_test").client.limiter, self.limiter)
        self.llm.answers.extend([json.dumps({"action": "discussion", "argument": "", "reponse": "ok"})] * 2)
        self.answer(self.REFORM, self.REFORM)
        self.inputs.append("o")
        app = self.app(c)
        app.handle("salut")
        app.handle("encore")
        self.assertEqual(self.limiter.used_today, 2)
        self.assertEqual(self.clock.sleeps, [GeminiQuota().min_interval_s])  # espacement commun
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*quota*.json")), ["gemini_quota.json"])
        self.assertTrue(all(f"/models/{TEST_MODEL}:" in r["url"] for r in self.transport.requests))
        self.assertEqual(app._quota_info()[0], 2)

    def test_journal_des_retours_dans_le_dossier_d_etat(self):
        c = self.components(profile=load_profile("brainstorm"))
        ll = c.app_kwargs["langage_libre"]
        self.assertEqual(ll.journal.path, self.tmp / "app" / "retours.jsonl")
        self.assertEqual(ll.n_exemples, self.settings.feedback_exemples)
        self.assertEqual(ll.reformulator.router.routes["reformulation"].agent, "gemini")


if __name__ == "__main__":
    unittest.main()

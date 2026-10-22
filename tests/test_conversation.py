"""Mode conversation : parler à l'assistant sans aucune commande.

Doubles uniquement : aucun modèle réel, aucun appel réseau, jamais .env.
"""

import json
import os
import re
import unittest
from dataclasses import replace
from unittest import mock

import config
from agents import AgentRouter, AgentSpec, LocalAgent, build_registry, resolve_routes
from config import Settings
from orchestrator import Orchestrator
from privacy.masker import Masker
from privacy.vault import Vault
from profiles.loader import list_profiles, load_profile
from qwen_ui import conversation as conv
from qwen_ui.app import App
from qwen_ui.bootstrap import make_app
from qwen_ui.langage_libre import FeedbackJournal, LangageLibre, Reformulator
from tests.test_langage_libre import EMAIL, TEST_MODEL, Fixture, feedback_json, review
from tests.test_orchestrator import RecordingWorker, ScriptedOrganizer, plan, spec
from tests.test_promise import FakeLLM
from tests.test_wiring import WiringFixture
from verify.basic import basic_verifier
from verify.schema import SchemaError

LOCAL = (("routage", "qwen"),)  # conversation comprise par le Qwen local : aucun accord à demander
COMMAND_RE = re.compile(r"(?<![\w/])/(tache|revue|dedup|valider|refuser|escalader|detail|resume|fiche|profil|accord|aide)\b")


def say(action="discussion", argument="", reponse="", profil=""):
    return json.dumps({"action": action, "argument": argument, "reponse": reponse, "profil": profil},
                      ensure_ascii=False)


def fiche_json(objectif="Réduire le gaspillage alimentaire", contraintes=("gratuite",)):
    return json.dumps({"objectif": objectif, "contraintes": list(contraintes), "perimetre": ["Android"],
                       "hors_perimetre": []}, ensure_ascii=False)


IDEAS = json.dumps({"idees": [{"titre": "Frigo scanné", "probleme": "p", "approche": "a", "effort": "faible",
                               "type": "simple"},
                              {"titre": "Recettes des restes", "probleme": "p", "approche": "a", "effort": "moyen",
                               "type": "ambitieux"}]}, ensure_ascii=False)


class ConvFixture(Fixture):
    """App réelle en mode conversation, routeur réel ; Qwen = FakeLLM, Gemini = transport simulé."""

    def make(self, llm_answers=(), *, roles=LOCAL, profile="brainstorm", plans=(), worker_outputs=(),
             review_factory=None):
        self.llm = FakeLLM(list(llm_answers))
        s = replace(self.settings, agent_roles=roles)
        reg = build_registry(s, gemini_limiter=self.limiter, gemini_client_factory=self.gemini_factory)
        for name in ("qwen", "qwen_interface", "qwen_format"):
            reg.register(LocalAgent(AgentSpec(name, "ollama", "qwen-factice"), self.llm))
        box = []
        self.masker = Masker(Vault())
        self.router = AgentRouter(reg, resolve_routes(s, None, reg.names()), logger=self.logger,
                                  personal_data=lambda: bool(box and box[0]._personal()), masker=self.masker,
                                  declared=lambda: box[0]._declared() if box else [], confirm=self.confirm)
        self.journal = FeedbackJournal(self.tmp / "app" / "retours.jsonl")
        ll = LangageLibre(Reformulator(self.router, self.llm), self.journal)
        self.org = ScriptedOrganizer(list(plans))
        self.worker = RecordingWorker(spec("generateur"), list(worker_outputs))

        def factory(name, prof=None):
            self.factory_calls.append(name)
            return Orchestrator(self.org, [self.worker], basic_verifier, logs_dir=self.tmp / "logs")

        app = App(self.llm, factory, quota_info=lambda: (self.limiter.used_today, 1400), state_dir=self.tmp / "app",
                  input_fn=self.input_fn, output_fn=self.outputs.append,
                  profile=load_profile(profile) if profile else None, profile_loader=load_profile,
                  profile_names=list_profiles, masker_factory=lambda: self.masker, langage_libre=ll,
                  role_llms={"routage": self.router.role_llm("routage"), "cadrage": self.router.role_llm("cadrage"),
                             "interface": self.router.role_llm("interface")},
                  review_factory=review_factory, conversation=True)
        box.append(app)
        self.app = app
        return app

    def prompts(self):
        """Messages « utilisateur » reçus par le Qwen factice."""
        return [c["messages"][-1].content for c in self.llm.calls]

    def no_command_shown(self):
        shown = [o for o in self.outputs if not o.startswith("=== Destinataire")]
        leaks = [o for o in shown if COMMAND_RE.search(o)]
        self.assertEqual(leaks, [], "une commande « / » est encore affichée")


# ------------------------------------------------------------------ fonctions pures


class YesNoTest(unittest.TestCase):
    def test_oui_naturels(self):
        for text in ("oui", "o", "Oui !", "ok", "vas-y", "Vas y stp", "ouais go", "d'accord", "c'est parti",
                     "oui merci", "carrément", "parfait", "OK, lance", "bien sûr", "allez on y va"):
            with self.subTest(text=text):
                self.assertIs(conv.parse_yes_no(text), True)

    def test_non_naturels(self):
        for text in ("non", "n", "Non merci", "laisse tomber", "pas maintenant", "annule", "nan", "stop"):
            with self.subTest(text=text):
                self.assertIs(conv.parse_yes_no(text), False)

    def test_entree_seule_ne_lance_rien(self):
        self.assertIs(conv.parse_yes_no(""), False)
        self.assertIs(conv.parse_yes_no("   "), False)

    def test_correction_ou_autre_demande(self):
        for text in ("plutôt 20 idées", "oui mais 20 idées", "non, plutôt sur les recettes", "c'est nul",
                     "si tu peux, en anglais", "oui non"):
            with self.subTest(text=text):
                self.assertIsNone(conv.parse_yes_no(text))

    def test_quitter_et_aide(self):
        self.assertTrue(conv.is_quit("Au revoir !"))
        self.assertTrue(conv.is_quit("bye"))
        self.assertFalse(conv.is_quit("salut"))
        self.assertTrue(conv.is_help("aide"))
        self.assertFalse(conv.is_help("aide-moi à trouver des idées"))


class HistoryTest(unittest.TestCase):
    def test_borne_et_sans_le_dernier_message(self):
        h = conv.History(max_turns=3, turn_chars=20)
        for i in range(5):
            h.user(f"message {i}")
        self.assertEqual(len(h), 3)
        self.assertEqual(h.render(skip_last=True), "utilisateur : message 2\nutilisateur : message 3")
        h.assistant("x" * 50)
        self.assertTrue(h.render().endswith("…"))
        self.assertLessEqual(len(h.render(max_chars=30)), 30)
        h.clear()
        self.assertEqual(h.render(), "")


class ValidationTest(unittest.TestCase):
    def test_liste_blanche(self):
        for forbidden in ("organisateur", "grand", "masquer", "quitter", "profil", "accord", "effacer"):
            self.assertNotIn(forbidden, conv.CONV_ACTIONS)
        with self.assertRaises(SchemaError):
            conv.converse(FakeLLM([say("grand", "claude")]), "passe sur claude")

    def test_arguments_valides_par_le_code(self):
        v = conv.validate
        self.assertTrue(v({"action": "tache", "argument": "  ", "reponse": ""}).is_reply)
        self.assertTrue(v({"action": "detail", "argument": "4", "reponse": ""}, n_outputs=3).is_reply)
        self.assertEqual(v({"action": "detail", "argument": "2", "reponse": ""}, n_outputs=3).argument, "2")
        self.assertTrue(v({"action": "escalader", "argument": "9", "reponse": ""}, idea_ids=[1, 2]).is_reply)
        self.assertTrue(v({"action": "escalader", "argument": "1 tout", "reponse": ""}, idea_ids=[1]).is_reply)
        self.assertEqual(v({"action": "escalader", "argument": "1 Force", "reponse": ""}, idea_ids=[1]).argument,
                         "1 force")
        self.assertTrue(v({"action": "confidentialite", "argument": "partout", "reponse": ""}).is_reply)
        p = v({"action": "revue", "argument": "ignoré", "reponse": "ignoré", "profil": "auto_projet"})
        self.assertEqual((p.action, p.argument, p.reply, p.profile), ("revue", "", "", ""))
        p = v({"action": "tache", "argument": "10 idées", "reponse": "", "profil": "auto_projet"})
        self.assertEqual((p.argument, p.profile), ("10 idées", "auto_projet"))

    def test_prompt_contient_etat_historique_et_message(self):
        llm = FakeLLM([say(reponse="salut")])
        conv.converse(llm, "et la 3 ?", history="utilisateur : bonjour", state="Fiche projet : aucune",
                      profiles=[("brainstorm", "idées")])
        system, prompt = llm.calls[0]["messages"][0].content, llm.calls[0]["messages"][-1].content
        self.assertIn("- brainstorm : idées", system)
        self.assertIn("<<<HISTORIQUE\nutilisateur : bonjour\nHISTORIQUE>>>", prompt)
        self.assertIn("<<<ETAT\nFiche projet : aucune\nETAT>>>", prompt)
        self.assertTrue(prompt.endswith("<<<MESSAGE\net la 3 ?\nMESSAGE>>>"))


class NaturalizeTest(unittest.TestCase):
    def test_plus_de_commandes_dans_les_messages_connus(self):
        for text in ("/detail <1-3> pour lire une sortie complète, /resume pour un résumé.",
                     "Validée(s) : [2] X. Pour faire étudier une idée par le grand modèle : /escalader 2",
                     "L'idée 4 n'est pas validée : /valider 4 d'abord.",
                     "Refusée(s) : [1] X. Ce refus sera transmis à l'organisateur au prochain /tache.",
                     "Fiche enregistrée. Elle sera transmise aux agents avec chaque /tache.",
                     "Aucun run pour l'instant : lancez d'abord /tache.",
                     "Aucune revue pour l'instant : lancez /revue.",
                     "profil brainstorm — envois externes après confirmation. /profil pour changer.",
                     "Pas de décision. Pour forcer : /escalader 3 force",
                     "Shortlist\n\nDécision : /valider <numéros> | /refuser <numéros> [raison] | /tache ... pour relancer",
                     "Le modèle a renvoyé une réponse mal formée (x). Réessayez ou utilisez une commande explicite."):
            with self.subTest(text=text):
                out = conv.naturalize(text)
                self.assertIsNone(COMMAND_RE.search(out), out)
        self.assertIn("« approfondis la 2 »", conv.naturalize("modèle : /escalader 2"))
        self.assertIn("« je garde la 4 »", conv.naturalize("pas validée : /valider 4 d'abord."))

    def test_chemins_et_texte_ordinaire_intacts(self):
        for text in ("Journal : /tmp/a/logs/run-1.jsonl", "C:\\Users\\x\\logs\\run.jsonl", "50/50", "3 idées"):
            self.assertEqual(conv.naturalize(text), text)


# ------------------------------------------------------------------ application


class ConversationAppTest(ConvFixture):
    def test_discussion_naturelle_et_memoire(self):
        app = self.make([say(reponse="Salut ! Dis-moi ton projet."), say(reponse="Avec plaisir.")])
        app.handle("salut")
        app.handle("tu te souviens de ce que j'ai dit ?")
        self.assertIn("Salut ! Dis-moi ton projet.", self.outputs)
        second = self.prompts()[1]
        self.assertIn("utilisateur : salut\nassistant : Salut ! Dis-moi ton projet.", second)
        self.assertNotIn("tu te souviens", second.split("<<<MESSAGE")[0])  # pas en double dans l'historique
        self.assertEqual(self.transport.requests, [])
        self.no_command_shown()

    def test_tache_confirmee_en_langage_naturel(self):
        app = self.make([say("tache", "Propose 2 idées d'appli anti-gaspi")],
                        plans=[plan(("generateur", "2 idées"))], worker_outputs=[IDEAS])
        self.inputs.append("vas-y")
        app.handle("trouve-moi 2 idées d'appli anti-gaspi")
        question = [o for o in self.outputs if o.endswith("(oui/non) ")]
        self.assertEqual(len(question), 1)
        self.assertIn("« Propose 2 idées d'appli anti-gaspi »", question[0])
        self.assertIn("On y va ?", question[0])
        self.assertEqual(len(self.org.plan_calls), 1)
        self.assertIn("Voilà, 2 idée(s) reçue(s).", self.text())
        self.assertNotIn("Commande proposée", self.text())
        self.no_command_shown()
        # Le résultat est mémorisé pour la suite de la conversation.
        self.assertIn("[1] Frigo scanné", app._history.render())

    def test_non_rien_n_est_lance(self):
        app = self.make([say("tache", "x")])
        self.inputs.append("non merci")
        app.handle("des idées ?")
        self.assertIn("D'accord, je ne lance rien.", self.outputs)
        self.assertEqual(self.factory_calls, [])

    def test_correction_au_lieu_de_oui(self):
        app = self.make([say("tache", "Propose 10 idées"), say("tache", "Propose 20 idées")],
                        plans=[plan(("generateur", "20 idées"))], worker_outputs=[IDEAS])
        self.inputs.extend(["plutôt 20 idées", "oui"])
        app.handle("trouve-moi 10 idées")
        self.assertEqual(len(self.org.plan_calls), 1)  # une seule tâche lancée : la corrigée
        second = self.prompts()[1]
        self.assertIn("Proposé : confier aux agents « Propose 10 idées »", second)
        self.assertIn("<<<MESSAGE\nplutôt 20 idées\nMESSAGE>>>", second)
        self.assertIn("« Propose 20 idées »", "\n".join(o for o in self.outputs if o.endswith("(oui/non) ")))

    def test_corrections_bornees(self):
        app = self.make([say("tache", "a"), say("tache", "b"), say("tache", "c")])
        self.inputs.extend(["autre chose", "encore autre chose", "et encore"])
        app.handle("des idées")
        self.assertEqual(len(self.llm.calls), 3)  # message + 2 corrections au plus, jamais de boucle
        self.assertEqual(self.factory_calls, [])

    def test_fiche_proposee_puis_validee(self):
        app = self.make([say("fiche", "Appli Android gratuite contre le gaspillage"), fiche_json()])
        self.inputs.append("oui c'est ça")
        app.handle("j'ai un projet d'appli android gratuite contre le gaspillage")
        self.assertIn("C'est bien ça ? (oui/non) ", self.outputs)
        self.assertIsNotNone(app.fiche)
        self.assertTrue((self.tmp / "app" / "fiche_projet.json").exists())
        self.no_command_shown()

    def test_fiche_corrigee(self):
        app = self.make([say("fiche", "Appli gratuite"), fiche_json(contraintes=("gratuite",)),
                         say("fiche", "Appli gratuite, hors ligne"), fiche_json(contraintes=("gratuite", "hors ligne"))])
        self.inputs.extend(["il faut aussi que ça marche hors ligne", "oui"])
        app.handle("mon projet : une appli gratuite")
        self.assertEqual(app.fiche.contraintes, ["gratuite", "hors ligne"])
        self.assertIn("Fiche proposée", self.prompts()[2])  # la correction voit la fiche proposée

    def test_quitter_et_aide_sans_modele(self):
        app = self.make()
        app.handle("aide")
        self.assertIn("sans aucune commande", self.text())
        self.no_command_shown()
        self.assertFalse(app.handle("au revoir"))
        self.assertEqual(self.llm.calls, [])

    def test_commandes_toujours_utilisables_et_non_reecrites(self):
        app = self.make()
        app.handle("/aide")
        self.assertIn("/tache", self.text())
        app.handle("/quota")
        self.assertIn("0/1400", self.text())
        self.assertEqual(self.llm.calls, [])

    def test_revue_sans_idees_aucune_question(self):
        app = self.make([say("revue")])
        app.handle("fais-les noter")
        self.assertIn("Il n'y a pas encore d'idées à noter : demande-moi d'abord des idées.", self.outputs)
        self.assertFalse(any(o.endswith("(oui/non) ") for o in self.outputs))

    def test_revue_demande_confirmation(self):
        calls = []
        app = self.make([say("tache", "2 idées"), say("revue")], plans=[plan(("generateur", "2 idées"))],
                        worker_outputs=[IDEAS], review_factory=lambda prof: calls.append(prof))
        self.inputs.extend(["oui", "pas maintenant"])
        app.handle("2 idées stp")
        app.handle("fais-les noter")
        self.assertIn("Je fais noter les idées par les relecteurs (gemini). On y va ? (oui/non) ", self.outputs)
        self.assertIn("D'accord, on ne les note pas pour l'instant.", self.outputs)
        self.assertEqual(calls, [])  # aucun relecteur construit ni appelé

    def test_retour_libre_applique_apres_oui(self):
        app = self.make([feedback_json([(2, "garde", "utile"), (1, "jette", "trop cher")])])  # avis reconnu par le code
        app.last_review = review("Scan", "Partage")
        self.inputs.append("ok")
        app.handle("je garde la 2, la 1 est trop chère")
        recs = [json.loads(l) for l in (self.tmp / "app" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted((r["idea_id"], r["decision"]) for r in recs), [(1, "refusée"), (2, "validée")])
        self.assertIn("« approfondis la 2 »", self.text())
        self.no_command_shown()
        # L'état transmis au modèle connaît désormais les décisions.
        self.assertIn("gardée par l'utilisateur", app._conv_state())

    def test_avis_reconnu_par_le_code_sans_appel_de_comprehension(self):
        # Vu en réel : « je garde la 1, elle me plaît bien » renvoyé en question par le modèle.
        app = self.make([feedback_json([(1, "garde", "elle me plaît bien")])])
        app.last_review = review("Scan", "Partage")
        self.inputs.append("oui")
        app.handle("je garde la 1, elle me plaît bien")
        self.assertEqual(len(self.llm.calls), 1)  # seulement l'interprétation de l'avis
        self.assertTrue((self.tmp / "app" / "decisions.jsonl").exists())

    def test_reconnaissance_des_avis(self):
        ids = [1, 2, 6]
        for text in ("je garde la 1", "on vire la 2 et la 6", "jette tout", "je retiens la 6", "la 2 je la supprime"):
            with self.subTest(text=text):
                self.assertTrue(conv.looks_like_feedback(text, ids))
        for text in ("approfondis la 2", "jette un œil à la 1", "montre la 2", "garde la 9", "je garde la 1"):
            with self.subTest(text=text):
                self.assertFalse(conv.looks_like_feedback(text, ids if text != "je garde la 1" else []))
        self.assertFalse(conv.looks_like_feedback("trouve 12 idées", ids))

    def test_numero_inconnu_refuse_par_le_code(self):
        app = self.make([say("escalader", "7")])
        app.last_review = review("Scan", "Partage")
        app.handle("approfondis la 7")
        self.assertIn("Quelle idée veux-tu faire approfondir ? Donne-moi son numéro.", self.outputs)

    def test_etat_resume_par_le_code(self):
        app = self.make()
        app.last_review = review("Scan", "Partage")
        state = app._conv_state()
        self.assertIn("Profil : brainstorm", state)
        self.assertIn("Fiche projet : aucune", state)
        self.assertIn("1 : Scan — 7.0/10, approuvée", state)


class ConversationPrivacyTest(ConvFixture):
    def test_donnee_perso_tout_reste_local(self):
        roles = (("routage", "gemini_test"), ("reformulation", "gemini_test"))
        app = self.make([say(reponse="Je note.")], roles=roles)
        app.handle(f"écris à {EMAIL} pour le devis")
        self.assertTrue(app._personal())
        self.assertEqual(self.transport.requests, [])  # rien n'est parti
        self.assertFalse(any("Pour bien te comprendre" in o for o in self.outputs))  # aucun accord demandé

    def test_accord_demande_une_fois_refus_qwen_local_sans_question_par_message(self):
        roles = (("routage", "gemini_test"), ("reformulation", "gemini_test"))
        app = self.make([say(reponse="un"), say(reponse="deux")], roles=roles)
        self.inputs.append("non")
        app.handle("salut")
        app.handle("ça va ?")
        asked = [o for o in self.outputs if o.startswith("Pour bien te comprendre")]
        self.assertEqual(len(asked), 1)
        self.assertIn("Gemini (quota gratuit)", asked[0])
        self.assertEqual(self.transport.requests, [])
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)
        self.assertEqual(len(self.llm.calls), 2)  # Qwen local a répondu aux deux messages
        self.assertIn("deux", self.outputs)

    def test_accord_donne_agent_externe_sans_question_par_envoi(self):
        roles = (("routage", "gemini_test"), ("reformulation", "gemini_test"))
        app = self.make([], roles=roles)
        self.answer(say(reponse="Bonjour !"), say(reponse="Re !"))
        self.inputs.append("oui")
        app.handle("salut")
        app.handle("re")
        self.assertEqual(len(self.transport.requests), 2)
        self.assertIn("Re !", self.outputs)
        self.assertTrue(self.router.has_session_consent("routage", "gemini"))
        self.assertNotIn("Envoyer ce texte à un agent externe ? (o/n) ", self.outputs)  # couvert par l'accord
        self.assertEqual(self.llm.calls, [])
        self.assertIn("utilisateur : salut", self.sent_texts()[1])  # l'historique accompagne le message

    def test_garde_tout_en_local(self):
        roles = (("routage", "gemini_test"), ("reformulation", "gemini_test"))
        app = self.make([], roles=roles)
        self.answer(say("confidentialite", "local"))
        self.inputs.append("oui")
        app.handle("garde tout en local")
        self.assertFalse(self.router.has_session_consent("routage", "gemini"))
        self.assertIn("je te comprends ici, sur ton PC", self.text())
        self.llm.answers.append(say(reponse="local"))
        app.handle("salut")
        self.assertEqual(len(self.transport.requests), 1)  # plus rien après la révocation
        self.assertIn("local", self.outputs)

    def test_quitter_le_profil_perso_vide_l_historique(self):
        app = self.make([say(reponse="noté")])
        app.handle(f"mon mail : {EMAIL}")
        self.assertTrue(len(app._history))
        app.handle("/profil brainstorm")
        self.assertEqual(len(app._history), 0)


class ConversationScenarioTest(ConvFixture):
    def test_parcours_complet_sans_aucune_commande(self):
        from review import IdeaVerdict, ReviewReport

        class Panel:
            reviewers = ["a", "b", "c"]
            max_requests = 3

            def review(self, ideas, fiche):
                verdicts = [IdeaVerdict(i, {}, 2, True, 7.5, True, "approuvée") for i in ideas]
                return ReviewReport("rev-x", verdicts, verdicts, [], 6.5, 2)

        app = self.make([say(reponse="Salut ! Parle-moi de ton projet."),
                         say("fiche", "Appli Android anti-gaspi gratuite"), fiche_json(),
                         say("tache", "Propose 2 idées de fonctionnalités"),
                         say("dedup"), json.dumps({"groupes": []}),
                         say("revue"),
                         feedback_json([(1, "garde", "")]),  # « je garde la 1 » : reconnu par le code
                         say("detail", "1"),
                         say("quota")],
                        plans=[plan(("generateur", "2 idées"))], worker_outputs=[IDEAS],
                        review_factory=lambda prof: Panel())
        self.inputs.extend(["oui", "oui", "oui", "oui", ""])  # fiche, tâche, revue, retour, raison passée
        for line in ("salut", "j'ai un projet d'appli android anti-gaspi gratuite", "vas-y, 2 idées",
                     "regroupe les doublons", "fais-les noter", "je garde la 1", "montre la sortie 1",
                     "il me reste combien ?"):
            self.assertTrue(app.handle(line), line)
        self.assertIsNotNone(app.fiche)
        self.assertIsNotNone(app.last_review)
        self.assertTrue(app._validated("rev-x", 1))
        self.assertNotIn("mal formée", self.text())
        self.no_command_shown()


class ConversationWiringTest(WiringFixture):
    def test_actif_par_defaut_desactivable(self):
        self.assertTrue(Settings().mode_conversation)
        with mock.patch.dict(os.environ, {"MODE_CONVERSATION": "0"}):
            self.assertFalse(config.load_settings().mode_conversation)
        app = make_app(self.components(replace(self.settings, mode_conversation=True)),
                       input_fn=self.input_fn, output_fn=self.outputs.append)
        self.assertIsNotNone(app._history)
        app = make_app(self.components(), input_fn=self.input_fn, output_fn=self.outputs.append)
        self.assertIsNone(app._history)  # MODE_CONVERSATION=0 : langage libre d'avant

    def test_claude_absent_aucune_question_d_accord_qwen_repond(self):
        # Défaut V3 : routage -> claude_haiku ; Claude Code introuvable : rien à autoriser.
        self.llm.answers.append(say(reponse="Bonjour, je suis là."))
        app = make_app(self.components(replace(self.settings, mode_conversation=True), load_profile("brainstorm")),
                       input_fn=self.input_fn, output_fn=self.outputs.append)
        app.handle("bonjour")
        self.assertIn("Bonjour, je suis là.", self.outputs)
        self.assertFalse(any("Pour bien te comprendre" in o for o in self.outputs))
        calls = [(e["role"], e["agent"], e["result"]) for e in self.logger.events if e["event"] == "agent_call"]
        self.assertIn(("routage", "claude_haiku", "indisponible"), calls)
        self.assertIn(("routage", "qwen", "ok"), calls)

    def test_env_example_documente(self):
        from tests.test_langage_libre import ROOT
        self.assertIn("# MODE_CONVERSATION=1", (ROOT / ".env.example").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

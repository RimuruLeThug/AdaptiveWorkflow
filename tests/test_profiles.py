"""Profils : le squelette tient sans modification ; la politique de données est appliquée par le code."""

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import Orchestrator
from privacy.masker import Masker
from privacy.vault import Vault
from profiles.loader import ProfileError, list_profiles, load_profile
from tests.test_orchestrator import ScriptedOrganizer, plan
from tests.test_promise import FakeLLM
from tests.test_qwen_ui import Harness
from verify.basic import basic_verifier
from workers.base import Reply, Worker
from workers.defaults import DEFAULT_WORKERS


def write_profile(base: Path, name: str, doc: dict, data=None) -> None:
    (base / name).mkdir(parents=True)
    (base / name / "profil.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    if data is not None:
        (base / name / "donnees.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


MINIMAL = {"nom": "traduction", "description": "Traductions", "donnees_personnelles": False,
           "workers": [{"nom": "traducteur", "description": "Traduit", "consigne_systeme": "Tu traduis."}]}


class LoaderTest(unittest.TestCase):
    def test_profils_livres(self):
        self.assertEqual(list_profiles(), ["auto_projet", "brainstorm", "bureautique_fictive"])
        auto = load_profile("auto_projet")
        self.assertFalse(auto.personal_data)
        self.assertEqual([w.name for w in auto.workers], ["idees_projet", "relecteur_projet"])
        self.assertEqual(load_profile("brainstorm").workers, tuple(DEFAULT_WORKERS))
        office = load_profile("bureautique_fictive")
        self.assertTrue(office.personal_data)
        self.assertEqual(len(office.records), 3)
        for r in office.records:  # minimisation au chargement
            self.assertNotIn("iban", r)
            self.assertNotIn("note_interne", r)

    def test_donnees_d_exemple_fictives(self):
        office = load_profile("bureautique_fictive")
        self.assertTrue(all(r["email"].endswith("@exemple.fr") for r in office.records))

    def test_profils_invalides(self):
        cases = {
            "schema_inconnu": dict(MINIMAL, workers=[dict(MINIMAL["workers"][0], schema_sortie="inconnu")]),
            "doublon": dict(MINIMAL, workers=MINIMAL["workers"] * 2),
            "perso_sans_champs": dict(MINIMAL, donnees_personnelles=True),
            "champ_en_trop": dict(MINIMAL, outils=["shell"]),
            "nom_worker_invalide": dict(MINIMAL, workers=[dict(MINIMAL["workers"][0], nom="Trad-1")]),
            "shortlist": dict(MINIMAL, revue={"shortlist_min": 9, "shortlist_max": 3}),
        }
        with tempfile.TemporaryDirectory() as d:
            for name, doc in cases.items():
                write_profile(Path(d), name, doc)
                with self.subTest(name=name), self.assertRaises(ProfileError):
                    load_profile(name, Path(d))
            with self.assertRaises(ProfileError):
                load_profile("absent", Path(d))
            (Path(d) / "casse").mkdir()
            (Path(d) / "casse" / "profil.json").write_text("{pas du json", encoding="utf-8")
            with self.assertRaises(ProfileError):
                load_profile("casse", Path(d))

    def test_nouveau_profil_sans_modifier_le_squelette(self):
        """Preuve (brief, section 12) : un cas d'usage nouveau tourne avec le bus tel quel."""
        with tempfile.TemporaryDirectory() as d:
            write_profile(Path(d), "traduction", MINIMAL)
            profile = load_profile("traduction", Path(d))

        class Echo(Worker):
            def run(self, instruction, context):
                return Reply(f"[{self.spec.name}] {instruction}")

        org = ScriptedOrganizer([plan(("traducteur", "Traduis « bonjour »"))])
        report = Orchestrator(org, [Echo(s) for s in profile.workers], basic_verifier).run("Traduire")
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.accepted[0].text, "[traducteur] Traduis « bonjour »")


class ProfileCommandsTest(Harness):
    def office_app(self, llm_answers=()):
        a = self.app(list(llm_answers))
        a._profile_loader = load_profile
        a._profile_names = list_profiles
        a._masker_factory = lambda: Masker(Vault())
        a.handle("/profil bureautique_fictive")
        return a

    def test_changement_de_profil(self):
        a = self.app([], plans=[plan(("generateur", "a"))], worker_outputs=["ok"])
        a._profile_loader = load_profile
        a._profile_names = list_profiles
        a.handle("/profil")
        self.assertIn("brainstorm, bureautique_fictive", self.text())
        a.handle("/profil inexistant")
        self.assertIn("introuvable", self.text())
        a.handle("/profil brainstorm")
        self.assertEqual(a.profile.name, "brainstorm")
        a.handle("/tache x")
        self.assertEqual(self.factory_calls, ["gemini"])

    def test_workers_du_profil_transmis(self):
        a = self.app([])
        a._profile_loader = load_profile
        received = []

        def factory(name, profile=None):
            received.append(profile.name if profile else None)
            return Orchestrator(ScriptedOrganizer([plan(("generateur", "a"))]),
                                [type("W", (Worker,), {"run": lambda s, i, c: Reply("ok")})(w) for w in profile.workers],
                                basic_verifier)
        a._factory = factory
        a.handle("/profil brainstorm")
        a.handle("/tache x")
        self.assertEqual(received, ["brainstorm"])

    def test_donnees_personnelles_aucun_appel_externe(self):
        a = self.office_app()
        self.assertIn("aucun fournisseur externe", self.text())
        for cmd in ("/tache rédige une relance", "/revue"):
            self.out.clear()
            a.last_report = object() if cmd == "/revue" else None
            a._review_factory = lambda profile=None: self.fail("aucune revue ne doit être créée")
            a.handle(cmd)
            self.assertIn("Refusé par la politique de données", self.text(), cmd)
        self.assertEqual(self.factory_calls, [])

    def test_escalade_refusee_pour_donnees_personnelles(self):
        from review import ReviewReport
        a = self.office_app()
        a.last_review = ReviewReport("r", [], [], [], 6.5, 2)
        a._escalator_factory = lambda provider: self.fail("aucune escalade")
        a.handle("/escalader 1")
        self.assertIn("Refusé par la politique de données", self.text())

    def test_masquer_avec_valeurs_declarees(self):
        a = self.office_app()
        self.out.clear()
        a.handle("/masquer relancer karim n'diaye au sujet de la facture F-2026-131")
        t = self.text()
        self.assertNotIn("karim n'diaye", t.lower().split("---")[0])
        self.assertIn("Contrôle de fuite : OK", t)

    def test_donnees_masquees(self):
        a = self.office_app()
        self.out.clear()
        a.handle("/donnees")
        t = self.text()
        for secret in ("DUPONT", "jean.dupont", "06 12 34 56 78", "Martin-Leroy"):
            self.assertNotIn(secret, t)
        self.assertIn("1250", t)

    def test_routage_local_autorise(self):
        a = self.office_app([json.dumps({"action": "discussion", "argument": "", "reponse": "Bonjour"})])
        a.handle("salut")
        self.assertIn("Bonjour", self.text())  # Qwen local reste disponible


if __name__ == "__main__":
    unittest.main()

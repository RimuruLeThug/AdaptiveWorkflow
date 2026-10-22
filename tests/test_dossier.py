"""Travail sur un dossier du PC (V3.2) : lecture seule, accord dédié, modifications validées.

Doubles uniquement : aucun modèle réel, aucun appel réseau, jamais .env. Chaque test qui
ouvre, analyse ou interroge un dossier vérifie qu'il est resté identique, octet pour octet.
"""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from agents import ROLES, AgentRegistry, AgentRouter, resolve_routes
from dossier import modifications as mods
from dossier.analyse import (ANALYSE_SCHEMA, PROPOSITION_SCHEMA, analyser, contexte_fichiers, fichiers_cles,
                             preparer, tout_tient, valider_chemins)
from dossier.inventaire import (DossierError, Limites, check_folder, chemin_sur, decoder, extraire_document,
                                fin_de_ligne, find_folders, inventorier, lire, resolve_folder)
from dossier.modifications import annuler, appliquer, construire, derniere_application, snapshot
from gemini_client import HttpResponse
from qwen_ui import conversation as conv
from tests.test_agents import fake_registry, router_for, settings
from tests.test_conversation import ConvFixture, say
from tests.test_gemini_client import ok_body
from tests.test_promise import FakeLLM
from verify.schema import validate

SECRET = "sk-SECRET-0123456789"  # valeur fictive d'un .env : ne doit JAMAIS être lue ni envoyée
EMAIL = "lea.dupont@exemple.fr"  # adresse fictive


def make_folder(root: Path) -> Path:
    """Dossier de test : projet Python avec un peu de tout (sensible, binaire, ignoré, CRLF, cp1252)."""
    p = root / "MonProjet"
    (p / "src").mkdir(parents=True)
    (p / "README.md").write_bytes("# Mon projet\n\nUne appli de recettes.\nAuteur : contact\n".encode("utf-8"))
    (p / "src" / "main.py").write_bytes(b"def main():\r\n    print('bonjour')\r\n\r\nmain()\r\n")
    (p / "notes.txt").write_bytes("Idée : réduire le gaspillage.\n".encode("cp1252"))
    (p / "requirements.txt").write_bytes(b"tzdata\n")
    (p / ".env").write_bytes(f"API_KEY={SECRET}\n".encode())
    (p / ".git").mkdir()
    (p / ".git" / "config").write_bytes(b"[core]\n")
    (p / "node_modules" / "lib").mkdir(parents=True)
    (p / "node_modules" / "lib" / "x.js").write_bytes(b"var x = 1;\n")
    (p / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binaire")
    (p / ".gitignore").write_bytes(b"*.log\nbuild/\n")
    (p / "debug.log").write_bytes(b"journal\n")
    (p / "build").mkdir()
    (p / "build" / "out.txt").write_bytes(b"genere\n")
    return p


def make_docx(path: Path, paragraphs) -> None:
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document><w:body>{body}</w:body></w:document>')


def analyse_json(nature="projet Python", elements=(("README.md", "présentation"),), pistes=("Ajouter des tests",)):
    return json.dumps({"nature": nature, "resume": "Une petite appli de recettes en Python.",
                       "elements": [{"chemin": c, "role": r} for c, r in elements],
                       "points_attention": ["Pas de tests"], "pistes": list(pistes)}, ensure_ascii=False)


def proposal_json(*mods_, resume="Je corrige le titre.", question=""):
    doc = {"resume": resume, "modifications": [dict(m) for m in mods_]}
    if question:
        doc["question"] = question
    return json.dumps(doc, ensure_ascii=False)


def remplacer(chemin, ancien, nouveau, explication="titre plus clair"):
    return {"action": "remplacer", "chemin": chemin, "ancien": ancien, "nouveau": nouveau, "explication": explication}


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.folder = make_folder(self.tmp)
        self.before = snapshot(self.folder)

    def tearDown(self):
        self._tmp.cleanup()

    def assertUnchanged(self):
        self.assertEqual(snapshot(self.folder), self.before, "le dossier a été modifié")


# ------------------------------------------------------------------- inventaire


class InventaireTest(Tmp):
    def test_natures_et_exclusions(self):
        inv = inventorier(self.folder)
        natures = {e.chemin: e.nature for e in inv.entrees}
        self.assertEqual(natures["README.md"], "texte")
        self.assertEqual(natures["src/main.py"], "texte")
        self.assertEqual(natures["notes.txt"], "texte")
        self.assertEqual(natures[".env"], "sensible")
        self.assertEqual(natures["logo.png"], "binaire")
        for absent in ("debug.log", "build/out.txt", ".git/config", "node_modules/lib/x.js"):
            self.assertNotIn(absent, natures)
        self.assertIn(".git/", inv.ignores)
        self.assertIn("node_modules/", inv.ignores)
        self.assertIn("build/", inv.ignores)
        self.assertIn("projet Python", inv.natures_projet())
        self.assertIn("Sensibles, JAMAIS lus", inv.resume_code())
        self.assertUnchanged()

    def test_fichier_sensible_jamais_lu(self):
        inv = inventorier(self.folder)
        with self.assertRaises(DossierError):
            lire(inv, ".env")
        real_open = open
        opened = []

        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", spy):
            inv = inventorier(self.folder)
            contexte_fichiers(inv, [e.chemin for e in inv.entrees], 100_000)
        self.assertFalse([p for p in opened if p.endswith(".env")], "le .env a été ouvert")
        self.assertUnchanged()

    def test_lien_symbolique_non_suivi(self):
        outside = self.tmp / "dehors"
        outside.mkdir()
        (outside / "secret.txt").write_text("hors du dossier")
        try:
            os.symlink(outside, self.folder / "lien", target_is_directory=True)
            os.symlink(outside / "secret.txt", self.folder / "lien.txt")
        except (OSError, NotImplementedError):
            self.skipTest("liens symboliques indisponibles")
        inv = inventorier(self.folder)
        self.assertIsNone(inv.get("lien.txt"))
        self.assertFalse(any(e.chemin.startswith("lien/") for e in inv.entrees))
        self.assertEqual(inv.liens, 2)
        with self.assertRaises(DossierError):
            chemin_sur(self.folder, "lien/secret.txt")

    def test_inventaire_partiel(self):
        inv = inventorier(self.folder, Limites(max_fichiers=3))
        self.assertTrue(inv.partiel)
        self.assertEqual(len(inv.entrees), 3)
        self.assertIn("PARTIEL", inv.resume_code())

    def test_dossiers_du_programme_exclus(self):
        state = self.folder / "etat_programme"
        state.mkdir()
        (state / "fiche.json").write_text("{}")
        inv = inventorier(self.folder, exclus=[state])
        self.assertIsNone(inv.get("etat_programme/fiche.json"))
        self.assertIn("etat_programme/", inv.ignores)

    def test_encodage_et_fins_de_ligne(self):
        inv = inventorier(self.folder)
        main = lire(inv, "src/main.py")
        self.assertEqual(main.fin_ligne, "\r\n")
        self.assertNotIn("\r", main.texte)
        notes = lire(inv, "notes.txt")
        self.assertEqual(notes.encodage, "cp1252")
        self.assertIn("Idée", notes.texte)
        self.assertEqual(decoder(b"\xef\xbb\xbfabc"), ("abc", "utf-8-sig"))
        self.assertEqual(fin_de_ligne(b"pas de fin", "\r\n"), "\r\n")
        self.assertUnchanged()

    def test_document_word_lu_en_texte(self):
        make_docx(self.folder / "compte_rendu.docx", ["Réunion du lundi", "Budget validé &amp; signé"])
        inv = inventorier(self.folder)
        self.assertEqual(inv.get("compte_rendu.docx").nature, "document")
        text = extraire_document(inv, "compte_rendu.docx")
        self.assertIn("Réunion du lundi", text)
        self.assertIn("Budget validé & signé", text)

    def test_chemins_refuses(self):
        for bad in ("../dehors.txt", "src/../README.md", "/etc/passwd", "C:\\Windows\\x.txt", "src/../../x", ".env", "sous/.env.local",
                    "con.txt", ".git/config", "node_modules/a.js", "a//b", "", "fichier.txt.", "a:b.txt"):
            with self.subTest(chemin=bad):
                with self.assertRaises(DossierError):
                    chemin_sur(self.folder, bad)
        self.assertEqual(chemin_sur(self.folder, "src\\main.py"), "src/main.py")
        self.assertEqual(chemin_sur(self.folder, "./README.md"), "README.md")
        self.assertEqual(chemin_sur(self.folder, ".env.example"), ".env.example")


class ChoixDossierTest(Tmp):
    def test_alias_bureau_et_recherche_par_nom(self):
        home = self.tmp / "home"
        projet = home / "Desktop" / "Appli Recettes"
        projet.mkdir(parents=True)
        self.assertEqual(resolve_folder("Bureau/Appli Recettes", home=home), projet)
        self.assertEqual(resolve_folder("« Appli Recettes »", home=home), projet)
        self.assertEqual(resolve_folder(str(projet), home=home), projet)
        self.assertEqual(find_folders("appli recettes", home=home), [projet])
        with self.assertRaises(DossierError):
            resolve_folder("Introuvable", home=home)

    def test_dossiers_trop_larges_refuses(self):
        home = self.tmp / "home"
        home.mkdir()
        for bad in (home, Path("/"), self.folder / "README.md", self.tmp / "absent"):
            with self.subTest(path=bad):
                with self.assertRaises(DossierError):
                    check_folder(bad, home=home)
        self.assertEqual(check_folder(self.folder, home=home), self.folder.resolve())


# ---------------------------------------------------------------------- analyse


class AnalyseTest(Tmp):
    def test_analyse_lit_les_fichiers_cles_et_valide_les_chemins(self):
        inv = inventorier(self.folder)
        llm = FakeLLM([analyse_json(elements=(("README.md", "présentation"), ("invente.py", "?"),
                                              (".env", "clés"), ("src/main.py", "point d'entrée")))])
        a = analyser(llm, inv, 10_000)
        prompt = llm.calls[0]["messages"][-1].content
        system = llm.calls[0]["messages"][0].content
        self.assertIn("Une appli de recettes", prompt)
        self.assertIn("def main()", prompt)
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn("var x = 1", prompt)  # node_modules ignoré
        self.assertIn("DONNÉES", system)
        self.assertEqual([c for c, _ in a.elements], ["README.md", "src/main.py"])
        self.assertIn("Fichiers importants", a.render())
        self.assertUnchanged()

    def test_extrait_si_le_budget_ne_suffit_pas(self):
        (self.folder / "long.md").write_text("x" * 5000)
        inv = inventorier(self.folder)
        ctx = contexte_fichiers(inv, ["long.md", "README.md"], 2000, tag="T")
        self.assertIn("long.md", ctx.extraits)
        self.assertNotIn("long.md", ctx.complets)
        self.assertIn("EXTRAIT", ctx.texte)
        self.assertIn("long.md", ctx.lectures)  # lu en entier par le code
        self.assertFalse(tout_tient(inv, 2000))
        self.assertTrue(tout_tient(inventorier(self.folder, Limites()), 100_000))

    def test_choix_des_fichiers_pour_un_gros_dossier(self):
        for i in range(30):
            (self.folder / f"page{i}.md").write_text("contenu " * 200)
        inv = inventorier(self.folder)
        llm = FakeLLM([json.dumps({"fichiers": ["page3.md", "../x", ".env", "inexistant.md"], "raison": "r"})])
        ctx = preparer(llm, inv, "corrige la page 3", 4000)
        self.assertEqual(ctx.montres, ["page3.md"])
        self.assertEqual(valider_chemins(inv, ["README.md", "logo.png"]), ["README.md"])
        self.assertEqual(valider_chemins(inv, ["logo.png"], lisibles=False), ["logo.png"])

    def test_ordre_des_fichiers_cles(self):
        inv = inventorier(self.folder)
        self.assertEqual(fichiers_cles(inv)[:2], ["README.md", "requirements.txt"])

    def test_schemas_acceptent_une_reponse_type(self):
        validate(json.loads(analyse_json()), ANALYSE_SCHEMA)
        validate(json.loads(proposal_json(remplacer("README.md", "a", "b"))), PROPOSITION_SCHEMA)


# ------------------------------------------------------------- modifications


class ConstruireTest(Tmp):
    def build(self, *mods_, complets=("README.md", "src/main.py", "notes.txt"), interdits=()):
        self.inv = inventorier(self.folder)
        lectures = {c: lire(self.inv, c) for c in ("README.md", "src/main.py", "notes.txt")}
        return construire(json.loads(proposal_json(*mods_)), self.inv, consigne="test", lectures=lectures,
                          complets=complets, interdits=interdits)

    def test_remplacer_creer_et_fusion(self):
        prop = self.build(remplacer("README.md", "# Mon projet", "# Recettes malines"),
                          remplacer("README.md", "Auteur : contact", "Auteur : moi"),
                          {"action": "creer", "chemin": "docs/guide.md", "contenu": "# Guide\n", "explication": "doc"})
        self.assertEqual([c.chemin for c in prop.changements], ["README.md", "docs/guide.md"])
        readme = prop.changements[0]
        self.assertIn("# Recettes malines", readme.apres)
        self.assertIn("Auteur : moi", readme.apres)
        self.assertEqual(len(readme.explications), 2)
        self.assertTrue(prop.changements[1].creation)
        self.assertEqual(prop.refus, [])
        self.assertIn("+# Recettes malines", prop.diff())
        self.assertIn("nouveau fichier", prop.render_resume())
        self.assertUnchanged()  # construire n'écrit rien

    def test_refus_du_code(self):
        prop = self.build(
            remplacer("README.md", "absent du fichier", "x"),
            remplacer("src/main.py", "main()", "start()"),  # présent deux fois
            {"action": "reecrire", "chemin": "requirements.txt", "contenu": "x", "explication": "non lu en entier"},
            {"action": "creer", "chemin": "README.md", "contenu": "écrase", "explication": "existe"},
            {"action": "creer", "chemin": "../evasion.txt", "contenu": "x", "explication": "hors dossier"},
            {"action": "creer", "chemin": ".env", "contenu": "x", "explication": "sensible"},
            {"action": "remplacer", "chemin": "logo.png", "ancien": "PNG", "nouveau": "x", "explication": "binaire"},
            {"action": "creer", "chemin": "rapport.docx", "contenu": "x", "explication": "document"},
            {"action": "remplacer", "chemin": "inexistant.md", "ancien": "a", "nouveau": "b", "explication": "?"},
            complets=("README.md",))
        self.assertTrue(prop.vide)
        self.assertEqual(len(prop.refus), 9)
        joined = "\n".join(prop.refus)
        for needle in ("introuvable", "présent 2 fois", "pas vu ce fichier en entier", "existe déjà", "chemin refusé",
                       "sensible", "binaire ou document", "n'existe pas"):
            self.assertIn(needle, joined)

    def test_dossier_du_programme_jamais_modifie(self):
        state = self.folder / "state"
        state.mkdir()
        prop = self.build({"action": "creer", "chemin": "state/x.json", "contenu": "{}", "explication": "x"},
                          interdits=[state])
        self.assertTrue(prop.vide)
        self.assertIn("programme lui-même", prop.refus[0])

    def test_jeton_de_masquage_jamais_ecrit(self):
        prop = self.build(remplacer("README.md", "Auteur : contact", "Auteur : [EMAIL_3]"))
        self.assertTrue(prop.vide)
        self.assertIn("jeton", prop.refus[0])


class AppliquerTest(Tmp):
    def proposition(self, *mods_):
        inv = inventorier(self.folder)
        lectures = {c: lire(inv, c) for c in ("README.md", "src/main.py", "notes.txt")}
        return construire(json.loads(proposal_json(*mods_)), inv, consigne="test", lectures=lectures,
                          complets=lectures.keys(), defaut_fin="\r\n")

    def test_ecrit_en_gardant_encodage_et_fins_de_ligne_puis_annule(self):
        prop = self.proposition(remplacer("src/main.py", "print('bonjour')", "print('salut')"),
                                remplacer("notes.txt", "réduire", "éviter"),
                                {"action": "creer", "chemin": "docs/a/guide.md", "contenu": "l1\nl2\n",
                                 "explication": "doc"})
        saves = self.tmp / "sauvegardes"
        done = appliquer(prop, self.folder, saves)
        self.assertEqual(sorted(done.fichiers), ["docs/a/guide.md", "notes.txt", "src/main.py"])
        self.assertEqual((self.folder / "src" / "main.py").read_bytes(),
                         b"def main():\r\n    print('salut')\r\n\r\nmain()\r\n")
        self.assertEqual((self.folder / "notes.txt").read_bytes(), "Idée : éviter le gaspillage.\n".encode("cp1252"))
        self.assertEqual((self.folder / "docs" / "a" / "guide.md").read_bytes(), b"l1\r\nl2\r\n")
        self.assertEqual(list(self.folder.glob("**/*.tmp")) + list(self.folder.glob("**/.*.tmp")), [])
        self.assertEqual(derniere_application(saves), done.manifeste)
        restored, skipped = annuler(done.manifeste, self.folder)
        self.assertEqual(sorted(restored), ["docs/a/guide.md", "notes.txt", "src/main.py"])
        self.assertEqual(skipped, [])
        self.assertUnchanged()
        self.assertFalse((self.folder / "docs").exists())  # dossiers créés retirés
        self.assertIsNone(derniere_application(saves))
        with self.assertRaises(DossierError):
            annuler(done.manifeste, self.folder)

    def test_rien_n_est_ecrit_si_un_fichier_a_change_depuis_la_lecture(self):
        prop = self.proposition(remplacer("README.md", "# Mon projet", "# Nouveau"),
                                remplacer("notes.txt", "réduire", "éviter"))
        (self.folder / "notes.txt").write_bytes(b"modifie ailleurs\n")
        self.before = snapshot(self.folder)
        with self.assertRaises(DossierError) as cm:
            appliquer(prop, self.folder, self.tmp / "sauvegardes")
        self.assertIn("a changé depuis", str(cm.exception))
        self.assertUnchanged()

    def test_echec_en_cours_de_route_remet_tout_en_etat(self):
        prop = self.proposition(remplacer("README.md", "# Mon projet", "# Nouveau"),
                                remplacer("notes.txt", "réduire", "éviter"))
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise PermissionError("fichier ouvert dans un autre programme")
            return real(src, dst)

        with mock.patch("dossier.modifications.os.replace", flaky):
            with self.assertRaises(DossierError) as cm:
                appliquer(prop, self.folder, self.tmp / "sauvegardes")
        self.assertIn("état d'origine", str(cm.exception))
        self.assertUnchanged()
        self.assertIsNone(derniere_application(self.tmp / "sauvegardes"))

    def test_annulation_ne_touche_pas_un_fichier_modifie_depuis(self):
        prop = self.proposition(remplacer("README.md", "# Mon projet", "# Nouveau"),
                                remplacer("notes.txt", "réduire", "éviter"))
        done = appliquer(prop, self.folder, self.tmp / "sauvegardes")
        (self.folder / "README.md").write_text("retouché à la main\n")
        restored, skipped = annuler(done.manifeste, self.folder)
        self.assertEqual((restored, skipped), (["notes.txt"], ["README.md"]))
        self.assertEqual((self.folder / "README.md").read_text(), "retouché à la main\n")

    def test_creation_refusee_si_le_fichier_apparait_entre_temps(self):
        prop = self.proposition({"action": "creer", "chemin": "nouveau.md", "contenu": "x", "explication": "e"})
        (self.folder / "nouveau.md").write_text("créé ailleurs")
        with self.assertRaises(DossierError):
            appliquer(prop, self.folder, self.tmp / "sauvegardes")
        self.assertEqual((self.folder / "nouveau.md").read_text(), "créé ailleurs")


# ------------------------------------------------------------------- rôle et accord


class RoleTest(unittest.TestCase):
    def test_role_dossier_accord_dedie(self):
        info = ROLES["dossier"]
        self.assertTrue(info.free_text and info.accord_session and info.accord_dedie)
        reg, fakes = fake_registry(claude_haiku=["ok", "ok"])
        router = router_for(reg, settings(v3=True), confirm=lambda p: False)
        roles = router.grant_session_consent_all("claude")
        self.assertNotIn("dossier", roles)
        self.assertFalse(router.has_session_consent("dossier", "claude"))
        router.grant_session_consent("dossier", "claude")
        self.assertTrue(router.has_session_consent("dossier", "claude"))
        router.revoke_session_consent("dossier")
        self.assertFalse(router.has_session_consent("dossier", "claude"))
        self.assertTrue(router.has_session_consent("routage", "claude"))  # les autres accords restent


class ValidateTest(unittest.TestCase):
    def test_actions_dossier_controlees_par_le_code(self):
        no = conv.validate({"action": "dossier_travail", "argument": "corrige", "reponse": ""})
        self.assertEqual((no.action, no.reply), ("discussion", conv.NO_FOLDER))
        ok = conv.validate({"action": "dossier_travail", "argument": "corrige  le   README", "reponse": ""},
                           folder_open=True)
        self.assertEqual((ok.action, ok.argument), ("dossier_travail", "corrige le README"))
        self.assertEqual(conv.validate({"action": "dossier_appliquer", "argument": "", "reponse": ""},
                                       folder_open=True).action, "discussion")
        self.assertEqual(conv.validate({"action": "dossier_appliquer", "argument": "", "reponse": ""},
                                       folder_open=True, has_proposal=True).action, "dossier_appliquer")
        self.assertEqual(conv.validate({"action": "dossier_annuler", "argument": "", "reponse": ""},
                                       folder_open=True).action, "discussion")
        path = conv.validate({"action": "dossier", "argument": " C:\\Mes  Projets\\Appli ", "reponse": ""})
        self.assertEqual((path.action, path.argument), ("dossier", "C:\\Mes  Projets\\Appli"))
        self.assertIn("dossier_appliquer", conv.CONV_ACTIONS)
        self.assertNotIn("dossier_appliquer", conv.CONFIRM_BEFORE)  # confirmé par le code au moment d'écrire
        self.assertIn("dossier_appliquer", conv.WRITE_ACTIONS)


# ------------------------------------------------------------------ application


class AppDossierFixture(ConvFixture):
    def setUp(self):
        super().setUp()
        self.folder = make_folder(self.tmp / "pc")
        self.before = snapshot(self.folder)

    def assertUnchanged(self):
        self.assertEqual(snapshot(self.folder), self.before, "le dossier a été modifié")

    def open_app(self, answers=(), roles=(("routage", "qwen"), ("dossier", "qwen")), **kw):
        app = self.make(list(answers), roles=roles, **kw)
        app._home = self.tmp / "pc"
        app._role_llms["dossier"] = self.router.role_llm("dossier")  # comme bootstrap
        return app

    def text(self):
        return "\n".join(self.outputs)


class CommandesTest(AppDossierFixture):
    def test_ouvrir_analyser_sans_rien_modifier(self):
        app = self.open_app([analyse_json()])
        app.handle(f"/dossier {self.folder}")
        out = self.text()
        self.assertIn("lecture seule", out)
        self.assertIn("Sensibles, JAMAIS lus", out)
        self.assertIn("Une petite appli de recettes", out)
        self.assertIn("Je n'ai rien modifié", out)
        self.assertNotIn("D'accord pour l'envoyer", out)  # agent local : rien à autoriser
        self.assertNotIn(SECRET, "\n".join(self.prompts()))
        self.assertIsNotNone(app.dossier)
        self.assertUnchanged()

    def test_question_puis_travail_refuse_puis_applique_puis_annule(self):
        app = self.open_app([analyse_json(),
                             json.dumps({"reponse": "main.py affiche bonjour.", "fichiers": ["src/main.py"]}),
                             proposal_json(remplacer("README.md", "# Mon projet", "# Recettes malines"))])
        app.handle(f"/dossier {self.folder}")
        app.handle("/question que fait main.py ?")
        self.assertIn("main.py affiche bonjour", self.text())
        self.inputs = ["n"]
        app.handle("/travail renomme le titre du README")
        self.assertIn("+# Recettes malines", self.text())
        self.assertIn("rien n'est encore écrit", self.text())
        self.assertIn("Rien n'a été écrit", self.text())
        self.assertUnchanged()
        self.assertIsNotNone(app.dossier.proposition)
        # Diff complet, puis application après « o ».
        app.handle("/diff")
        self.inputs = ["o"]
        app.handle("/appliquer")
        self.assertTrue((self.folder / "README.md").read_text(encoding="utf-8").startswith("# Recettes malines"))
        self.assertIsNone(app.dossier.proposition)
        saved = list((self.tmp / "app" / "dossiers").glob("*/propositions/*/modifications.diff"))
        self.assertEqual(len(saved), 1)  # diff gardé dans state/, jamais dans le dossier
        # Annulation refusée : rien n'est touché ; puis annulation après confirmation.
        written = snapshot(self.folder)
        self.inputs = ["n"]
        app.handle("/annuler")
        self.assertEqual(snapshot(self.folder), written)
        self.inputs = ["o"]
        app.handle("/annuler")
        self.assertIn("C'est annulé", self.text())
        self.assertUnchanged()
        app.handle("/annuler")
        self.assertIn("Rien à annuler", self.text())

    def test_appliquer_sans_oui_n_ecrit_rien(self):
        app = self.open_app([analyse_json(), proposal_json(remplacer("README.md", "# Mon projet", "# X"))])
        app.handle(f"/dossier {self.folder}")
        for answer in ("", "non", "peut-être", "oui oui je crois pas"):
            self.inputs = ["n"] if not app.dossier.proposition else []
            if app.dossier.proposition is None:
                app.handle("/travail titre")
            self.inputs = [answer]
            app.handle("/appliquer")
            self.assertUnchanged()

    def test_dossier_choisi_par_nom_ou_par_saisie(self):
        app = self.open_app([analyse_json(), analyse_json()])
        app.handle("/dossier MonProjet")  # relatif au dossier personnel
        self.assertEqual(app.dossier.racine, self.folder.resolve())
        app.handle("/dossier fermer")
        self.assertIsNone(app.dossier)
        self.inputs = [str(self.folder)]
        app.handle("/dossier choisir")  # pas de fenêtre : le chemin est demandé
        self.assertIsNotNone(app.dossier)
        self.assertUnchanged()

    def test_fenetre_de_choix(self):
        app = self.open_app([analyse_json()])
        app._folder_picker = lambda: str(self.folder)
        app.handle("/dossier choisir")
        self.assertEqual(app.dossier.racine, self.folder.resolve())

    def test_dossier_du_programme_protege(self):
        app = self.open_app([analyse_json(), proposal_json(
            {"action": "creer", "chemin": "app/x.json", "contenu": "{}", "explication": "e"})])
        # L'utilisateur ouvre le dossier qui contient l'état du programme (le projet lui-même).
        (self.tmp / "app").mkdir(exist_ok=True)
        (self.tmp / "app" / "fiche_projet.json").write_text("{}")
        app.handle(f"/dossier {self.tmp}")
        self.assertIsNone(app.dossier.inv.get("app/fiche_projet.json"))
        self.assertIn("app/", app.dossier.inv.ignores)
        app.handle("/travail range l'état")
        self.assertIn("programme lui-même", self.text())
        self.assertIsNone(app.dossier.proposition)


class AccordDedieTest(AppDossierFixture):
    ROLES = (("routage", "qwen"), ("dossier", "gemini_test,qwen"))

    def test_refus_tout_reste_local_jusqu_a_la_fermeture(self):
        app = self.open_app([analyse_json()], roles=self.ROLES)
        self.inputs = ["n"]
        app.handle(f"/dossier {self.folder}")
        self.assertIn("D'accord pour l'envoyer", self.text())
        self.assertEqual(self.transport.requests, [])  # rien n'est parti
        self.assertTrue(app.dossier_local)
        self.assertTrue(app._personal())
        self.assertIn("Une petite appli de recettes", self.text())  # analyse par le Qwen local
        app.handle("/dossier fermer")
        self.assertFalse(app.dossier_local)
        self.assertFalse(app._personal())

    def test_accord_masque_et_jamais_le_secret(self):
        (self.folder / "README.md").write_text(f"# Mon projet\nContact : {EMAIL}\n", encoding="utf-8")
        app = self.open_app([], roles=self.ROLES)
        self.answer(analyse_json())
        self.inputs = ["o"]
        app.handle(f"/dossier {self.folder}")
        question = self.text()
        self.assertIn("données personnelles possibles", question)
        self.assertIn("EMAIL", question)
        sent = "\n".join(self.sent_texts())
        self.assertNotIn(EMAIL, sent)
        self.assertIn("[EMAIL_1]", sent)
        self.assertNotIn(SECRET, sent)
        self.assertNotIn(str(self.tmp), sent)  # jamais le chemin complet (nom d'utilisateur)
        self.assertTrue(self.router.has_session_consent("dossier", "gemini"))
        # Une question ensuite : pas de nouvelle demande d'accord pour ce dossier.
        self.answer(json.dumps({"reponse": "ok", "fichiers": []}))
        app.handle("/question qui est le contact ?")
        self.assertEqual(self.text().count("D'accord pour l'envoyer"), 1)
        app.handle("/dossier fermer")
        self.assertFalse(self.router.has_session_consent("dossier", "gemini"))

    def test_accord_de_conversation_ne_couvre_pas_le_dossier(self):
        app = self.open_app([analyse_json()], roles=self.ROLES)
        self.router.grant_session_consent_all("gemini")
        self.inputs = ["n"]
        app.handle(f"/dossier {self.folder}")
        self.assertIn("D'accord pour l'envoyer", self.text())
        self.assertEqual(self.transport.requests, [])

    def test_profil_donnees_personnelles_aucune_question(self):
        app = self.open_app([analyse_json()], roles=self.ROLES, profile="bureautique_fictive")
        app.handle(f"/dossier {self.folder}")
        self.assertNotIn("D'accord pour l'envoyer", self.text())
        self.assertEqual(self.transport.requests, [])
        self.assertIsNotNone(app.dossier.analyse)


class ConversationDossierTest(AppDossierFixture):
    def test_parcours_complet_sans_commande(self):
        app = self.open_app([
            say("dossier", "MonProjet"), analyse_json(),
            say("dossier_question", "à quoi sert main.py ?"), json.dumps({"reponse": "Il affiche bonjour."}),
            say("dossier_travail", "change le titre du README en « Recettes malines »"),
            proposal_json(remplacer("README.md", "# Mon projet", "# Recettes malines")),
            say("dossier_annuler"),
            say("dossier_fermer"),
        ])
        app.handle("regarde mon dossier MonProjet")
        self.assertIn("Une petite appli de recettes", self.text())
        app.handle("à quoi sert main.py ?")
        self.assertIn("Il affiche bonjour", self.text())
        self.inputs = ["vas-y", "oui"]  # « On y va ? » puis « J'écris ces modifications ? »
        app.handle("change le titre du README")
        self.assertTrue((self.folder / "README.md").read_text(encoding="utf-8").startswith("# Recettes malines"))
        self.assertIn("annule", self.text())
        self.inputs = ["oui"]
        app.handle("finalement annule")
        self.assertUnchanged()
        app.handle("on arrête avec ce dossier")
        self.assertIsNone(app.dossier)
        self.no_command_shown()
        self.assertNotIn("/appliquer", self.text())
        self.assertNotIn("/annuler", self.text())

    def test_correction_a_la_question_d_ecriture(self):
        app = self.open_app([
            say("dossier", str(self.folder)), analyse_json(),
            say("dossier_travail", "titre en anglais"),
            proposal_json(remplacer("README.md", "# Mon projet", "# Mon projet FR")),
            say("dossier_travail", "titre en anglais, pas en français"),
            proposal_json(remplacer("README.md", "# Mon projet", "# My project")),
        ])
        app.handle(f"ouvre {self.folder}")
        self.inputs = ["oui", "non, en anglais stp", "oui", "oui"]
        app.handle("mets le titre en anglais")
        self.assertTrue((self.folder / "README.md").read_text(encoding="utf-8").startswith("# My project"))
        self.assertNotIn("Mon projet FR", (self.folder / "README.md").read_text(encoding="utf-8"))

    def test_le_modele_ne_peut_pas_ecrire_seul(self):
        app = self.open_app([
            say("dossier", str(self.folder)), analyse_json(),
            say("dossier_travail", "titre"), proposal_json(remplacer("README.md", "# Mon projet", "# X")),
            say("dossier_appliquer"),
        ])
        app.handle("ouvre le dossier")
        self.inputs = ["oui", "non"]
        app.handle("change le titre")
        self.assertUnchanged()
        self.inputs = ["non"]  # le modèle propose d'appliquer : le code redemande, l'utilisateur refuse
        app.handle("ok")
        self.assertUnchanged()
        self.assertIsNotNone(app.dossier.proposition)

    def test_contenu_du_dossier_jamais_dans_la_conversation(self):
        app = self.open_app([
            say("dossier", str(self.folder)), analyse_json(),
            say("dossier_question", "que dit le README ?"),
            json.dumps({"reponse": "Il présente une appli de recettes.", "fichiers": ["README.md"]}),
            say("discussion", reponse="Avec plaisir !"),
        ])
        app.handle("ouvre le dossier")
        app.handle("que dit le README ?")
        app.handle("merci")
        conversation_prompts = [c["messages"][-1].content for c in self.llm.calls
                                if c["messages"][0].content.startswith("Tu es l'assistant de CheapWorkflow")]
        self.assertEqual(len(conversation_prompts), 3)
        last = conversation_prompts[-1]
        self.assertIn("MonProjet", last)  # le nom du dossier et ses fichiers importants : oui
        self.assertIn("README.md", last)
        for content in ("Une appli de recettes", "Une petite appli de recettes", "présente une appli"):
            self.assertNotIn(content, last)  # le contenu des fichiers et des réponses : jamais
        # Rôle « dossier » local : la conversation reste locale tant que le dossier est ouvert.
        self.assertTrue(app.dossier_local)
        self.assertGreater(len(app._history), 0)

    def test_fermer_un_dossier_reste_local_vide_l_historique(self):
        app = self.open_app([say("dossier", str(self.folder)), analyse_json(), say("dossier_fermer")])
        app.handle("ouvre le dossier")
        self.assertTrue(app.dossier_local)
        app.handle("ferme-le")
        self.assertFalse(app.dossier_local)
        self.assertEqual(len(app._history), 0)

    def test_action_dossier_sans_dossier_ouvert(self):
        app = self.open_app([say("dossier_travail", "corrige le README")])
        app.handle("corrige le README")
        self.assertIn("Aucun dossier n'est ouvert", self.text())

    def test_garde_tout_en_local_couvre_le_dossier(self):
        app = self.open_app([say("dossier", str(self.folder)), analyse_json(), say("confidentialite", "local")])
        app.handle("ouvre le dossier")
        app.handle("garde tout en local")
        self.assertTrue(app.dossier_local)
        self.assertIn("reste sur ton PC", app._privacy_line())


if __name__ == "__main__":
    unittest.main()

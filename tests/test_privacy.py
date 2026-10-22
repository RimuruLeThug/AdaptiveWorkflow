"""Masquage et contrôle de fuite — hors ligne, données fictives uniquement."""

import json
import tempfile
import unittest
from pathlib import Path

from local_llm import LocalLLMError
from privacy.detectors import detect, iban_valid, luhn_valid
from privacy.masker import LeakBlocked, Masker, PreviewRefused, declared_values, minimize, render_preview
from privacy.policy import PolicyError, check_provider
from privacy.vault import Vault, VaultError
from tests.test_promise import FakeLLM


class FakeCipher:
    """Chiffrement de test réversible (le vrai : DPAPI sous Windows, vérifié par « python -m privacy selftest »)."""

    name = "test"

    def encrypt(self, data: bytes) -> bytes:
        return b"ENC" + bytes(b ^ 0x5A for b in data)

    def decrypt(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC"):
            raise ValueError("pas chiffré")
        return bytes(b ^ 0x5A for b in data[3:])


DOSSIER = ("Client : M. Jean DUPONT, né le 3 mars 1980. Contact : jean.dupont@exemple.fr, 06 12 34 56 78. "
           "IBAN FR76 3000 6000 0112 3456 7890 189. NIR 1 80 03 75 123 456 78. SIRET 732 829 320 00074. "
           "Adresse : 12 bis rue des Lilas, 75011 Paris. Montant du devis : 1250 euros, échéance le 3 mars 2026.")


def kinds(text, **kw):
    return [(f.kind, f.value) for f in detect(text, **kw)]


class DetectorsTest(unittest.TestCase):
    def test_dossier_complet(self):
        found = dict((v, k) for k, v in kinds(DOSSIER))
        expected = {
            "Jean DUPONT": "PERSONNE", "3 mars 1980": "NAISSANCE", "jean.dupont@exemple.fr": "EMAIL",
            "06 12 34 56 78": "TELEPHONE", "FR76 3000 6000 0112 3456 7890 189": "IBAN",
            "1 80 03 75 123 456 78": "NIR", "732 829 320 00074": "SIRET", "12 bis rue des Lilas": "ADRESSE",
            "75011": "CODE_POSTAL",
        }
        for value, kind in expected.items():
            with self.subTest(value=value):
                self.assertEqual(found.get(value), kind)
        self.assertNotIn("1250", found)  # montant gardé
        self.assertNotIn("3 mars 2026", found)  # une échéance n'est pas une date de naissance

    def test_variantes(self):
        cases = {
            "+33 6 12 34 56 78": "TELEPHONE", "0033612345678": "TELEPHONE", "01.23.45.67.89": "TELEPHONE",
            "FR7630006000011234567890189": "IBAN", "DE89 3704 0044 0532 0130 00": "IBAN",
            "2 75 12 2A 123 456": "NIR", "732829320": "SIREN", "Mme Élodie Martin-Leroy": "PERSONNE",
            "Dr Paul Henri": "PERSONNE", "Marie LEFEBVRE": "PERSONNE", "Hervé LE GALL": "PERSONNE",
            "Inès N'DIAYE": "PERSONNE", "Mme MARTIN-LEROY": "PERSONNE",
            "date de naissance : 03/03/1980": "NAISSANCE", "contact@pme-exemple.com": "EMAIL",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertIn(kind, [k for k, _ in kinds(text)])

    def test_iban_et_siret_invalides_masques_quand_meme(self):
        self.assertFalse(iban_valid("FR76 3000 6000 0112 3456 7890 180"))
        self.assertEqual(kinds("FR76 3000 6000 0112 3456 7890 180")[0][0], "IBAN")
        self.assertFalse(luhn_valid("73282932000075"))
        self.assertEqual(kinds("732 829 320 00075")[0][0], "SIRET")

    def test_jetons_jamais_redetectes(self):
        self.assertEqual(kinds("Voir [PERSONNE_1] et [CODE_POSTAL_12345]"), [])
        self.assertEqual(kinds("[CODE_POSTAL_1] [NAISSANCE_2]"), [])

    def test_client_nomme_personne(self):
        # « Personne » est un vrai nom de famille : il ne doit jamais corrompre un jeton [PERSONNE_n].
        m = Masker(Vault())
        res = m.mask("M. Jean DUPONT a vu Mme Personne.", declared=["Personne"])
        self.assertEqual(m.leak_check(res.text, declared=["Personne"]), [])
        self.assertEqual(m.unmask(res.text), ("M. Jean DUPONT a vu Mme Personne.", []))

    def test_nom_compose_entier(self):
        self.assertEqual(kinds("Hervé LE GALL signe"), [("PERSONNE", "Hervé LE GALL")])
        self.assertEqual(kinds("M. LE GALL signe"), [("PERSONNE", "LE GALL")])

    def test_adresse_bornee(self):
        found = kinds("12 rue des Lilas 75011 Paris. Rappeler M. Jean DUPONT demain.")
        self.assertIn(("ADRESSE", "12 rue des Lilas"), found)
        self.assertIn(("CODE_POSTAL", "75011"), found)
        self.assertIn(("PERSONNE", "Jean DUPONT"), found)

    def test_valeurs_declarees(self):
        found = kinds("pierre durand a rappelé ; Durand confirme", declared=["Durand", "pierre"])
        self.assertEqual([k for k, _ in found], ["DECLARE", "DECLARE", "DECLARE"])
        self.assertEqual(kinds("Durandal", declared=["Durand"]), [])  # mot entier seulement

    def test_detecteur_de_noms_externe(self):
        from privacy.detectors import Finding
        fake_spacy = lambda text: [Finding("PERSONNE", text.index("pierre"), text.index("pierre") + 6, "pierre")]
        self.assertIn(("PERSONNE", "pierre"), kinds("appeler pierre demain", name_detector=fake_spacy))

    def test_pas_de_chevauchement(self):
        found = detect(DOSSIER)
        for a, b in zip(found, found[1:]):
            self.assertLessEqual(a.end, b.start)


class VaultTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "coffre.bin"

    def tearDown(self):
        self._tmp.cleanup()

    def test_jetons_stables_et_persistants(self):
        v = Vault(self.path, FakeCipher())
        t1 = v.token_for("EMAIL", "Jean.Dupont@Exemple.fr")
        t2 = v.token_for("EMAIL", "jean.dupont@exemple.fr")
        t3 = v.token_for("TELEPHONE", "06 12 34 56 78")
        t4 = v.token_for("TELEPHONE", "0612345678")
        self.assertEqual((t1, t2, t3, t4), ("[EMAIL_1]", "[EMAIL_1]", "[TELEPHONE_1]", "[TELEPHONE_1]"))
        v.save()
        v2 = Vault(self.path, FakeCipher())
        self.assertEqual(v2.token_for("EMAIL", "jean.dupont@exemple.fr"), "[EMAIL_1]")  # même client, même jeton
        self.assertEqual(v2.token_for("EMAIL", "autre@exemple.fr"), "[EMAIL_2]")
        self.assertEqual(v2.value_for("[EMAIL_1]"), "Jean.Dupont@Exemple.fr")

    def test_fichier_chiffre(self):
        v = Vault(self.path, FakeCipher())
        v.token_for("PERSONNE", "Jean DUPONT")
        v.save()
        raw = self.path.read_bytes()
        self.assertNotIn(b"DUPONT", raw)
        self.assertTrue(raw.startswith(b"ENC"))

    def test_disque_sans_chiffrement_refuse(self):
        with self.assertRaises(VaultError):
            Vault(self.path, None)

    def test_coffre_illisible_bloque(self):
        self.path.write_bytes(b"pas chiffre")
        with self.assertRaises(VaultError):
            Vault(self.path, FakeCipher())

    def test_valeur_declaree_devient_personne(self):
        self.assertEqual(Vault().token_for("DECLARE", "Durand"), "[PERSONNE_1]")

    def test_repr_sans_valeurs(self):
        v = Vault()
        v.token_for("PERSONNE", "Jean DUPONT")
        self.assertNotIn("DUPONT", repr(v))


class MaskerTest(unittest.TestCase):
    def masker(self, llm=None):
        return Masker(Vault(), indirect_llm=llm)

    def test_masquage_et_fuite(self):
        m = self.masker()
        res = m.mask(DOSSIER)
        for secret in ("DUPONT", "jean.dupont", "06 12", "FR76", "1 80 03", "732 829", "rue des Lilas", "75011", "1980"):
            self.assertNotIn(secret, res.text)
        self.assertIn("1250 euros", res.text)
        self.assertEqual(m.leak_check(res.text), [])

    def test_meme_personne_meme_jeton(self):
        res = self.masker().mask("M. Jean DUPONT appelle. Rappeler M. Jean DUPONT.")
        self.assertEqual(res.text.count("[PERSONNE_1]"), 2)

    def test_reinjection_exacte(self):
        m = self.masker()
        res = m.mask(DOSSIER)
        restored, unknown = m.unmask(res.text)
        self.assertEqual(restored, DOSSIER)
        self.assertEqual(unknown, [])

    def test_reinjection_jetons_inconnus_ou_deformes(self):
        m = self.masker()
        m.mask("M. Jean DUPONT")
        restored, unknown = m.unmask("Bonjour [PERSONNE_1], voir [PERSONNE_9] et PERSONNE_1 sans crochets, "
                                     "CODE_POSTAL_3 aussi")
        self.assertIn("Jean DUPONT", restored)
        self.assertEqual(unknown, ["[PERSONNE_9]", "PERSONNE_1", "CODE_POSTAL_3"])

    def test_reinjection_de_tous_les_types(self):
        m = self.masker()
        res = m.mask(DOSSIER)
        types = {t.strip("[]").rsplit("_", 1)[0] for _, t in res.replaced}
        self.assertGreaterEqual(types, {"PERSONNE", "NAISSANCE", "EMAIL", "TELEPHONE", "IBAN", "NIR", "SIRET",
                                        "ADRESSE", "CODE_POSTAL"})
        self.assertEqual(m.unmask(res.text), (DOSSIER, []))

    def test_fuite_d_une_valeur_connue(self):
        m = self.masker()
        res = m.mask("M. Jean DUPONT")
        leaks = m.leak_check(res.text + " (le modèle a recopié : jean dupont)")
        self.assertEqual(leaks[0].kind, "VALEUR_CONNUE")

    def test_fuite_d_un_detecteur(self):
        m = self.masker()
        self.assertTrue(m.leak_check("écrire à quelquun@exemple.fr"))

    def test_envoi_bloque_si_fuite(self):
        m = self.masker()
        m.mask("M. Jean DUPONT")  # le coffre connaît « Jean DUPONT »
        m._known = lambda text, res: text  # simule un masquage défaillant : le contrôle doit bloquer
        confirms = []
        # Le nom apparaît en minuscules, hors des motifs : seul le coffre le reconnaît.
        with self.assertRaises(LeakBlocked):
            m.prepare_external("note : jean dupont doit rappeler", confirm=lambda p: confirms.append(p) or True)
        self.assertEqual(confirms, [])  # bloqué avant même l'aperçu

    def test_valeur_du_coffre_remasquee_avec_son_jeton(self):
        m = self.masker()
        token = m.mask("M. Jean DUPONT").text.split()[-1]  # « [PERSONNE_1] »
        # Le nom revient en minuscules, hors des motifs : le code le remasque avec le MÊME jeton.
        res = m.prepare_external("note : jean  dupont doit rappeler", confirm=lambda p: True)
        self.assertEqual(res.text, f"note : {token} doit rappeler")
        self.assertEqual(m.leak_check(res.text), [])
        self.assertEqual(m.unmask(res.text)[0], "note : Jean DUPONT doit rappeler")

    def test_ancien_indice_remasque_au_lieu_de_bloquer(self):
        # Indice indirect retenu lors d'un envoi précédent, plus signalé par Qwen ensuite.
        llm = FakeLLM([json.dumps({"indices": [{"passage": "le directeur de l'agence de Brive", "raison": "poste"}]}),
                       json.dumps({"indices": []})])
        m = self.masker(llm)
        m.mask("Avis de le directeur de l'agence de Brive sur le projet.")
        res = m.prepare_external("Consigne : demander le directeur de l'agence de Brive.", confirm=lambda p: True)
        self.assertNotIn("Brive", res.text)
        self.assertIn("[INDICE_1]", res.text)

    def test_indice_du_meme_envoi_remasque_quelle_que_soit_la_casse(self):
        # Vu en réel : synthèse bloquée (VALEUR_CONNUE) par un indice retenu dans le même masquage.
        llm = FakeLLM([json.dumps({"indices": [{"passage": "Sans recette", "raison": "x"}]})])
        m = self.masker(llm)
        res = m.mask("Idée 3 : Sans recette. Idée 11 : cuisiner sans  recette, avec les restes.")
        self.assertEqual(m.leak_check(res.text), [])
        self.assertEqual(res.text.count("[INDICE_1]"), 2)

    def test_valeur_connue_mot_entier_seulement(self):
        m = self.masker()
        m.vault.token_for("PERSONNE", "Ali")
        self.assertEqual(m.leak_check("3 idées réalisables en une semaine"), [])
        self.assertEqual(m.mask("3 idées réalisables").text, "3 idées réalisables")
        self.assertEqual(m.leak_check("rappeler ALI demain")[0].kind, "VALEUR_CONNUE")

    def test_apercu_obligatoire(self):
        m = self.masker()
        seen = []
        with self.assertRaises(PreviewRefused):
            m.prepare_external(DOSSIER, confirm=lambda p: seen.append(p) or False)
        self.assertIn("Aperçu", seen[0])
        self.assertNotIn("DUPONT", seen[0])
        res = m.prepare_external(DOSSIER, confirm=lambda p: True)
        self.assertNotIn("DUPONT", res.text)

    def test_indices_indirects_verifies(self):
        text = "Le directeur de l'agence de Brive, M. Jean DUPONT, a validé."
        llm = FakeLLM([json.dumps({"indices": [
            {"passage": "Le directeur de l'agence de Brive", "raison": "poste unique"},
            {"passage": "le maire de Tulle", "raison": "inventé"},
            {"passage": "[PERSONNE_1]", "raison": "jeton"}]})])
        m = self.masker(llm)
        res = m.mask(text)
        self.assertNotIn("Brive", res.text)
        self.assertEqual(res.indirect_kept, ["[INDICE_1]"])
        self.assertEqual(len(res.indirect_rejected), 2)  # non vérifiables : ignorés
        self.assertNotIn("Jean DUPONT", llm.calls[0]["messages"][-1].content)  # Qwen ne voit que le texte masqué
        restored, _ = m.unmask(res.text)
        self.assertEqual(restored, text)

    def test_indices_indisponibles(self):
        for answer in (LocalLLMError("éteint"), "pas du json"):
            res = self.masker(FakeLLM([answer])).mask("M. Jean DUPONT")
            self.assertIn("[PERSONNE_1]", res.text)
            self.assertTrue(any("indirects" in w for w in res.warnings))

    def test_avertissement_sans_spacy(self):
        self.assertTrue(any("spaCy" in w for w in self.masker().mask("x").warnings))

    def test_apercu_rendu(self):
        res = self.masker().mask(DOSSIER)
        text = render_preview(res)
        self.assertIn("EMAIL x1", text)
        self.assertIn("relisez", text)


class RandomizedTest(unittest.TestCase):
    """300 dossiers fictifs aléatoires : aucune valeur sensible ne doit survivre au masquage."""

    PRENOMS = ["Jean", "Élodie", "Karim", "Sophie", "Lucas", "Inès", "Hervé", "Chloé", "Mathéo", "Aïcha"]
    NOMS = ["Durand", "Martin", "Lefèvre", "N'Diaye", "Moreau", "Garcia", "Le Gall", "Petit", "Roux", "Fournier"]
    CIVILITES = ["M.", "Mme", "Monsieur", "Madame", "Dr"]
    VOIES = ["rue", "avenue", "boulevard", "chemin", "impasse", "place"]

    def test_aucune_fuite_et_reinjection_exacte(self):
        import random
        rng = random.Random(20261015)
        for i in range(300):
            prenom, nom = rng.choice(self.PRENOMS), rng.choice(self.NOMS)
            nom_maj = nom.upper()
            local = nom.lower().replace(" ", "").replace("'", "")
            email = f"{prenom.lower()}.{local}{i}@exemple.fr"
            tel = "0" + str(rng.randint(1, 7)) + "".join(f" {rng.randint(0, 99):02d}" for _ in range(4))
            iban = "FR76 " + " ".join(f"{rng.randint(0, 9999):04d}" for _ in range(5)) + f" {rng.randint(0, 999):03d}"
            cp = f"{rng.randint(1, 95):02d}{rng.randint(0, 999):03d}"
            nir = f"{rng.randint(1, 2)} {rng.randint(0, 99):02d} {rng.randint(1, 12):02d} {rng.randint(1, 95):02d} {rng.randint(0, 999):03d} {rng.randint(0, 999):03d} {rng.randint(1, 97):02d}"
            siret = f"{rng.randint(100, 999)} {rng.randint(100, 999)} {rng.randint(100, 999)} {rng.randint(10000, 99999)}"
            adresse = f"{rng.randint(1, 200)} {rng.choice(self.VOIES)} des Tilleuls"
            texte = (f"{rng.choice(self.CIVILITES)} {prenom} {nom_maj} ({email}, {tel}) habite {adresse}, {cp} Ville. "
                     f"IBAN {iban}. NIR {nir}. Fournisseur SIRET {siret}. Devis de 1250 euros. "
                     f"Note : {nom} rappellera.")
            m = Masker(Vault())
            res = m.mask(texte, declared=[nom])
            for secret in (nom_maj, nom, email, tel, iban, cp, nir, siret, adresse):
                with self.subTest(i=i, secret=secret):
                    self.assertNotIn(secret, res.text)
            self.assertIn("1250 euros", res.text)
            self.assertEqual(m.leak_check(res.text, declared=[nom]), [], (i, res.text))
            # Sans champ déclaré : les motifs seuls doivent masquer le nom en majuscules.
            sans_note = texte.split(" Note :")[0]
            res2 = Masker(Vault()).mask(sans_note)
            for part in nom_maj.replace("'", " ").split():
                if len(part) > 2:
                    self.assertNotIn(part, res2.text, (i, res2.text))
            restored, unknown = m.unmask(res.text)
            self.assertEqual(unknown, [], i)
            self.assertEqual(restored.casefold(), texte.casefold(), i)  # forme canonique du jeton


class MinimizationTest(unittest.TestCase):
    def test_minimisation(self):
        records = [{"nom": "Durand", "email": "d@exemple.fr", "montant": 120, "notes": "VIP"}]
        self.assertEqual(minimize(records, ["nom", "montant"]), [{"nom": "Durand", "montant": 120}])
        self.assertEqual(declared_values(records, ["nom", "absent"]), ["Durand"])


class PolicyTest(unittest.TestCase):
    def test_donnees_personnelles_local_uniquement(self):
        for provider in ("gemini", "claude"):
            with self.subTest(provider=provider), self.assertRaises(PolicyError):
                check_provider(provider, personal_data=True)
        check_provider("local", personal_data=True)
        check_provider("gemini", personal_data=False)
        with self.assertRaises(PolicyError):
            check_provider("inconnu", personal_data=False)


class SelfTestCommand(unittest.TestCase):
    def test_selftest_avec_chiffrement_de_test(self):
        from unittest import mock
        import privacy.__main__ as cli
        with mock.patch.object(cli, "default_cipher", lambda d: FakeCipher()):
            self.assertEqual(cli.selftest(), 0)


if __name__ == "__main__":
    unittest.main()

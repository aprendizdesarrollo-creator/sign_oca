# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
from io import BytesIO

from PIL import Image
from PyPDF2 import PdfFileReader

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged


def _png(color):
    buf = BytesIO()
    Image.new("RGBA", (60, 20), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue())


@tagged("post_install", "-at_install")
class TestSignAuthorizedSignature(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        manager_groups = "base.group_user,sign_oca.sign_oca_group_manager"
        cls.custodian = new_test_user(
            cls.env, "auth_sig_custodian", groups=manager_groups, email="hum@example.com"
        )
        cls.outsider = new_test_user(
            cls.env, "auth_sig_outsider", groups=manager_groups, email="out@example.com"
        )
        cls.legal_rep = new_test_user(
            cls.env, "auth_sig_legal", groups="base.group_user", email="rl@example.com"
        )
        cls.other_owner = new_test_user(
            cls.env, "auth_sig_other", groups="base.group_user", email="ot@example.com"
        )
        cls.legal_png = _png((0, 0, 255, 255))
        Signature = cls.env["sign.oca.authorized.signature"]
        cls.legal_signature = Signature.create(
            {
                "name": "Representante legal",
                "signer_user_id": cls.legal_rep.id,
                "job_title": "Representante legal",
                "signature": cls.legal_png,
                "custodian_user_ids": [(6, 0, cls.custodian.ids)],
            }
        )
        cls.second_signature = Signature.create(
            {
                "name": "Director",
                "signer_user_id": cls.other_owner.id,
                "signature": _png((255, 0, 0, 255)),
                "custodian_user_ids": [(6, 0, cls.custodian.ids)],
            }
        )

    def _new_mass_send(self, user, **vals):
        MassSend = self.env["sign.oca.mass.send"].with_user(user)
        values = MassSend.default_get(list(MassSend._fields))
        values.update(vals)
        return MassSend.create(values)

    def test_option_count_controls_selector(self):
        mass = self._new_mass_send(self.custodian)
        self.assertEqual(mass.signature_option_count, 2)
        self.custodian.sudo().sign_oca_signature = _png((0, 0, 0, 255))
        mass.invalidate_recordset(["signature_option_count"])
        self.assertEqual(mass.signature_option_count, 3)
        outsider_mass = self._new_mass_send(self.outsider)
        self.assertEqual(outsider_mass.signature_option_count, 0)

    def test_custodian_sees_only_enabled_signatures(self):
        visible = self.env["sign.oca.authorized.signature"].with_user(self.custodian).search([])
        self.assertEqual(visible, self.legal_signature | self.second_signature)
        self.assertFalse(
            self.env["sign.oca.authorized.signature"].with_user(self.outsider).search([])
        )

    def test_custodian_uses_authorized_signature(self):
        mass = self._new_mass_send(
            self.custodian, authorized_signature_id=self.legal_signature.id
        )
        # aunque el formulario traiga otra imagen, se usa la configurada
        mass.signature = _png((0, 255, 0, 255))
        mass._check_ready()
        self.assertEqual(mass.signer_user_id, self.legal_rep)
        self.assertEqual(mass.signer_job_title, "Representante legal")
        self.assertEqual(mass.signature, self.legal_png)

    def test_non_custodian_cannot_use_signature(self):
        mass = self._new_mass_send(
            self.outsider, authorized_signature_id=self.legal_signature.id
        )
        with self.assertRaises(UserError):
            mass._check_ready()

    def test_own_saved_signature_required(self):
        mass = self._new_mass_send(self.outsider)
        with self.assertRaises(UserError):
            mass._check_ready()
        own = _png((10, 10, 10, 255))
        self.outsider.sudo().sign_oca_signature = own
        mass._check_ready()
        self.assertEqual(mass.signer_user_id, self.outsider)
        self.assertEqual(mass.signature, own)

    def test_enable_signature_from_user(self):
        self.outsider.sign_oca_authorized_signature_ids = [(4, self.legal_signature.id)]
        self.assertIn(self.outsider, self.legal_signature.custodian_user_ids)
        mass = self._new_mass_send(self.outsider)
        self.assertEqual(mass.signature_option_count, 1)

    def test_single_authorized_signature_is_default(self):
        self.second_signature.custodian_user_ids = [(5, 0, 0)]
        values = (
            self.env["sign.oca.mass.send"]
            .with_user(self.custodian)
            .default_get(["authorized_signature_id", "signer_user_id", "signature"])
        )
        self.assertEqual(values["authorized_signature_id"], self.legal_signature.id)
        self.assertEqual(values["signer_user_id"], self.legal_rep.id)


@tagged("post_install", "-at_install")
class TestSignOnBehalf(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.tools import misc

        cls.data = base64.b64encode(
            open(misc.file_path("sign_oca/tests/empty.pdf"), "rb").read()
        )
        cls.custodian = new_test_user(
            cls.env,
            "behalf_custodian",
            groups="base.group_user,sign_oca.sign_oca_group_manager",
            email="cus@example.com",
        )
        cls.stranger = new_test_user(
            cls.env, "behalf_stranger", groups="base.group_user", email="st@example.com"
        )
        cls.owner = new_test_user(
            cls.env, "behalf_owner", groups="base.group_user", email="ow@example.com"
        )
        cls.owner_png = _png((0, 0, 255, 255))
        cls.authorized = cls.env["sign.oca.authorized.signature"].create(
            {
                "name": "Firma del dueño",
                "signer_user_id": cls.owner.id,
                "signature": cls.owner_png,
                "custodian_user_ids": [(6, 0, cls.custodian.ids)],
            }
        )
        role = cls.env.ref("sign_oca.sign_role_customer")
        cls.request = cls.env["sign.oca.request"].create(
            {
                "name": "Acuerdo de prueba",
                "data": cls.data,
                "signer_ids": [
                    (0, 0, {"partner_id": cls.owner.partner_id.id, "role_id": role.id})
                ],
            }
        )
        cls.item = cls.request.add_item(
            {
                "role_id": role.id,
                "field_id": cls.env.ref("sign_oca.sign_field_signature").id,
                "page": 1,
                "position_x": 10,
                "position_y": 10,
                "width": 20,
                "height": 10,
            }
        )
        cls.request.action_send()
        cls.signer = cls.request.signer_ids

    def _as(self, user):
        # como en el portal: registro en sudo, usuario real de la petición
        return self.signer.with_user(user).sudo()

    def test_custodian_detected(self):
        self.assertEqual(self._as(self.custodian)._get_on_behalf_signature(), self.authorized)
        self.assertFalse(self._as(self.stranger)._get_on_behalf_signature())
        self.assertFalse(self._as(self.owner)._get_on_behalf_signature())

    def test_info_shows_on_behalf(self):
        info = self._as(self.custodian).get_info()
        self.assertEqual(info["on_behalf"]["name"], self.owner.partner_id.name)
        self.assertFalse(info["can_save_signature"])
        self.assertTrue(info["saved_signature"].endswith(self.owner_png.decode()))
        self.assertTrue(info["sent_by"])

    def test_custodian_signs_with_authorized_image(self):
        drawn = "data:image/png;base64," + _png((0, 255, 0, 255)).decode()
        key = str(self.item["id"])
        self._as(self.custodian).action_sign({key: {"value": drawn}})
        self.assertTrue(self.signer.signed_on)
        self.assertEqual(self.signer.signed_by_user_id, self.custodian)
        self.assertEqual(self.signer.authorized_signature_id, self.authorized)
        stored = self.request.signatory_data[key]["value"]
        self.assertTrue(stored.endswith(self.owner_png.decode()))
        self.assertIn(
            "a nombre de",
            " ".join(self.request.message_ids.mapped("body")),
        )

    def test_custodian_cannot_reject(self):
        with self.assertRaises(UserError):
            self._as(self.custodian).action_reject(reason="no")

    def test_pdf_metadata(self):
        reader = PdfFileReader(BytesIO(base64.b64decode(self.request.data)))
        info = reader.getDocumentInfo()
        self.assertEqual(info["/Title"], "Acuerdo de prueba")
        self.assertEqual(info["/Producer"], "Odoo - Firmas")
        self.assertNotIn("/SignOcaSigners", info)
        self._as(self.custodian).action_sign(
            {str(self.item["id"]): {"value": "x"}}
        )
        self.request._write_pdf_metadata(final=True)
        info = PdfFileReader(BytesIO(base64.b64decode(self.request.data))).getDocumentInfo()
        self.assertIn(self.owner.partner_id.name, info["/SignOcaSigners"])
        self.assertIn(self.custodian.name, info["/SignOcaSigners"])

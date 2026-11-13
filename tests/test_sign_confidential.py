# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import re
from io import BytesIO
from unittest.mock import patch

from PyPDF2 import PdfFileReader

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import misc

CONFIDENTIAL_MODULE = "odoo.addons.sign_oca.models.sign_oca_confidential"


class ConfidentialCommon:
    @classmethod
    def _setup_confidential(cls):
        cls.data = base64.b64encode(
            open(misc.file_path("sign_oca/tests/empty.pdf"), "rb").read()
        )
        cls.role = cls.env.ref("sign_oca.sign_role_customer")
        cls.signer_partner = cls.env["res.partner"].create(
            {"name": "Firmante NDA", "email": "firmante@example.com"}
        )
        employee_vals = {
            "name": "Firmante NDA",
            "work_contact_id": cls.signer_partner.id,
            "identification_id": "1.020.304",
        }
        # el módulo empleados vuelve obligatorios los nombres por separado
        Employee = cls.env["hr.employee"]
        for field_name, value in (("first_name", "Firmante"), ("surname", "NDA")):
            if field_name in Employee._fields:
                employee_vals[field_name] = value
        Employee.create(employee_vals)
        cls.sign_admin = new_test_user(
            cls.env, "conf_sign_admin", groups="base.group_user,sign_oca.sign_oca_group_admin"
        )
        cls.creator = new_test_user(
            cls.env, "conf_creator", groups="base.group_user,sign_oca.sign_oca_group_creator"
        )
        cls.other_user = new_test_user(
            cls.env, "conf_other", groups="base.group_user,sign_oca.sign_oca_group_user"
        )
        cls.confidential_user = new_test_user(
            cls.env,
            "conf_manager",
            groups="base.group_user,sign_oca.sign_oca_group_confidential",
        )

    @classmethod
    def _create_request(cls, **vals):
        values = {
            "name": "NDA confidencial",
            "data": cls.data,
            "is_confidential": True,
            "access_protection": "document_number",
            "signer_ids": [
                (0, 0, {"partner_id": cls.signer_partner.id, "role_id": cls.role.id})
            ],
        }
        values.update(vals)
        return cls.env["sign.oca.request"].with_user(cls.creator).create(values).sudo()


# post_install: los módulos que extienden hr.employee (nombres obligatorios)
# deben estar cargados para poder crear el empleado de prueba
@tagged("post_install", "-at_install")
class TestSignConfidential(ConfidentialCommon, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_confidential()
        cls.request = cls._create_request()
        cls.signer = cls.request.signer_ids

    def _visible_for(self, user, record=None):
        record = record or self.request
        return bool(
            self.env["sign.oca.request"].with_user(user).search([("id", "=", record.id)])
        )

    def test_confidential_hidden_from_sign_admin(self):
        self.assertFalse(self._visible_for(self.sign_admin))
        self.assertFalse(self._visible_for(self.other_user))
        self.assertTrue(self._visible_for(self.creator))
        self.assertTrue(self._visible_for(self.confidential_user))
        with self.assertRaises(AccessError):
            self.request.with_user(self.sign_admin).read(["name"])
        # los firmantes y el registro de actividad también quedan ocultos
        self.assertFalse(
            self.env["sign.oca.request.signer"]
            .with_user(self.sign_admin)
            .search([("request_id", "=", self.request.id)])
        )
        self.assertFalse(
            self.env["sign.oca.request.log"]
            .with_user(self.sign_admin)
            .search([("request_id", "=", self.request.id)])
        )

    def test_regular_document_still_visible_to_sign_admin(self):
        regular = self._create_request(is_confidential=False, access_protection="none")
        self.assertTrue(self._visible_for(self.sign_admin, regular))

    def test_responsible_sees_document(self):
        request = self.env["sign.oca.request"].create(
            {
                "name": "NDA de otro responsable",
                "data": self.data,
                "is_confidential": True,
                "user_id": self.other_user.id,
            }
        )
        self.assertTrue(self._visible_for(self.other_user, request))
        self.assertFalse(self._visible_for(self.creator, request))

    def test_access_list_grants_and_revokes(self):
        access = (
            self.env["sign.oca.request.access"]
            .with_user(self.creator)
            .create({"request_id": self.request.id, "user_id": self.other_user.id})
        )
        self.assertTrue(self._visible_for(self.other_user))
        self.assertIn(
            "grant_access",
            self.env["sign.oca.request.log"]
            .sudo()
            .search([("request_id", "=", self.request.id)])
            .mapped("action"),
        )
        access.with_user(self.creator).unlink()
        self.assertFalse(self._visible_for(self.other_user))

    def test_only_managers_change_permissions(self):
        with self.assertRaises(AccessError):
            self.env["sign.oca.request.access"].with_user(self.sign_admin).create(
                {"request_id": self.request.id, "user_id": self.sign_admin.id}
            )
        self.env["sign.oca.request.access"].with_user(self.confidential_user).create(
            {"request_id": self.request.id, "user_id": self.other_user.id}
        )
        # quien solo tiene permiso de lectura no puede quitar la confidencialidad
        with self.assertRaises(AccessError):
            self.request.with_user(self.other_user).write({"is_confidential": False})

    def test_document_number_key(self):
        self.assertTrue(self.signer.check_access_key("1020304"))
        self.assertTrue(self.signer.check_access_key("1.020.304"))
        self.assertFalse(self.signer.check_access_key("999"))
        self.assertFalse(self.signer.check_access_key(""))

    def test_lock_after_failed_attempts(self):
        for _attempt in range(5):
            self.assertFalse(self.signer.check_access_key("000"))
        self.assertTrue(self.signer._access_locked())
        self.assertFalse(self.signer.check_access_key("1020304"))

    def test_defined_password(self):
        request = self._create_request(
            access_protection="password", access_password="Clave-NDA-1"
        )
        self.assertTrue(request.access_password_hash)
        self.assertNotIn("Clave-NDA-1", request.access_password_hash)
        self.assertFalse(request.access_password)
        self.assertTrue(request.signer_ids.check_access_key("Clave-NDA-1"))
        self.assertFalse(request.signer_ids.check_access_key("clave-nda-1"))

    def test_password_mode_requires_password(self):
        with self.assertRaises(ValidationError):
            self._create_request(access_protection="password")

    def test_otp_single_use(self):
        with patch(f"{CONFIDENTIAL_MODULE}.secrets.randbelow", return_value=123456):
            self.assertTrue(self.signer.send_access_otp())
        # no se reenvía antes de un minuto
        self.assertFalse(self.signer.send_access_otp())
        self.assertFalse(self.signer.check_access_otp("000000"))
        self.assertTrue(self.signer.check_access_otp("123456"))
        self.assertFalse(self.signer.check_access_otp("123456"))

    def test_encrypted_copy_uses_document_number(self):
        attachment = self.request._get_encrypted_copy_for_partner(self.signer_partner)
        reader = PdfFileReader(BytesIO(attachment.raw))
        self.assertTrue(reader.isEncrypted)
        self.assertFalse(reader.decrypt("000"))
        self.assertTrue(reader.decrypt("1020304"))
        without_number = self.env["res.partner"].create({"name": "Sin documento"})
        self.assertFalse(self.request._get_encrypted_copy_for_partner(without_number))

    def test_template_propagates_configuration(self):
        template = self.env["sign.oca.template"].create(
            {
                "name": "Plantilla NDA",
                "data": self.data,
                "is_confidential": True,
                "access_protection": "password",
                "access_password": "Plantilla-1",
                "require_otp": True,
            }
        )
        vals = template._prepare_sign_oca_request_vals_from_record(False)
        self.assertTrue(vals["is_confidential"])
        self.assertTrue(vals["require_otp"])
        self.assertEqual(vals["access_protection"], "password")
        self.assertEqual(vals["access_password_hash"], template.access_password_hash)

    def test_partner_document_numbers(self):
        supplier = self.env["res.partner"].create(
            {"name": "Proveedor", "vat": "900.123.456-7"}
        )
        numbers = supplier._sign_oca_document_numbers()
        self.assertIn("9001234567", numbers)
        self.assertIn("900123456", numbers)


@tagged("post_install", "-at_install")
class TestSignConfidentialPortal(ConfidentialCommon, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_confidential()
        cls.request = cls._create_request()
        cls.request.add_item(
            {
                "role_id": cls.role.id,
                "field_id": cls.env.ref("sign_oca.sign_field_name").id,
                "page": 1,
                "position_x": 10,
                "position_y": 10,
                "width": 10,
                "height": 10,
            }
        )
        cls.request.with_user(cls.creator).action_send()
        cls.signer = cls.request.signer_ids

    def _csrf(self, html):
        return re.search(r'name="csrf_token"\s+value="([^"]+)"', html).group(1)

    def _content_url(self):
        return f"/sign_oca/content/{self.signer.id}/{self.signer.access_token}"

    def test_portal_asks_key_before_showing_document(self):
        page = self.url_open(self.signer.access_url)
        self.assertIn('name="key"', page.text)
        self.assertNotEqual(self.url_open(self._content_url()).content, base64.b64decode(self.data))

        unlock_url = f"/sign_oca/unlock/{self.signer.id}/{self.signer.access_token}"
        wrong = self.url_open(
            unlock_url,
            data={"csrf_token": self._csrf(page.text), "step": "key", "key": "111"},
        )
        self.assertIn("La clave no es correcta", wrong.text)

        self.url_open(
            unlock_url,
            data={"csrf_token": self._csrf(wrong.text), "step": "key", "key": "1020304"},
        ).raise_for_status()
        content = self.url_open(self._content_url()).content
        self.assertEqual(content, base64.b64decode(self.request.data))
        self.assertEqual(
            PdfFileReader(BytesIO(content)).getDocumentInfo()["/Title"],
            "NDA confidencial",
        )

# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Documentos confidenciales.

Un documento marcado como confidencial solo lo ven su responsable, quien lo
emitió, sus firmantes, los usuarios que se agreguen a su lista de permisos y
el grupo «Documentos confidenciales». Ni siquiera el administrador de firmas
lo ve si no está en alguno de esos casos.

Además se puede pedir una clave (el número de documento del firmante o una
clave definida) y un código de un solo uso enviado por correo antes de abrir
el documento desde el enlace de firma.
"""

import logging
import re
import secrets
from base64 import b64decode
from datetime import timedelta
from io import BytesIO

from markupsafe import Markup
from passlib.context import CryptContext
from PyPDF2 import PdfFileReader, PdfFileWriter
from PyPDF2.constants import UserAccessPermissions

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression

_logger = logging.getLogger(__name__)

CONFIDENTIAL_GROUP = "sign_oca.sign_oca_group_confidential"
# minutos que dura abierto un documento tras validar la clave en el portal
UNLOCK_MINUTES = 30
OTP_MINUTES = 10
OTP_RESEND_SECONDS = 60
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15

PROTECTION_SELECTION = [
    ("none", "Sin clave"),
    ("document_number", "Número de documento del firmante"),
    ("password", "Clave definida por el emisor"),
]
CONFIDENTIAL_FIELDS = (
    "is_confidential",
    "access_protection",
    "access_password",
    "require_otp",
)

_crypt = CryptContext(["pbkdf2_sha512"])


def _normalize_key(value):
    """Deja solo letras y números en mayúscula: «1.234.567» = «1234567»."""
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()


class SignOcaConfidentialMixin(models.AbstractModel):
    """Opciones de confidencialidad comunes a plantillas, solicitudes y envíos
    masivos."""

    _name = "sign.oca.confidential.mixin"
    _description = "Opciones de documento confidencial"

    is_confidential = fields.Boolean(
        string="Documento confidencial",
        tracking=True,
        help="Solo lo ven el responsable, quien lo emitió, los firmantes, los "
        "usuarios de la lista de permisos y el grupo «Documentos "
        "confidenciales». El administrador de firmas no lo ve si no está en "
        "alguno de esos casos.",
    )
    access_protection = fields.Selection(
        PROTECTION_SELECTION,
        string="Clave para abrir",
        default="none",
        required=True,
        help="Clave que se pide al abrir el documento desde el enlace de firma. "
        "Con «Número de documento del firmante» la copia firmada que llega "
        "por correo también queda cifrada con ese número.",
    )
    access_password = fields.Char(
        string="Clave",
        compute="_compute_access_password",
        inverse="_inverse_access_password",
        help="Solo se guarda cifrada: no se puede consultar después.",
    )
    access_password_hash = fields.Char(readonly=True)
    has_access_password = fields.Boolean(compute="_compute_access_password")
    require_otp = fields.Boolean(
        string="Código de verificación por correo",
        help="Además de la clave, se envía un código de un solo uso al correo "
        "del firmante y se pide antes de abrir el documento.",
    )

    @api.depends("access_password_hash")
    def _compute_access_password(self):
        for record in self:
            record.access_password = False
            record.has_access_password = bool(record.sudo().access_password_hash)

    def _inverse_access_password(self):
        for record in self:
            if record.access_password:
                record.sudo().access_password_hash = _crypt.hash(
                    record.access_password
                )

    @api.model
    def _hash_password_vals(self, vals):
        # se cifra antes de guardar: la restricción de clave obligatoria se
        # valida antes de que corra el inverse
        if vals.get("access_password"):
            vals = dict(vals)
            vals["access_password_hash"] = _crypt.hash(vals.pop("access_password"))
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._hash_password_vals(v) for v in vals_list])

    def write(self, vals):
        return super().write(self._hash_password_vals(vals))

    def _confidential_vals(self):
        """Valores para heredar la configuración en el documento generado."""
        self.ensure_one()
        return {
            "is_confidential": self.is_confidential,
            "access_protection": self.access_protection,
            "access_password_hash": self.sudo().access_password_hash,
            "require_otp": self.require_otp,
        }

    @api.constrains("is_confidential", "access_protection", "access_password_hash")
    def _check_access_password(self):
        for record in self:
            if (
                record.access_protection == "password"
                and not record.sudo().access_password_hash
            ):
                raise ValidationError(
                    self.env._("Defina la clave con la que se abrirá el documento.")
                )


class SignOcaTemplate(models.Model):
    _name = "sign.oca.template"
    _inherit = ["sign.oca.template", "sign.oca.confidential.mixin"]

    def _prepare_sign_oca_request_vals_from_record(self, record):
        vals = super()._prepare_sign_oca_request_vals_from_record(record)
        vals.update(self._confidential_vals())
        return vals


class SignOcaRequest(models.Model):
    _name = "sign.oca.request"
    _inherit = ["sign.oca.request", "sign.oca.confidential.mixin"]

    confidential_access_ids = fields.One2many(
        "sign.oca.request.access",
        "request_id",
        string="Permisos",
        copy=True,
        help="Usuarios que, sin participar en el documento, pueden verlo.",
    )
    has_confidential_access = fields.Boolean(
        compute="_compute_has_confidential_access",
        search="_search_has_confidential_access",
        help="El usuario actual puede ver el documento aunque sea confidencial.",
    )
    can_manage_confidentiality = fields.Boolean(
        compute="_compute_can_manage_confidentiality"
    )

    @api.model
    def _confidential_access_domain(self):
        """Documentos que el usuario actual puede ver aunque sean confidenciales.

        Lo usa la regla de registro global, así que no puede depender de otras
        reglas: solo de campos propios del documento.
        """
        user = self.env.user
        if user.has_group(CONFIDENTIAL_GROUP):
            return expression.TRUE_DOMAIN
        return expression.OR(
            [
                [("user_id", "=", user.id)],
                [("create_uid", "=", user.id)],
                [("signer_ids.partner_id", "=", user.partner_id.id)],
                [("confidential_access_ids.user_id", "=", user.id)],
            ]
        )

    def _search_has_confidential_access(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            raise ValidationError(self.env._("Operación de búsqueda no soportada."))
        domain = self._confidential_access_domain()
        if (operator == "=") == value:
            return domain
        return ["!"] + expression.normalize_domain(domain)

    @api.depends_context("uid")
    def _compute_has_confidential_access(self):
        user = self.env.user
        is_member = user.has_group(CONFIDENTIAL_GROUP)
        for record in self:
            sudo_record = record.sudo()
            record.has_confidential_access = bool(
                is_member
                or sudo_record.user_id == user
                or sudo_record.create_uid == user
                or user.partner_id in sudo_record.signer_ids.partner_id
                or user in sudo_record.confidential_access_ids.user_id
            )

    @api.depends_context("uid")
    def _compute_can_manage_confidentiality(self):
        for record in self:
            record.can_manage_confidentiality = record._is_confidential_manager()

    def _is_confidential_manager(self):
        """Puede cambiar la confidencialidad y la lista de permisos."""
        self.ensure_one()
        user = self.env.user
        if self.env.su or user.has_group(CONFIDENTIAL_GROUP):
            return True
        if not self.id:
            return True
        record = self.sudo()
        return user in (record.user_id | record.create_uid)

    def _check_confidential_manager(self):
        for record in self:
            if not record._is_confidential_manager():
                raise AccessError(
                    self.env._(
                        "Solo el responsable, quien emitió el documento o el "
                        "grupo «Documentos confidenciales» pueden cambiar la "
                        "confidencialidad o los permisos de «%s».",
                        record.name,
                    )
                )

    def write(self, vals):
        if any(
            name in vals for name in CONFIDENTIAL_FIELDS + ("access_password_hash",)
        ):
            self._check_confidential_manager()
        return super().write(vals)

    def _needs_portal_unlock(self):
        self.ensure_one()
        return self.access_protection != "none" or self.require_otp

    # ------------------------------------------------------------------
    # Correos al completar las firmas
    # ------------------------------------------------------------------
    def _post_signed_certificate(self):
        """En un documento confidencial los adjuntos quedan como nota interna:
        un comentario reenviaría el PDF sin clave a todos los seguidores."""
        if not self.is_confidential:
            return super()._post_signed_certificate()
        self.ensure_one()
        attachments = self._get_completion_attachments()
        self.message_post(
            body=self.env._(
                "Documento confidencial firmado por todos los firmantes. Se "
                "adjuntan el documento final (protegido contra modificación) y "
                "el certificado de firmas."
            ),
            attachment_ids=attachments.ids,
            subtype_xmlid="mail.mt_note",
        )

    def _notify_completion_documents(self):
        """Documento confidencial: cada destinatario recibe su copia cifrada
        con su número de documento. Si no se puede cifrar para él, no se
        adjunta el documento y se le indica dónde consultarlo."""
        if not self.is_confidential:
            return super()._notify_completion_documents()
        self.ensure_one()
        certificate = self.env["ir.attachment"].sudo()
        try:
            certificate = self._get_signature_certificate_attachment()
        except Exception:
            _logger.exception(
                "No se pudo generar el certificado de firmas de %s", self.name
            )
        partners = self.signer_ids.mapped("partner_id") | self.user_id.partner_id
        for partner in partners:
            attachments = certificate
            copy = False
            if self.access_protection == "document_number":
                copy = self._get_encrypted_copy_for_partner(partner)
            if copy:
                attachments |= copy
                body = self.env._(
                    "%(name)s ha enviado el documento confidencial firmado por "
                    "todos los participantes. El PDF adjunto se abre con su "
                    "número de documento, sin puntos ni espacios.",
                    name=self.create_uid.name,
                )
            else:
                body = self.env._(
                    "%(name)s ha enviado el documento confidencial firmado por "
                    "todos los participantes. Por ser confidencial no se adjunta "
                    "por correo: consúltelo en Odoo, en Firmas o en Mis "
                    "documentos de firma.",
                    name=self.create_uid.name,
                )
            self.env["mail.thread"].with_context(
                mail_notify_force_send=False
            ).message_notify(
                body=body,
                partner_ids=partner.ids,
                subject=self.env._("Documento firmado: %s", self.name),
                subtype_id=self.env.ref("mail.mt_comment").id,
                mail_auto_delete=False,
                attachment_ids=attachments.ids,
            )
        return partners

    def _get_encrypted_copy_for_partner(self, partner):
        """Copia del documento firmado que se abre con el número de documento
        de `partner`. Vacío si no tiene número registrado."""
        self.ensure_one()
        numbers = partner._sign_oca_document_numbers()
        if not numbers:
            return self.env["ir.attachment"]
        try:
            reader = PdfFileReader(BytesIO(b64decode(self.data)), strict=False)
            writer = PdfFileWriter()
            writer.clone_document_from_reader(reader)
            writer.encrypt(
                user_password=numbers[0],
                owner_password=secrets.token_urlsafe(24),
                permissions_flag=(
                    UserAccessPermissions.PRINT
                    | UserAccessPermissions.PRINT_TO_REPRESENTATION
                    | UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS
                ),
            )
            buf = BytesIO()
            writer.write(buf)
        except Exception:
            _logger.exception(
                "No se pudo cifrar la copia de %s para %s", self.name, partner.name
            )
            return self.env["ir.attachment"]
        base_name = (self.filename or "%s.pdf" % self.name).removesuffix(".pdf")
        return (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": "%s (con clave).pdf" % base_name,
                    "description": self.env._(
                        "Copia cifrada para %s", partner.display_name
                    ),
                    "raw": buf.getvalue(),
                    "mimetype": "application/pdf",
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
        )


class SignOcaMassSend(models.Model):
    _name = "sign.oca.mass.send"
    _inherit = ["sign.oca.mass.send", "sign.oca.confidential.mixin"]


class SignOcaMassSendLine(models.Model):
    _inherit = "sign.oca.mass.send.line"

    def _prepare_request_vals(self, pdf_bytes, boxes):
        vals = super()._prepare_request_vals(pdf_bytes, boxes)
        vals.update(self.mass_send_id._confidential_vals())
        return vals


class SignOcaRequestSigner(models.Model):
    _inherit = "sign.oca.request.signer"

    access_failed_count = fields.Integer(readonly=True, copy=False)
    access_locked_until = fields.Datetime(readonly=True, copy=False)
    otp_hash = fields.Char(readonly=True, copy=False)
    otp_expiry = fields.Datetime(readonly=True, copy=False)
    otp_sent_on = fields.Datetime(readonly=True, copy=False)

    def _access_locked(self):
        self.ensure_one()
        return bool(
            self.access_locked_until
            and self.access_locked_until > fields.Datetime.now()
        )

    def _register_failed_access(self):
        self.ensure_one()
        count = self.access_failed_count + 1
        vals = {"access_failed_count": count}
        if count >= MAX_FAILED_ATTEMPTS:
            vals.update(
                {
                    "access_failed_count": 0,
                    "access_locked_until": fields.Datetime.now()
                    + timedelta(minutes=LOCK_MINUTES),
                }
            )
        self.sudo().write(vals)
        self._set_action_log("unlock_failed")

    def check_access_key(self, key):
        """Valida la clave del documento. Devuelve True o False; tras varios
        intentos fallidos bloquea el enlace unos minutos."""
        self.ensure_one()
        sign_request = self.request_id.sudo()
        if sign_request.access_protection == "none":
            return True
        if self._access_locked():
            return False
        key = (key or "").strip()
        valid = False
        if key and sign_request.access_protection == "document_number":
            valid = _normalize_key(key) in self.partner_id._sign_oca_document_numbers()
        elif key and sign_request.access_protection == "password":
            valid = bool(
                sign_request.access_password_hash
                and _crypt.verify(key, sign_request.access_password_hash)
            )
        if not valid:
            self._register_failed_access()
            return False
        self.sudo().access_failed_count = 0
        return True

    def send_access_otp(self):
        """Genera un código de 6 dígitos y lo envía al correo del firmante.

        Devuelve False si se pidió hace menos de un minuto o el firmante no
        tiene correo."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.otp_sent_on and self.otp_sent_on > now - timedelta(
            seconds=OTP_RESEND_SECONDS
        ):
            return False
        if not self.partner_id.email:
            return False
        code = "%06d" % secrets.randbelow(1000000)
        self.sudo().write(
            {
                "otp_hash": _crypt.hash(code),
                "otp_expiry": now + timedelta(minutes=OTP_MINUTES),
                "otp_sent_on": now,
            }
        )
        body = Markup(
            "<p>%s</p><p style='font-size:24px;letter-spacing:6px;'><b>%s</b></p>"
            "<p>%s</p>"
        ) % (
            self.env._(
                "Su código para abrir el documento «%s» es:", self.request_id.name
            ),
            code,
            self.env._(
                "Vence en %s minutos. Si usted no lo pidió, ignore este correo.",
                OTP_MINUTES,
            ),
        )
        # sudo: el firmante suele llegar como usuario público desde el enlace
        self.env["mail.mail"].sudo().create(
            {
                "subject": self.env._("Código de verificación: %s", self.request_id.name),
                "body_html": body,
                "email_to": self.partner_id.email_formatted,
                "email_from": self.request_id.sudo().company_id.email_formatted
                or self.env.ref("base.partner_root").sudo().email_formatted,
                "auto_delete": True,
            }
        ).send()
        self._set_action_log("otp_sent")
        return True

    def check_access_otp(self, code):
        self.ensure_one()
        if self._access_locked():
            return False
        code = (code or "").strip()
        valid = bool(
            code
            and self.otp_hash
            and self.otp_expiry
            and self.otp_expiry > fields.Datetime.now()
            and _crypt.verify(code, self.otp_hash)
        )
        if not valid:
            self._register_failed_access()
            return False
        # un solo uso
        self.sudo().write({"otp_hash": False, "otp_expiry": False})
        return True


class SignRequestLog(models.Model):
    _inherit = "sign.oca.request.log"

    action = fields.Selection(
        selection_add=[
            ("unlock", "Apertura con clave"),
            ("unlock_failed", "Clave o código incorrecto"),
            ("otp_sent", "Código de verificación enviado"),
            ("grant_access", "Permiso concedido"),
            ("revoke_access", "Permiso retirado"),
        ],
        ondelete={
            "unlock": "cascade",
            "unlock_failed": "cascade",
            "otp_sent": "cascade",
            "grant_access": "cascade",
            "revoke_access": "cascade",
        },
    )


class SignOcaRequestAccess(models.Model):
    """Lista de permisos de un documento confidencial."""

    _name = "sign.oca.request.access"
    _description = "Permiso sobre documento confidencial"
    _order = "request_id desc, id"
    _rec_name = "user_id"

    request_id = fields.Many2one(
        "sign.oca.request",
        string="Documento",
        required=True,
        ondelete="cascade",
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Usuario",
        required=True,
        ondelete="cascade",
        domain=[("share", "=", False)],
    )
    note = fields.Char(string="Motivo")
    request_state = fields.Selection(related="request_id.state", string="Estado")
    is_confidential = fields.Boolean(related="request_id.is_confidential")

    _sql_constraints = [
        (
            "request_user_unique",
            "unique(request_id, user_id)",
            "El usuario ya tiene permiso sobre este documento.",
        )
    ]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.request_id._check_confidential_manager()
        for record in records:
            record._log_change("grant_access", self.env._("concedió acceso a"))
        return records

    def write(self, vals):
        self.request_id._check_confidential_manager()
        if "request_id" in vals:
            self.env["sign.oca.request"].browse(
                vals["request_id"]
            )._check_confidential_manager()
        return super().write(vals)

    def unlink(self):
        self.request_id._check_confidential_manager()
        for record in self:
            record._log_change("revoke_access", self.env._("retiró el acceso a"))
        return super().unlink()

    def _log_change(self, action, verb):
        self.ensure_one()
        sign_request = self.request_id
        sign_request._set_action_log(action)
        # sudo: el grupo confidencial gestiona permisos pero no escribe en la
        # solicitud, y publicar en el chatter exige escritura
        sign_request.sudo().message_post(
            author_id=self.env.user.partner_id.id,
            body=Markup("<p>%s %s <b>%s</b>%s</p>")
            % (
                self.env.user.display_name,
                verb,
                self.user_id.display_name,
                Markup(": %s") % self.note if self.note else "",
            ),
            subtype_xmlid="mail.mt_note",
        )


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _sign_oca_document_numbers(self):
        """Números de documento con los que se identifica el contacto.

        Toma la identificación del empleado asociado (contacto de trabajo o
        usuario) y el NIF/NIT del contacto. Del NIT también se acepta el
        número sin dígito de verificación.
        """
        self.ensure_one()
        partner = self.sudo()
        values = []
        employees = (
            self.env["hr.employee"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    "|",
                    ("work_contact_id", "=", partner.id),
                    ("user_partner_id", "=", partner.id),
                ]
            )
        )
        values += employees.mapped("identification_id")
        if partner.vat:
            values.append(partner.vat)
            if "-" in partner.vat:
                values.append(partner.vat.rsplit("-", 1)[0])
        numbers = []
        for value in values:
            number = _normalize_key(value)
            if number and number not in numbers:
                numbers.append(number)
        return numbers

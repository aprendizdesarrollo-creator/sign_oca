# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Firmas autorizadas en custodia.

Una firma autorizada es la firma de una persona (por ejemplo el representante
legal) que pueden usar en los envíos masivos los usuarios que la custodian.
Así una sola sesión (la de Talento Humano, por ejemplo) gestiona varias
firmas sin que cada dueño tenga que entrar a trazarla.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


class SignOcaAuthorizedSignature(models.Model):
    _name = "sign.oca.authorized.signature"
    _inherit = ["mail.thread"]
    _description = "Firma autorizada en custodia"
    _order = "signer_user_id, id"

    name = fields.Char(string="Descripción", required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    signer_user_id = fields.Many2one(
        "res.users",
        string="Dueño de la firma",
        required=True,
        domain=[("share", "=", False)],
        tracking=True,
    )
    job_title = fields.Char(string="Cargo", tracking=True)
    signature = fields.Binary(
        string="Firma",
        attachment=True,
        required=True,
        help="Se puede trazar o cargar como imagen (PNG con fondo transparente "
        "se ve mejor).",
    )
    custodian_user_ids = fields.Many2many(
        "res.users",
        "sign_oca_authorized_signature_custodian_rel",
        "signature_id",
        "user_id",
        string="Habilitada para",
        domain=[("share", "=", False)],
        tracking=True,
        help="Usuarios que pueden usar esta firma en los envíos masivos.",
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )

    @api.onchange("signer_user_id")
    def _onchange_signer_user_id(self):
        employee = self.signer_user_id.employee_ids[:1]
        if employee and not self.job_title:
            self.job_title = employee.job_title or employee.job_id.name

    @api.model
    def _get_available(self, user=None):
        """Firmas autorizadas que `user` puede usar."""
        user = user or self.env.user
        return self.sudo().search(
            [
                ("custodian_user_ids", "in", user.ids),
                ("company_id", "in", user.company_ids.ids),
            ]
        )


class SignOcaMassSend(models.Model):
    _inherit = "sign.oca.mass.send"

    authorized_signature_id = fields.Many2one(
        "sign.oca.authorized.signature",
        string="Firma a usar",
        domain="[('custodian_user_ids', 'in', uid)]",
        help="Vacío: se usa la firma guardada en su propio perfil.",
    )
    signature_option_count = fields.Integer(
        compute="_compute_signature_option_count"
    )

    @api.depends_context("uid")
    def _compute_signature_option_count(self):
        user = self.env.user
        count = len(self.env["sign.oca.authorized.signature"]._get_available(user))
        if user.sudo().sign_oca_signature:
            count += 1
        for record in self:
            record.signature_option_count = count

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        user = self.env.user
        available = self.env["sign.oca.authorized.signature"]._get_available(user)
        # sin firma propia y con una sola autorizada, esa es la que va
        authorized = available.browse()
        if not user.sudo().sign_oca_signature and len(available) == 1:
            authorized = available
            values["authorized_signature_id"] = authorized.id
        for name, value in self._signature_values(authorized).items():
            if name in fields_list:
                values[name] = value
        return values

    def _signature_values(self, authorized=None):
        """Firmante, cargo y firma según la opción elegida."""
        authorized = authorized or self.env["sign.oca.authorized.signature"]
        if authorized:
            authorized = authorized.sudo()
            return {
                "signer_user_id": authorized.signer_user_id.id,
                "signer_job_title": authorized.job_title,
                "signature": authorized.signature,
            }
        user = self.env.user
        employee = user.employee_ids[:1]
        return {
            "signer_user_id": user.id,
            "signer_job_title": employee.job_title or employee.job_id.name or False,
            "signature": user.sudo().sign_oca_signature,
        }

    @api.onchange("authorized_signature_id")
    def _onchange_authorized_signature_id(self):
        self.update(self._signature_values(self.authorized_signature_id))

    def _onchange_signer_user_id(self):
        # El firmante ya no se elige a mano: sale de la firma seleccionada.
        return

    @api.onchange("department_ids")
    def _onchange_department_ids(self):
        """Los departamentos elegidos mandan sobre la lista de empleados.

        Antes solo se sumaban (`|=`): al quitar un departamento sus empleados
        se quedaban en la lista y al vaciar la selección seguían todos, así
        que era fácil enviarle el documento a quien ya no tocaba. Ahora la
        lista se recalcula desde cero, de modo que cambiar de departamento
        borra a los empleados anteriores.
        """
        if not self.department_ids:
            self.employee_ids = self.env["hr.employee"]
            return
        self.employee_ids = self.env["hr.employee"].search(
            [
                ("department_id", "child_of", self.department_ids.ids),
                ("company_id", "in", [self.company_id.id, False]),
            ]
        )

    def _check_ready(self):
        self.ensure_one()
        authorized = self.authorized_signature_id.sudo()
        if authorized:
            if self.env.user not in authorized.custodian_user_ids:
                raise UserError(
                    self.env._(
                        "Usted no está habilitado para usar la firma «%s».",
                        authorized.name,
                    )
                )
        elif not self.env.user.sudo().sign_oca_signature:
            raise UserError(
                self.env._(
                    "No tiene una firma guardada en su perfil ni firmas "
                    "autorizadas. Guárdela en Mis preferencias o pida al "
                    "administrador de firmas que le habilite una."
                )
            )
        # La firma siempre sale de la configuración, no del formulario: así
        # nadie puede estampar una imagen distinta a la autorizada.
        self.write(self._signature_values(authorized))
        if not authorized:
            return super()._check_ready()
        # Quien custodia una firma autorizada no es su dueño, así que no aplica
        # la validación original de «solo el dueño aprueba su firma»: la
        # reemplaza la habilitación del administrador. El resto se repite.
        if self.state != "draft":
            raise UserError(self.env._("El envío ya se procesó."))
        if not (self.exception_public or self.exception_known or self.exception_court):
            raise UserError(
                self.env._("Marque al menos una excepción a la confidencialidad.")
            )
        if self.recipient_type == "supplier":
            raise UserError(
                self.env._(
                    "El envío a proveedores está suspendido: el formato FTGT38 "
                    "solo trae las declaraciones para colaboradores. Falta el "
                    "texto aprobado por Legal para proveedores."
                )
            )
        if not self.signer_user_id.partner_id.email:
            raise UserError(
                self.env._("%s no tiene correo electrónico.") % self.signer_user_id.name
            )
        signature = self.signature
        if isinstance(signature, bytes):
            signature = signature.decode()
        self.env["sign.oca.request.signer"]._validate_signature_value(signature)

    def action_generate(self):
        self.ensure_one()
        result = super().action_generate()
        if self.authorized_signature_id:
            self.authorized_signature_id.sudo().message_post(
                body=self.env._(
                    "%(user)s usó esta firma en el envío masivo %(mass)s.",
                    user=self.env.user.name,
                    mass=self.name,
                )
            )
        return result


class ResUsers(models.Model):
    _inherit = "res.users"

    # Solo el Administrador de firmas: así leer usuarios con todos sus campos
    # no falla por permisos para el resto.
    sign_oca_authorized_signature_ids = fields.Many2many(
        "sign.oca.authorized.signature",
        "sign_oca_authorized_signature_custodian_rel",
        "user_id",
        "signature_id",
        string="Firmas autorizadas",
        groups="sign_oca.sign_oca_group_admin",
        help="Firmas que este usuario puede usar en los envíos masivos.",
    )


class SignOcaRequestSigner(models.Model):
    """Firma desde el portal a nombre de un tercero.

    Quien custodia una firma autorizada puede firmar, con esa firma, los
    documentos que le corresponden a su dueño. La imagen que se estampa es
    siempre la configurada, no la que se trace en el navegador.
    """

    _inherit = "sign.oca.request.signer"

    signed_by_user_id = fields.Many2one(
        "res.users",
        string="Firmado por",
        readonly=True,
        copy=False,
        help="Custodio que firmó a nombre del firmante con una firma autorizada.",
    )
    authorized_signature_id = fields.Many2one(
        "sign.oca.authorized.signature",
        string="Firma autorizada usada",
        readonly=True,
        copy=False,
    )

    def _get_on_behalf_signature(self):
        """Firma autorizada con la que el usuario actual puede firmar por este
        firmante. Vacío si el usuario es el propio firmante o no la custodia."""
        self.ensure_one()
        Signature = self.env["sign.oca.authorized.signature"]
        user = self._current_request_user()
        if user._is_public() or self.partner_id == user.partner_id:
            return Signature
        owners = self.partner_id.sudo().user_ids
        if not owners:
            return Signature
        return Signature._get_available(user).filtered(
            lambda s: s.signer_user_id in owners
        )[:1]

    def _check_signer_identity(self, access_token=False):
        if self._get_on_behalf_signature():
            return
        return super()._check_signer_identity(access_token=access_token)

    def get_info(self, access_token=False):
        info = super().get_info(access_token=access_token)
        sign_request = self.request_id.sudo()
        info["sent_by"] = sign_request.user_id.name or sign_request.create_uid.name
        authorized = self._get_on_behalf_signature()
        info["on_behalf"] = False
        if authorized:
            authorized = authorized.sudo()
            image = authorized.signature
            if isinstance(image, bytes):
                image = image.decode()
            info.update(
                {
                    "saved_signature": "data:image/png;base64," + image,
                    "can_save_signature": False,
                    "on_behalf": {
                        "name": self.partner_id.name,
                        "signature_name": authorized.name,
                        "custodian": self._current_request_user().name,
                    },
                }
            )
        return info

    def action_reject(self, reason=False, access_token=False, reason_id=False):
        if self._get_on_behalf_signature():
            raise UserError(
                self.env._(
                    "Con una firma autorizada solo se puede firmar. El rechazo "
                    "debe hacerlo %s.",
                    self.partner_id.name,
                )
            )
        return super().action_reject(
            reason=reason, access_token=access_token, reason_id=reason_id
        )

    def action_sign(self, items, access_token=False, latitude=False, longitude=False):
        authorized = self._get_on_behalf_signature().sudo()
        if not authorized:
            return super().action_sign(
                items, access_token=access_token, latitude=latitude, longitude=longitude
            )
        image = authorized.signature
        if isinstance(image, bytes):
            image = image.decode()
        image = "data:image/png;base64," + image
        # los campos de firma llevan la imagen autorizada aunque el navegador
        # mande otra
        items = dict(items or {}) if isinstance(items, dict) else items
        if isinstance(items, dict):
            for key, stored in (self.request_id.sudo().signatory_data or {}).items():
                if (
                    stored.get("role_id") == self.role_id.id
                    and stored.get("field_type") == "signature"
                ):
                    entry = dict(items.get(str(key)) or items.get(key) or {})
                    entry["value"] = image
                    items[str(key)] = entry
        custodian = self._current_request_user()
        result = super().action_sign(
            items, access_token=access_token, latitude=latitude, longitude=longitude
        )
        self.sudo().write(
            {"signed_by_user_id": custodian.id, "authorized_signature_id": authorized.id}
        )
        body = self.env._(
            "%(custodian)s firmó a nombre de %(owner)s con la firma autorizada "
            "«%(signature)s».",
            custodian=custodian.name,
            owner=self.partner_id.name,
            signature=authorized.name,
        )
        sign_request = self.request_id.sudo().with_context(mail_notify_force_send=False)
        sign_request.message_post(
            body=body, subtype_xmlid="mail.mt_note", author_id=custodian.partner_id.id
        )
        authorized.message_post(body=body, author_id=custodian.partner_id.id)
        sign_request.message_notify(
            partner_ids=self.partner_id.ids,
            subject=self.env._("Se firmó a su nombre: %s", self.request_id.name),
            body=body,
        )
        return result

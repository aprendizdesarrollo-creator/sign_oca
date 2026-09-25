# Copyright 2023 Dixmit
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib
import json
import logging
import secrets
from base64 import b64decode, b64encode
from datetime import timedelta
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO

from PyPDF2 import PdfFileReader, PdfFileWriter
from PyPDF2.constants import UserAccessPermissions
from reportlab.graphics.shapes import Drawing, Line, Rect
from reportlab.lib.colors import black, transparent
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from markupsafe import Markup
from reportlab.platypus import Image, Paragraph

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request
from odoo.tools import float_repr

_logger = logging.getLogger(__name__)


def _is_field_value_filled(item):
    """Return whether a value is actually completed for its field type."""
    value = item.get("value")
    if item.get("field_type") == "text":
        return isinstance(value, str) and bool(value.strip())
    if item.get("field_type") == "check":
        return value is True
    return bool(value)


class SignOcaRequest(models.Model):
    _name = "sign.oca.request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Sign Request"
    _order = "state, id desc"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    template_id = fields.Many2one("sign.oca.template")
    data = fields.Binary(required=True)
    filename = fields.Char()
    user_id = fields.Many2one(
        comodel_name="res.users",
        string="Responsible",
        default=lambda self: self.env.user,
        required=True,
    )
    record_ref = fields.Reference(
        lambda self: [
            (m.model, m.name)
            for m in self.env["ir.model"]
            .sudo()
            .search([("transient", "=", False), ("model", "not like", "sign.oca")])
        ],
        string="Object",
    )
    signed = fields.Boolean(copy=False)
    signer_ids = fields.One2many(
        "sign.oca.request.signer",
        inverse_name="request_id",
        auto_join=True,
        copy=True,
        string="Signers",
    )
    signer_id = fields.Many2one(
        comodel_name="sign.oca.request.signer",
        compute="_compute_signer_id",
        help="The signer related to the active user.",
        string="Signer",
    )
    signed_partner_ids = fields.Many2many(
        comodel_name="res.partner",
        compute="_compute_signature_status_partners",
        string="Firmaron",
    )
    pending_partner_ids = fields.Many2many(
        comodel_name="res.partner",
        compute="_compute_signature_status_partners",
        string="Pendientes de firma",
    )

    @api.depends("signer_ids", "signer_ids.signed_on", "signer_ids.partner_id")
    def _compute_signature_status_partners(self):
        for record in self:
            record.signed_partner_ids = record.signer_ids.filtered(
                lambda s: s.signed_on
            ).mapped("partner_id")
            record.pending_partner_ids = record.signer_ids.filtered(
                lambda s: not s.signed_on
            ).mapped("partner_id")
    state = fields.Selection(
        [
            ("0_sent", "Sent"),
            ("1_draft", "Draft"),
            ("2_signed", "Signed"),
            ("3_cancel", "Cancelled"),
            ("4_rejected", "Rejected"),
        ],
        default="1_draft",
        required=True,
        copy=False,
        tracking=True,
    )
    rejected_signer_id = fields.Many2one(
        "sign.oca.request.signer",
        string="Rechazado por",
        readonly=True,
        copy=False,
        help="Firmante que rechazó el documento.",
    )
    rejection_reason = fields.Text(
        string="Detalle del rechazo", readonly=True, copy=False
    )
    reject_reason_id = fields.Many2one(
        "sign.oca.reason",
        string="Motivo del rechazo",
        readonly=True,
        copy=False,
    )
    cancellation_reason = fields.Text(
        string="Detalle de la cancelación", readonly=True, copy=False
    )
    cancel_reason_id = fields.Many2one(
        "sign.oca.reason",
        string="Motivo de la cancelación",
        readonly=True,
        copy=False,
    )
    cancelled_by_id = fields.Many2one(
        "res.users", string="Cancelado por", readonly=True, copy=False
    )
    cancelled_on = fields.Datetime(string="Cancelado el", readonly=True, copy=False)
    # --- Versionado -------------------------------------------------
    version = fields.Integer(
        default=1,
        readonly=True,
        copy=False,
        help="Número de versión del documento dentro de su cadena de revisiones.",
    )
    parent_request_id = fields.Many2one(
        "sign.oca.request",
        string="Versión anterior",
        readonly=True,
        copy=False,
        ondelete="set null",
        index=True,
    )
    child_request_ids = fields.One2many(
        "sign.oca.request",
        "parent_request_id",
        string="Versiones posteriores",
        readonly=True,
    )
    version_signatures_kept = fields.Boolean(
        string="Conservó firmas previas",
        readonly=True,
        copy=False,
        help="Al crear esta versión se decidió dar por válidas las firmas ya "
        "realizadas en la versión anterior, en lugar de pedirlas de nuevo.",
    )
    version_note = fields.Text(
        string="Qué cambió en esta versión", readonly=True, copy=False
    )
    has_newer_version = fields.Boolean(compute="_compute_has_newer_version")
    completion_pending = fields.Boolean(
        string="Cierre pendiente",
        readonly=True,
        copy=False,
        index=True,
        help="El documento se firmó por completo y el cron aún debe generar "
        "el certificado y avisar a los implicados.",
    )
    signed_count = fields.Integer(compute="_compute_signed_count")
    signer_count = fields.Integer(compute="_compute_signer_count")
    to_sign = fields.Boolean(compute="_compute_to_sign")
    signatory_data = fields.Serialized(
        default=lambda r: {},
        copy=False,
    )
    current_hash = fields.Char(copy=False)
    company_id = fields.Many2one(
        "res.company",
        default=lambda r: r.env.company.id,
        required=True,
    )
    next_item_id = fields.Integer(compute="_compute_next_item_id")
    ask_location = fields.Boolean()

    @api.depends("signer_ids")
    @api.depends_context("uid")
    def _compute_signer_id(self):
        user = self.env.user
        for record in self:
            user_diff_roles = record.signer_ids.filtered(
                lambda x: x.partner_id == user.partner_id.commercial_partner_id
            )
            record.signer_id = (
                fields.first(user_diff_roles.filtered(lambda x: x.is_allow_signature))
                if user_diff_roles.filtered(lambda x: x.is_allow_signature)
                else fields.first(user_diff_roles)
            )

    @api.depends(
        "signer_ids",
        "signer_ids.is_allow_signature",
    )
    @api.depends_context("uid")
    def _compute_to_sign(self):
        for record in self:
            record.to_sign = (
                record.signer_id.is_allow_signature if record.signer_id else False
            )

    def sign(self):
        self.ensure_one()
        if not self.signer_id:
            return self.get_formview_action()
        return self.signer_id.sign()

    @api.depends("signatory_data")
    def _compute_next_item_id(self):
        for record in self:
            record.next_item_id = (
                record.signatory_data
                and max([int(key) for key in record.signatory_data.keys()])
                or 0
            ) + 1

    def preview(self):
        self.ensure_one()
        self._set_action_log("view")
        return {
            "type": "ir.actions.client",
            "tag": "sign_oca_preview",
            "name": self.name,
            "params": {
                "res_model": self._name,
                "res_id": self.id,
            },
        }

    def get_info(self):
        self.ensure_one()
        return {
            "name": self.name,
            "items": self.signatory_data,
            "roles": [
                {"id": signer.role_id.id, "name": signer.role_id.name}
                for signer in self.signer_ids
            ],
            "fields": [
                {"id": field.id, "name": field.name}
                for field in self.env["sign.oca.field"].search([])
            ],
        }

    def _ensure_draft(self):
        self.ensure_one()
        if not self.signer_ids:
            raise ValidationError(
                self.env._(
                    "There are no signers, please fill them before configuring it"
                )
            )
        if not self.state == "1_draft":
            raise ValidationError(
                self.env._("You can only configure requests in draft state")
            )

    def configure(self):
        self._ensure_draft()
        self._set_action_log("configure")
        return {
            "type": "ir.actions.client",
            "tag": "sign_oca_configure",
            "name": self.name,
            "params": {
                "res_model": self._name,
                "res_id": self.id,
            },
        }

    def delete_item(self, item_id):
        self._ensure_draft()
        data = self.signatory_data
        data.pop(str(item_id))
        self.signatory_data = data
        self._set_action_log("delete_field")

    def set_item_data(self, item_id, vals):
        self._ensure_draft()
        data = self.signatory_data
        data[str(item_id)].update(vals)
        self.signatory_data = data
        self._set_action_log("edit_field")

    def add_item(self, item_vals):
        self._ensure_draft()
        item_id = self.next_item_id
        field_id = self.env["sign.oca.field"].browse(item_vals["field_id"])
        signatory_data = self.signatory_data
        signatory_data[item_id] = {
            "id": item_id,
            "field_id": field_id.id,
            "field_type": field_id.field_type,
            "required": False,
            "name": field_id.name,
            "role_id": self.signer_ids[0].role_id.id,
            "page": 1,
            "position_x": 0,
            "position_y": 0,
            "width": 0,
            "height": 0,
            "value": False,
            "default_value": field_id.default_value,
            "placeholder": "",
        }
        signatory_data[item_id].update(item_vals)
        self.signatory_data = signatory_data
        self._set_action_log("add_field")
        return signatory_data[item_id]

    def cancel(self, reason=False, reason_id=False):
        """Cancela la solicitud. El motivo queda registrado y visible."""
        reason = (reason or "").strip()
        vals = {"state": "3_cancel"}
        if reason or reason_id:
            vals.update(
                {
                    "cancellation_reason": reason,
                    "cancel_reason_id": reason_id and reason_id.id or False,
                    "cancelled_by_id": self.env.user.id,
                    "cancelled_on": fields.Datetime.now(),
                }
            )
        self.write(vals)
        for record in self:
            record._set_action_log("cancel")
            if reason or reason_id:
                motivo = " · ".join(
                    filter(None, [reason_id and reason_id.name or "", reason])
                )
                record.message_post(
                    body=Markup("<p><b>%s</b> %s</p><p><b>%s</b> %s</p>")
                    % (
                        self.env.user.display_name,
                        self.env._("canceló esta solicitud de firma."),
                        self.env._("Motivo:"),
                        motivo,
                    ),
                    subtype_xmlid="mail.mt_comment",
                )

    def action_cancel_wizard(self):
        """Botón: pide el motivo antes de cancelar."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Cancelar solicitud de firma"),
            "res_model": "sign.oca.request.cancel",
            "view_mode": "form",
            "target": "new",
            "context": {"default_request_id": self.id},
        }

    @api.depends("signer_ids")
    def _compute_signer_count(self):
        for record in self:
            record.signer_count = len(record.signer_ids)

    @api.depends("signer_ids", "signer_ids.signed_on")
    def _compute_signed_count(self):
        for record in self:
            record.signed_count = len(record.signer_ids.filtered(lambda r: r.signed_on))

    def open_template(self):
        return self.template_id.configure()

    def action_send(self, sign_now=False, message=""):
        self.ensure_one()
        if self.state != "1_draft":
            return
        self._set_action_log("validate")
        self.state = "0_sent"
        for signer in self.signer_ids:
            signer._portal_ensure_token()
            if sign_now and signer.partner_id == self.env.user.partner_id:
                continue
            render_result = self.env["ir.qweb"]._render(
                "sign_oca.sign_oca_template_mail",
                {"record": signer, "body": message, "link": signer.access_url},
                engine="ir.qweb",
                minimal_qcontext=True,
            )
            self.env["mail.thread"].message_notify(
                body=render_result,
                partner_ids=signer.partner_id.ids,
                subject=self.env._("New document to sign"),
                subtype_id=self.env.ref("mail.mt_comment").id,
                mail_auto_delete=False,
                email_layout_xmlid="mail.mail_notification_light",
            )

    def action_send_signed_request(self):
        self.ensure_one()
        if (
            self.state != "2_signed"
            or not self.env.company.sign_oca_send_sign_request_copy
        ):
            return
        self._notify_completion_documents()

    def _notify_completion_documents(self):
        """Envía a todos los involucrados (firmantes + responsable) el documento
        firmado protegido y el certificado de firmas."""
        self.ensure_one()
        attachments = self._get_completion_attachments()
        partners = self.signer_ids.mapped("partner_id") | self.user_id.partner_id
        for partner in partners:
            # The message will not be linked to the record because we do not want
            # it happen.
            # force_send=False: los correos se encolan y los envía el cron de
            # correo de Odoo. Enviándolos aquí, cada destinatario con un SMTP
            # lento sumaba decenas de segundos al proceso.
            self.env["mail.thread"].with_context(mail_notify_force_send=False).message_notify(
                body=self.env._(
                    "%(name)s (%(email)s) ha enviado el documento firmado por "
                    "todos los participantes. Se adjuntan el documento final "
                    "(protegido contra modificación) y el certificado de firmas.",
                    name=self.create_uid.name,
                    email=self.create_uid.email,
                ),
                partner_ids=partner.ids,
                subject=self.env._("Documento firmado: %s", self.name),
                subtype_id=self.env.ref("mail.mt_comment").id,
                mail_auto_delete=False,
                attachment_ids=attachments.ids,
            )
        return partners

    def action_send_certificate(self):
        """Botón: envía manualmente el certificado a todos los involucrados."""
        self.ensure_one()
        if self.state != "2_signed" or not all(
            self.signer_ids.mapped("signed_on")
        ):
            raise UserError(
                self.env._(
                    "El certificado solo se puede enviar cuando el documento "
                    "está completamente firmado por todos los firmantes."
                )
            )
        partners = self._notify_completion_documents()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Certificado enviado"),
                "message": self.env._(
                    "Se envió el certificado y el documento firmado a: %s",
                    ", ".join(partners.mapped("name")),
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def _check_signed(self):
        self.ensure_one()
        if self.state != "0_sent":
            return
        self.invalidate_recordset(["state", "signatory_data"])
        signers = self.env["sign.oca.request.signer"].sudo().search(
            [("request_id", "=", self.id)]
        )
        if not signers or not all(signers.mapped("signed_on")):
            return
        for item in (self.signatory_data or {}).values():
            if not isinstance(item, dict):
                return
            if item.get("role_id") and not _is_field_value_filled(item):
                return
        if self.state == "0_sent":
            self.write({"state": "2_signed"})
            # Generar el certificado y el PDF protegido cuesta varios segundos.
            # Si se hace aquí, el último firmante se queda esperando (y si algo
            # falla, su firma ya está guardada pero el navegador nunca recibe
            # respuesta). Se deja marcado y lo hace el cron.
            self.completion_pending = True

    def _process_pending_completions(self):
        """Cron: publica certificado y avisa a los implicados.

        Se ejecuta fuera de la petición del firmante para que firmar sea
        inmediato.
        """
        pending = self.search(
            [("completion_pending", "=", True), ("state", "=", "2_signed")], limit=50
        )
        for request in pending:
            try:
                request._post_signed_certificate()
                request.action_send_signed_request()
                request.completion_pending = False
                # cada documento en su propia transacción: que uno falle no
                # debe impedir el resto
                self.env.cr.commit()
            except Exception:  # noqa: BLE001
                self.env.cr.rollback()
                _logger.exception(
                    "No se pudo cerrar la firma del documento %s (id=%s)",
                    request.name,
                    request.id,
                )

    @api.model
    def get_dashboard_data(self):
        """Indicadores del tablero de firmas.

        Solo cuenta la última versión de cada documento: las versiones
        anteriores quedan como historial y falsearían los totales.
        """
        base = [("child_request_ids", "=", False)]
        counts = {}
        for key, state in [
            ("draft", "1_draft"),
            ("sent", "0_sent"),
            ("signed", "2_signed"),
            ("cancelled", "3_cancel"),
            ("rejected", "4_rejected"),
        ]:
            counts[key] = self.search_count(base + [("state", "=", state)])
        total = sum(counts.values())
        # tasa de firma: firmados sobre lo que salió a firma
        closed = counts["signed"] + counts["rejected"] + counts["cancelled"]
        rate = round(counts["signed"] * 100.0 / closed, 1) if closed else 0.0

        my_partner_id = self.env.user.partner_id.id
        my_signed_count = self.search_count(
            base
            + [
                (
                    "signer_ids",
                    "any",
                    [("partner_id", "=", my_partner_id), ("signed_on", "!=", False)],
                )
            ]
        )
        pending_signers = self.env["sign.oca.request.signer"].search_count(
            [
                ("signed_on", "=", False),
                ("rejected_on", "=", False),
                ("request_id.state", "=", "0_sent"),
            ]
        )
        month_start = fields.Date.context_today(self).replace(day=1)
        signed_this_month = self.search_count(
            base
            + [
                ("state", "=", "2_signed"),
                ("write_date", ">=", fields.Datetime.to_datetime(month_start)),
            ]
        )
        recent = self.search(base, order="write_date desc", limit=8)
        pending_docs = self.search(
            base + [("state", "=", "0_sent")], order="create_date asc", limit=8
        )
        my_signers = self.env["sign.oca.request.signer"].search(
            [
                ("partner_id", "=", self.env.user.partner_id.id),
                ("signed_on", "=", False),
                ("rejected_on", "=", False),
                ("request_id.state", "=", "0_sent"),
            ],
            order="create_date asc",
        )
        return {
            "counts": counts,
            "total": total,
            "rate": rate,
            "pending_signers": pending_signers,
            "signed_this_month": signed_this_month,
            "recent": [
                {
                    "id": r.id,
                    "name": r.name,
                    "state": r.state,
                    "state_label": dict(
                        self._fields["state"]._description_selection(self.env)
                    ).get(r.state),
                    "signed": r.signed_count,
                    "signers": r.signer_count,
                    "version": r.version,
                }
                for r in recent
            ],
            "pending": [
                {
                    "id": r.id,
                    "name": r.name,
                    "signed": r.signed_count,
                    "signers": r.signer_count,
                    "date": r.create_date and fields.Date.to_string(r.create_date),
                }
                for r in pending_docs
            ],
            "my_partner_id": my_partner_id,
            "my_pending_count": len(my_signers),
            "my_signed_count": my_signed_count,
            "my_pending": [
                {
                    "signer_id": s.id,
                    "id": s.request_id.id,
                    "name": s.request_id.name,
                    "role": s.role_id.name,
                    "date": s.create_date and fields.Date.to_string(s.create_date),
                    "url": s.access_url,
                }
                for s in my_signers[:6]
            ],
            "departments": self._get_dashboard_departments(),
        }

    @api.model
    def _get_dashboard_departments(self):
        """Reparto de firmas por departamento del firmante.

        El firmante es un contacto; se une con el empleado por su contacto de
        trabajo y, si no, por el usuario asociado. Quien no corresponda a
        ningún empleado (clientes, proveedores) queda fuera del reparto.
        """
        signers = self.env["sign.oca.request.signer"].search(
            [("request_id.state", "in", ("0_sent", "2_signed", "4_rejected"))]
        )
        if not signers:
            return []
        partner_ids = signers.mapped("partner_id").ids
        employees = (
            self.env["hr.employee"]
            .sudo()
            .search_read(
                [
                    "|",
                    ("work_contact_id", "in", partner_ids),
                    ("user_partner_id", "in", partner_ids),
                ],
                ["work_contact_id", "user_partner_id", "department_id"],
            )
        )
        partner_to_dept = {}
        for emp in employees:
            if not emp["department_id"]:
                continue
            for key in ("work_contact_id", "user_partner_id"):
                if emp[key]:
                    partner_to_dept.setdefault(emp[key][0], emp["department_id"])
        stats = {}
        for signer in signers:
            dept = partner_to_dept.get(signer.partner_id.id)
            if not dept:
                continue
            entry = stats.setdefault(
                dept[0],
                {
                    "id": dept[0],
                    "name": dept[1],
                    "signed": 0,
                    "pending": 0,
                    "rejected": 0,
                    "partner_ids": [],
                },
            )
            if signer.partner_id.id not in entry["partner_ids"]:
                entry["partner_ids"].append(signer.partner_id.id)
            if signer.signed_on:
                entry["signed"] += 1
            elif signer.rejected_on:
                entry["rejected"] += 1
            else:
                entry["pending"] += 1
        result = []
        for entry in stats.values():
            total = entry["signed"] + entry["pending"] + entry["rejected"]
            if not total:
                continue
            entry["total"] = total
            entry["rate"] = round(entry["signed"] * 100.0 / total)
            entry["rejected_rate"] = round(entry["rejected"] * 100.0 / total)
            result.append(entry)
        return sorted(result, key=lambda d: d["total"], reverse=True)[:8]

    @api.depends("child_request_ids")
    def _compute_has_newer_version(self):
        for record in self:
            record.has_newer_version = bool(record.child_request_ids)

    def action_view_versions(self):
        """Abre toda la cadena de versiones del documento."""
        self.ensure_one()
        root = self
        while root.parent_request_id:
            root = root.parent_request_id
        chain = root
        pending = root
        while pending:
            pending = pending.mapped("child_request_ids")
            chain |= pending
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Versiones del documento"),
            "res_model": "sign.oca.request",
            "view_mode": "list,form",
            "domain": [("id", "in", chain.ids)],
            "context": {"create": False},
        }

    def action_new_version(self):
        """Abre el asistente para subir una versión corregida."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Nueva versión del documento"),
            "res_model": "sign.oca.request.new.version",
            "view_mode": "form",
            "target": "new",
            "context": {"default_request_id": self.id},
        }

    def _notify_rejected(self, signer):
        """Avisa en el chatter (y por correo a los seguidores) del rechazo."""
        self.ensure_one()
        reason = " · ".join(
            filter(
                None,
                [
                    signer.reject_reason_id.name or "",
                    signer.rejection_reason or "",
                ],
            )
        ) or self.env._("Sin motivo indicado")
        # Markup + %-format sobre datos escapados: el motivo lo escribe el
        # firmante desde el portal, así que no puede inyectar HTML.
        body = Markup("<p><b>%s</b> %s</p><p><b>%s</b> %s</p>") % (
            signer.partner_id.display_name,
            self.env._("rechazó la firma de este documento."),
            self.env._("Motivo:"),
            reason,
        )
        # el firmante suele ser un portal/público: sudo para poder postear.
        # mail_notify_force_send=False: el correo se encola y lo envía el cron.
        # Enviándolo aquí, la petición del portal se quedaba esperando al SMTP
        # (con el servidor de correo caído son segundos por destinatario) y el
        # firmante veía la pantalla congelada sin llegar a la de rechazo.
        self.sudo().with_context(mail_notify_force_send=False).message_post(
            body=body,
            subtype_xmlid="mail.mt_comment",
            partner_ids=self._get_rejection_recipients().ids,
        )

    def _get_rejection_recipients(self):
        """A quién avisar del rechazo: quien creó la solicitud.

        Los demás seguidores del chatter reciben la notificación por el
        mecanismo normal de mail.thread.
        """
        self.ensure_one()
        return self.create_uid.partner_id

    def _get_signature_certificate_attachment(self):
        """Genera (una sola vez) el PDF del certificado de firmas como adjunto."""
        self.ensure_one()
        cert_name = self.env._("Certificado de firmas - %s.pdf", self.name)
        existing = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", self._name),
                    ("res_id", "=", self.id),
                    ("name", "=", cert_name),
                ],
                limit=1,
            )
        )
        if existing:
            return existing
        pdf, _report_type = (
            self.env["ir.actions.report"]
            .sudo()
            ._render_qweb_pdf(
                "sign_oca.action_report_signature_certificate", res_ids=self.ids
            )
        )
        return (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": cert_name,
                    "raw": pdf,
                    "mimetype": "application/pdf",
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
        )

    def _get_protected_signed_attachment(self):
        """Copia del documento firmado cifrada contra modificación.

        Se cifra con clave de propietario aleatoria (se descarta) y permisos
        de solo lectura/impresión: el PDF abre sin contraseña pero no se puede
        editar, ensamblar ni rellenar. Idempotente por nombre.
        """
        self.ensure_one()
        base_name = (self.filename or "%s.pdf" % self.name).removesuffix(".pdf")
        prot_name = "%s (protegido).pdf" % base_name
        existing = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", self._name),
                    ("res_id", "=", self.id),
                    ("name", "=", prot_name),
                ],
                limit=1,
            )
        )
        if existing:
            return existing
        reader = PdfFileReader(BytesIO(b64decode(self.data)), strict=False)
        writer = PdfFileWriter()
        writer.clone_document_from_reader(reader)
        read_only = (
            UserAccessPermissions.PRINT
            | UserAccessPermissions.PRINT_TO_REPRESENTATION
            | UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS
        )
        writer.encrypt(
            user_password="",
            owner_password=secrets.token_urlsafe(24),
            permissions_flag=read_only,
        )
        buf = BytesIO()
        writer.write(buf)
        return (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": prot_name,
                    "raw": buf.getvalue(),
                    "mimetype": "application/pdf",
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
        )

    def _get_download_attachment(self):
        """Adjunto que se entrega al descargar el documento.

        Firmado por todos: la copia protegida contra modificación. Antes de eso
        el documento en curso (la copia protegida se guarda una sola vez y no
        debe generarse con firmas incompletas).
        """
        self.ensure_one()
        if self.state == "2_signed":
            try:
                return self._get_protected_signed_attachment()
            except Exception:
                _logger.exception(
                    "No se pudo proteger el PDF firmado de %s; se descarga el original",
                    self.name,
                )
        return (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", self._name),
                    ("res_id", "=", self.id),
                    ("res_field", "=", "data"),
                ],
                limit=1,
            )
        )

    def action_download_signed(self):
        """Descarga el documento firmado protegido."""
        self.ensure_one()
        if self.state != "2_signed":
            raise UserError(self.env._("El documento aún no está firmado por todos."))
        attachment = self._get_download_attachment()
        if not attachment:
            raise UserError(self.env._("No se encontró el documento firmado."))
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true&access_token=%s"
            % (attachment.id, attachment.generate_access_token()[0]),
            "target": "self",
        }

    def _get_completion_attachments(self):
        """Certificado + documento firmado protegido (con fallback a copia simple)."""
        self.ensure_one()
        attachments = self.env["ir.attachment"].sudo()
        try:
            attachments |= self._get_signature_certificate_attachment()
        except Exception:
            _logger.exception(
                "No se pudo generar el certificado de firmas de %s", self.name
            )
        try:
            attachments |= self._get_protected_signed_attachment()
        except Exception:
            _logger.exception(
                "No se pudo proteger el PDF firmado de %s; se adjunta copia simple",
                self.name,
            )
            signed_doc = (
                self.env["ir.attachment"]
                .sudo()
                .search(
                    [
                        ("res_model", "=", self._name),
                        ("res_id", "=", self.id),
                        ("res_field", "=", "data"),
                    ],
                    limit=1,
                )
            )
            if signed_doc:
                attachments |= signed_doc.copy(
                    {
                        "res_field": False,
                        "name": self.filename or "%s.pdf" % self.name,
                    }
                )
        return attachments

    def _post_signed_certificate(self):
        """Al completarse todas las firmas publica en el chatter de la solicitud
        el documento firmado (protegido) y el certificado de firmas."""
        self.ensure_one()
        attachments = self._get_completion_attachments()
        self.message_post(
            body=self.env._(
                "Documento firmado por todos los firmantes. "
                "Se adjuntan el documento final (protegido contra modificación) "
                "y el certificado de firmas."
            ),
            attachment_ids=attachments.ids,
            subtype_xmlid="mail.mt_comment",
        )

    def action_open_portal(self):
        """Abre la vista de portal de la solicitud (como la ve el firmante)."""
        self.ensure_one()
        signer = self.signer_ids.filtered(
            lambda s: s.partner_id == self.env.user.partner_id
        )[:1]
        if not signer:
            raise UserError(
                self.env._(
                    "La vista portal solo está disponible para el firmante "
                    "asociado al usuario actual. Para consultar el documento "
                    "sin firmarlo, utiliza la vista previa."
                )
            )
        signer._portal_ensure_token()
        return {
            "type": "ir.actions.act_url",
            "url": signer.access_url,
            "target": "new",
        }

    @api.model
    def _cron_remind_pending_signers(self, days=3):
        """Recordatorio a firmantes pendientes cada `days` días."""
        limit_date = fields.Datetime.now() - timedelta(days=days)
        signers = self.env["sign.oca.request.signer"].search(
            [
                ("request_id.state", "=", "0_sent"),
                ("signed_on", "=", False),
                ("create_date", "<=", limit_date),
                "|",
                ("last_reminder_date", "=", False),
                ("last_reminder_date", "<=", limit_date),
            ]
        )
        for signer in signers:
            try:
                signer._portal_ensure_token()
                render_result = self.env["ir.qweb"]._render(
                    "sign_oca.sign_oca_template_mail",
                    {
                        "record": signer,
                        "body": self.env._(
                            "Recordatorio: tiene pendiente la firma del documento "
                            "'%s'.",
                            signer.request_id.name,
                        ),
                        "link": signer.access_url,
                    },
                    engine="ir.qweb",
                    minimal_qcontext=True,
                )
                self.env["mail.thread"].message_notify(
                    body=render_result,
                    partner_ids=signer.partner_id.ids,
                    subject=self.env._("Recordatorio: documento pendiente de firma"),
                    subtype_id=self.env.ref("mail.mt_comment").id,
                    mail_auto_delete=False,
                    email_layout_xmlid="mail.mail_notification_light",
                )
                signer.last_reminder_date = fields.Datetime.now()
            except Exception:
                _logger.exception(
                    "No se pudo enviar recordatorio de firma a %s",
                    signer.partner_id.display_name,
                )

    def _set_action_log_vals(self, action, **kwargs):
        vals = kwargs.copy()
        # Bearer tokens grant access to the portal signer endpoint.  They are
        # credentials, not audit data, and must never be persisted in logs.
        vals.pop("access_token", None)
        vals.update(
            {"action": action, "request_id": self.id, "ip": self._get_action_log_ip()}
        )
        return vals

    def _get_action_log_ip(self):
        if not request or not hasattr(request, "httprequest"):
            # This comes from a server call. Set as localhost
            return "0.0.0.0"
        return request.httprequest.access_route[-1]

    def _set_action_log(self, action, **kwargs):
        self.ensure_one()
        return (
            self.env["sign.oca.request.log"]
            .sudo()
            .create(self._set_action_log_vals(action, **kwargs))
        )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            record._set_action_log("create")
        return records


class SignOcaRequestSigner(models.Model):
    _name = "sign.oca.request.signer"
    _inherit = ["portal.mixin", "mail.thread", "mail.activity.mixin"]
    _description = "Sign Request Value"
    _order = "signed_on desc, create_date desc, id desc"

    data = fields.Binary(related="request_id.data")
    request_id = fields.Many2one("sign.oca.request", required=True, ondelete="cascade")
    partner_name = fields.Char(related="partner_id.name")
    partner_id = fields.Many2one("res.partner", required=True, ondelete="restrict")
    role_id = fields.Many2one("sign.oca.role", required=True, ondelete="restrict")
    signed_on = fields.Datetime()
    rejected_on = fields.Datetime(string="Rechazado el", readonly=True, copy=False)
    rejection_reason = fields.Text(
        string="Detalle del rechazo", readonly=True, copy=False
    )
    reject_reason_id = fields.Many2one(
        "sign.oca.reason",
        string="Motivo del rechazo",
        readonly=True,
        copy=False,
    )
    last_reminder_date = fields.Datetime(
        copy=False, help="Último recordatorio de firma pendiente enviado por el cron."
    )
    signature_hash = fields.Char()
    model = fields.Char(compute="_compute_model", store=True)
    res_id = fields.Integer(compute="_compute_res_id", store=True)
    is_allow_signature = fields.Boolean(compute="_compute_is_allow_signature")
    secure_sequence_number = fields.Integer(
        string="Inalteralbility No Gap Sequence #",
        readonly=True,
        copy=False,
        index=True,
    )
    inalterable_hash = fields.Char(
        string="Inalterability Hash", readonly=True, copy=False
    )
    sequence_id = fields.Many2one(
        "ir.sequence", copy=False, default=lambda r: r._get_sequence()
    )
    altered_hash = fields.Boolean(compute="_compute_altered_hash")
    latitude = fields.Float()
    longitude = fields.Float()

    @api.depends("request_id.record_ref")
    def _compute_model(self):
        for item in self.filtered(lambda x: x.request_id.record_ref):
            item.model = item.request_id.record_ref._name

    @api.depends("request_id.record_ref")
    def _compute_res_id(self):
        for item in self.filtered(lambda x: x.request_id.record_ref):
            item.res_id = item.request_id.record_ref.id

    @api.depends("signed_on", "partner_id")
    @api.depends_context("uid")
    def _compute_is_allow_signature(self):
        user = self.env.user
        for item in self:
            item.is_allow_signature = bool(
                not item.signed_on
                and not item.rejected_on
                and item.request_id.state == "0_sent"
                and item.partner_id == user.partner_id
            )

    @api.depends("access_token")
    def _compute_access_url(self):
        result = super()._compute_access_url()
        for record in self:
            record.access_url = f"/sign_oca/document/{record.id}/{record.access_token}"
        return result

    @api.onchange("role_id")
    def _onchange_role_id(self):
        for item in self:
            item.partner_id = item.role_id._get_partner_from_record(
                item.request_id.record_ref
            )

    def _current_request_user(self):
        """Usuario real de la petición, incluso cuando el registro está en sudo."""
        return request.env.user if request else self.env.user

    def _check_signer_identity(self, access_token=False):
        """Enforce the signer boundary at the model entry point.

        Public portal requests may use the document bearer token.  Backend
        calls must come from the exact assigned partner.  A server-side
        superuser call is retained for existing trusted automation, but it is
        never used to bypass an active HTTP user's identity.
        """
        self.ensure_one()
        current_user = self._current_request_user()
        if not current_user._is_public():
            if self.partner_id == current_user.partner_id:
                return
            if not request and self.env.su:
                return
        elif access_token and self.access_token and compare_digest(
            str(access_token), str(self.access_token)
        ):
            return
        raise AccessError(
            self.env._("Este documento solo puede ser gestionado por el firmante asignado.")
        )

    def _can_access_saved_signature(self):
        """Saved signatures are profile data, not document-bearer data."""
        current_user = self._current_request_user()
        if current_user._is_public():
            return False
        if self.partner_id == current_user.partner_id:
            return True
        return not request and self.env.su

    def _get_signature_user(self):
        """Usuario donde se guarda/lee la firma reutilizable del firmante."""
        self.ensure_one()
        if not self._can_access_saved_signature():
            return self.env["res.users"]
        return self.partner_id.sudo().user_ids[:1]

    def save_signature(self, signature, access_token=False):
        """Guarda la firma trazada en el usuario, para reutilizarla luego."""
        self.ensure_one()
        self._check_signer_identity(access_token=access_token)
        if not self._can_access_saved_signature():
            return False
        user = self._get_signature_user()
        if not user or not signature:
            return False
        self._validate_signature_value(signature)
        # llega como 'data:image/png;base64,XXXX'
        if "," in signature:
            signature = signature.split(",", 1)[1]
        user.sudo().sign_oca_signature = signature
        return True

    def get_info(self, access_token=False):
        self.ensure_one()
        self._check_signer_identity(access_token=access_token)
        self._set_action_log("view")
        user = self._get_signature_user()
        saved = user.sudo().sign_oca_signature if user else False
        reasons = (
            self.env["sign.oca.reason"]
            .sudo()
            .search([("usage", "in", ["reject", "both"])])
        )
        default_reason = (
            self.env["sign.oca.reason"].sudo().get_default_reason("reject")
        )
        return {
            # motivos de rechazo que se ofrecen en el portal
            "reject_reasons": [
                {
                    "id": r.id,
                    "name": r.name,
                    "requires_detail": r.requires_detail,
                    "description": r.description or "",
                }
                for r in reasons
            ],
            "default_reject_reason_id": default_reason.id or False,
            # firma guardada por la persona, para ofrecerla de un clic
            "saved_signature": (
                "data:image/png;base64," + saved.decode()
                if isinstance(saved, bytes)
                else ("data:image/png;base64," + saved if saved else False)
            ),
            "can_save_signature": bool(user),
            "role_id": self.role_id.id if not self.signed_on else False,
            "name": self.request_id.template_id.name,
            "items": self.request_id.signatory_data,
            "to_sign": self.request_id.to_sign,
            "ask_location": self.request_id.ask_location,
            "partner": {
                "id": self.partner_id.id,
                "name": self.partner_id.name,
                "email": self.partner_id.email,
                "phone": self.partner_id.phone,
            },
        }

    def sign(self):
        self.ensure_one()
        if not self.is_allow_signature:
            raise ValidationError(
                self.env._("You are not allowed to sign this document.")
            )
        return {
            "target": "new",
            "type": "ir.actions.act_url",
            "url": self.access_url,
        }

    def action_reject(self, reason=False, access_token=False, reason_id=False):
        """El firmante rechaza el documento: nadie más puede firmarlo.

        Las firmas ya realizadas se conservan (quedan en el PDF y en el log),
        pero la solicitud pasa a 'Rechazado' y deja de admitir firmas.
        """
        self.ensure_one()
        self._check_signer_identity(access_token=access_token)
        self._lock_request_for_action()
        self._check_signer_identity(access_token=access_token)
        if self.signed_on:
            raise ValidationError(
                self.env._("%s ya firmó el documento, no puede rechazarlo.")
                % self.partner_id.name
            )
        if self.rejected_on:
            raise ValidationError(
                self.env._("%s ya rechazó este documento.") % self.partner_id.name
            )
        if self.request_id.state != "0_sent":
            raise ValidationError(
                self.env._("Este documento no admite rechazo en su estado actual.")
            )
        reason = (reason or "").strip()
        motivo = (
            self.env["sign.oca.reason"].sudo().browse(int(reason_id))
            if reason_id
            else self.env["sign.oca.reason"]
        )
        if motivo.requires_detail and not reason:
            raise ValidationError(
                self.env._("El motivo «%s» exige explicar el detalle.") % motivo.name
            )
        self.write(
            {
                "rejected_on": fields.Datetime.now(),
                "rejection_reason": reason,
                "reject_reason_id": motivo.id or False,
            }
        )
        self.request_id.sudo().write(
            {
                "state": "4_rejected",
                "rejected_signer_id": self.id,
                "rejection_reason": reason,
                "reject_reason_id": motivo.id or False,
            }
        )
        self._set_action_log("reject")
        self.request_id._notify_rejected(self)
        return {
            "type": "ir.actions.act_url",
            "url": self.access_url,
        }

    def _lock_request_for_action(self):
        """Serialize request-wide signer updates and refresh ORM caches."""
        self.ensure_one()
        sign_request = self.request_id
        sign_request.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM sign_oca_request WHERE id = %s FOR UPDATE",
            (sign_request.id,),
        )
        sign_request.invalidate_recordset()
        self.invalidate_recordset()

    def action_sign(self, items, access_token=False, latitude=False, longitude=False):
        self.ensure_one()
        self._check_signer_identity(access_token=access_token)
        self._lock_request_for_action()
        self._check_signer_identity(access_token=access_token)
        if self.signed_on:
            raise ValidationError(
                self.env._("Users %s has already signed the document")
                % self.partner_id.name
            )
        if self.rejected_on:
            raise ValidationError(
                self.env._("%s rechazó el documento, ya no puede firmarse.")
                % self.partner_id.name
            )
        if self.request_id.state != "0_sent":
            raise ValidationError(self.env._("Request cannot be signed"))
        if not isinstance(items, dict):
            raise ValidationError(self.env._("Invalid signature data"))

        # Validate all fields assigned to this signer before changing any
        # record or generating the signed PDF.  The browser sends the whole
        # item definition, but only the value is accepted from the client so
        # field type, role and position cannot be altered in the request.
        signatory_data = dict(self.request_id.signatory_data or {})
        signer_items = {}
        for key, stored_item in signatory_data.items():
            if stored_item.get("role_id") != self.role_id.id:
                continue
            submitted_item = items.get(key)
            if submitted_item is None:
                submitted_item = items.get(str(key))
            if not isinstance(submitted_item, dict):
                raise ValidationError(
                    self.env._("Field %s is missing") % stored_item.get("name", key)
                )
            item = dict(stored_item)
            item["value"] = submitted_item.get("value")
            self._check_signable(stored_item, item)
            signer_items[key] = item
        signatory_data.update(signer_items)

        self.signed_on = fields.Datetime.now()
        # current_hash = self.request_id.current_hash

        input_data = BytesIO(b64decode(self.request_id.data))
        reader = PdfFileReader(input_data)
        output = PdfFileWriter()
        pages = {}
        for page_number in range(1, reader.numPages + 1):
            pages[page_number] = reader.getPage(page_number - 1)

        for item in signer_items.values():
            page = pages[item["page"]]
            new_page = self._get_pdf_page(item, page.mediaBox)
            if new_page:
                page.mergePage(new_page)
            pages[item["page"]] = page
        for page_number in pages:
            output.addPage(pages[page_number])
        output_stream = BytesIO()
        output.write(output_stream)
        output_stream.seek(0)
        signed_pdf = output_stream.read()
        final_hash = hashlib.sha1(signed_pdf).hexdigest()
        # TODO: Review that the hash has not been changed...
        self.request_id.sudo().write(
            {
                "signatory_data": signatory_data,
                "data": b64encode(signed_pdf),
                "current_hash": final_hash,
            }
        )
        self.signature_hash = final_hash
        self.latitude = latitude
        self.longitude = longitude
        self.request_id.sudo()._check_signed()
        self._set_action_log("sign")
        if self.sequence_id:
            self.flush_recordset()
            new_number = self.sequence_id.next_by_id()
            self.write(
                {
                    "secure_sequence_number": new_number,
                    "inalterable_hash": self._get_new_hash(new_number),
                }
            )
        # El envío del documento firmado lo hace el cron (ver
        # _process_pending_completions): aquí alargaría varios segundos la
        # espera del firmante.
        return {
            "type": "ir.actions.act_url",
            "url": self.access_url,
        }

    def _stamp_own_items(self, pdf_bytes, signatory_data):
        """Estampa sobre `pdf_bytes` los campos ya firmados por este firmante.

        Se usa al crear una versión nueva conservando las firmas anteriores:
        el PDF es otro, así que hay que volver a dibujar en él lo que la
        persona firmó, en la misma posición que tenía.
        """
        self.ensure_one()
        reader = PdfFileReader(BytesIO(pdf_bytes))
        output = PdfFileWriter()
        pages = {}
        for page_number in range(1, reader.numPages + 1):
            pages[page_number] = reader.getPage(page_number - 1)
        for key in signatory_data:
            item = signatory_data[key]
            if item.get("role_id") != self.role_id.id or not item.get("value"):
                continue
            if item.get("page") not in pages:
                # La versión nueva tiene menos páginas: no hay dónde estampar.
                continue
            page = pages[item["page"]]
            new_page = self._get_pdf_page(item, page.mediaBox)
            if new_page:
                page.mergePage(new_page)
            pages[item["page"]] = page
        for page_number in sorted(pages):
            output.addPage(pages[page_number])
        output_stream = BytesIO()
        output.write(output_stream)
        output_stream.seek(0)
        return output_stream.read()

    def _check_signable(self, item, submitted_item=None):
        submitted_item = submitted_item or item
        value_to_check = {**item, "value": submitted_item.get("value")}
        if not _is_field_value_filled(value_to_check):
            field_name = item.get("name") or self.env._("Signature")
            raise ValidationError(self.env._("Field %s is not filled") % field_name)
        if item.get("field_type") == "signature":
            self._validate_signature_value(submitted_item["value"])

    def _validate_signature_value(self, value):
        """Ensure a submitted signature is a real image before signing."""
        if not isinstance(value, str):
            raise ValidationError(self.env._("The signature image is invalid."))
        base64_str = value.split(",", 1)[1] if "," in value else value
        try:
            if len(base64_str) % 4:
                base64_str += "=" * (4 - len(base64_str) % 4)
            image_data = b64decode(base64_str, validate=True)
            ImageReader(BytesIO(image_data)).getSize()
        except Exception as error:
            _logger.info("Invalid signature image received: %s", error)
            raise ValidationError(
                self.env._("The signature image is invalid. Please draw it again.")
            ) from error

    def _get_pdf_page_text(self, item, box):
        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=(box.getWidth(), box.getHeight()))
        if not item["value"]:
            return False
        par = Paragraph(item["value"], style=self._getParagraphStyle())
        par.wrap(
            item["width"] / 100 * float(box.getWidth()),
            item["height"] / 100 * float(box.getHeight()),
        )
        par.drawOn(
            can,
            item["position_x"] / 100 * float(box.getWidth()),
            (100 - item["position_y"] - item["height"]) / 100 * float(box.getHeight()),
        )
        can.save()
        packet.seek(0)
        new_pdf = PdfFileReader(packet)
        return new_pdf.getPage(0)

    def _getParagraphStyle(self):
        return ParagraphStyle(name="Oca Sign Style")

    def _get_pdf_page_check(self, item, box):
        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=(box.getWidth(), box.getHeight()))
        width = item["width"] / 100 * float(box.getWidth())
        height = item["height"] / 100 * float(box.getHeight())
        drawing = Drawing(width=width, height=height)
        drawing.add(
            Rect(
                0,
                0,
                width,
                height,
                strokeWidth=3,
                strokeColor=black,
                fillColor=transparent,
            )
        )
        if item["value"]:
            drawing.add(Line(0, 0, width, height, strokeColor=black, strokeWidth=3))
            drawing.add(Line(0, height, width, 0, strokeColor=black, strokeWidth=3))
        drawing.drawOn(
            can,
            item["position_x"] / 100 * float(box.getWidth()),
            (100 - item["position_y"] - item["height"]) / 100 * float(box.getHeight()),
        )
        can.save()
        packet.seek(0)
        new_pdf = PdfFileReader(packet)
        return new_pdf.getPage(0)

    def _get_pdf_page_signature(self, item, box):
        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=(box.getWidth(), box.getHeight()))
        if not item["value"]:
            return False
        try:
            base64_str = item["value"]
            if "," in base64_str:
                base64_str = item["value"].split(",")[1]
            if len(base64_str) % 4:
                base64_str += "=" * (4 - len(base64_str) % 4)
            image_data = b64decode(base64_str, validate=True)
            par = Image(
                BytesIO(image_data),
                width=item["width"] / 100 * float(box.getWidth()),
                height=item["height"] / 100 * float(box.getHeight()),
            )
            par.drawOn(
                can,
                item["position_x"] / 100 * float(box.getWidth()),
                (100 - item["position_y"] - item["height"])
                / 100
                * float(box.getHeight()),
            )
        except Exception as e:
            _logger.info(f"Error decoding Base64 string: {e}")
            return False
        can.save()
        packet.seek(0)
        new_pdf = PdfFileReader(packet)
        return new_pdf.getPage(0)

    def _get_pdf_page(self, item, box):
        return getattr(self, f"_get_pdf_page_{ item['field_type'] }")(item, box)

    def _set_action_log(self, action, **kwargs):
        self.ensure_one()
        return self.request_id._set_action_log(action, signer_id=self.id, **kwargs)

    def _compute_display_name(self):
        for signer in self:
            signer.display_name = signer.partner_id.display_name

    def _get_sequence(self):
        return self.env.ref(
            "sign_oca.sign_inalterability_sequence", raise_if_not_found=False
        )

    @api.depends(
        lambda r: ["request_id.data", "inalterable_hash", "secure_sequence_number"]
        + r._get_integrity_hash_fields()
    )
    def _compute_altered_hash(self):
        for record in self:
            record.altered_hash = (
                record.inalterable_hash
                and record.inalterable_hash
                != record._get_new_hash(record.secure_sequence_number)
            )

    def _get_new_hash(self, secure_seq_number):
        prev_sign = self.sudo().search(
            [
                ("sequence_id", "=", self.sequence_id.id),
                ("secure_sequence_number", "!=", 0),
                ("secure_sequence_number", "=", int(secure_seq_number) - 1),
            ]
        )
        if prev_sign and len(prev_sign) != 1:
            raise UserError(
                self.env._(
                    "An error occurred when computing the inalterability. "
                    "Impossible to get the unique previous signer information."
                )
            )
        return self._compute_hash(prev_sign.inalterable_hash if prev_sign else "")

    def _compute_hash(self, previous_hash):
        """Computes the hash of the browse_record given as self, based on the hash
        of the previous record in the company's securisation sequence given as
        parameter
        """
        self.ensure_one()
        hash_string = sha256((previous_hash + self._string_to_hash()).encode("utf-8"))
        return hash_string.hexdigest()

    def _string_to_hash(self):
        def _getattrstring(obj, field_str):
            field_value = obj[field_str]
            if obj._fields[field_str].type == "many2one":
                field_value = field_value.id
            if obj._fields[field_str].type == "monetary":
                return float_repr(field_value, obj.currency_id.decimal_places)
            return str(field_value)

        values = {"items": {}}
        for field in self._get_integrity_hash_fields():
            values[field] = _getattrstring(self, field)
        for key, signatory_value in self.request_id.signatory_data.items():
            if signatory_value["role_id"] == self.role_id.id:
                values[key] = signatory_value
        return json.dumps(
            values,
            sort_keys=True,
            ensure_ascii=True,
            indent=None,
            separators=(",", ":"),
        )

    def _get_integrity_hash_fields(self):
        return ["partner_id", "role_id", "signed_on", "signature_hash"]


class SignRequestLog(models.Model):
    _name = "sign.oca.request.log"
    _description = "Sign Request Log"
    _log_access = False
    _description = "Log access and edition on requests"

    uid = fields.Many2one(
        "res.users",
        required=True,
        ondelete="cascade",
        default=lambda r: r.env.user.id,
    )
    date = fields.Datetime(required=True, default=lambda r: fields.Datetime.now())
    partner_id = fields.Many2one(
        "res.partner", required=True, default=lambda r: r.env.user.partner_id.id
    )
    request_id = fields.Many2one("sign.oca.request", required=True, ondelete="cascade")
    signer_id = fields.Many2one("sign.oca.request.signer")
    action = fields.Selection(
        [
            ("create", "Create"),
            ("validate", "Validate"),
            ("view", "View Document"),
            ("sign", "Sign"),
            ("add_field", "Add field"),
            ("edit_field", "Edit field"),
            ("delete_field", "Delete field"),
            ("cancel", "Cancel"),
            ("reject", "Reject"),
            ("configure", "Configure"),
        ],
        required=True,
    )
    # Kept for schema compatibility with older installations.  New code never
    # stores bearer tokens here, and regular users cannot read the field.
    access_token = fields.Char(groups="sign_oca.sign_oca_group_manager")
    ip = fields.Char()

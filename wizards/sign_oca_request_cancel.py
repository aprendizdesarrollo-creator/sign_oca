# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError


class SignOcaRequestCancel(models.TransientModel):
    _name = "sign.oca.request.cancel"
    _description = "Cancelar una solicitud de firma"

    request_id = fields.Many2one(
        "sign.oca.request", required=True, readonly=True, ondelete="cascade"
    )
    reason_id = fields.Many2one(
        "sign.oca.reason",
        string="Motivo",
        domain="[('usage', 'in', ['cancel', 'both'])]",
        default=lambda self: self.env["sign.oca.reason"].get_default_reason("cancel"),
        help="Motivos configurables en Firmas → Configuración. Si ninguno "
        "encaja, escribe el detalle abajo.",
    )
    requires_detail = fields.Boolean(related="reason_id.requires_detail")
    reason = fields.Text(
        string="Detalle",
        help="Queda visible en el documento y registrado en el historial.",
    )
    signed_count = fields.Integer(
        related="request_id.signed_count", string="Firmas ya realizadas"
    )

    @api.constrains("reason_id", "reason")
    def _check_reason(self):
        for wizard in self:
            if not wizard.reason_id and not (wizard.reason or "").strip():
                raise UserError(
                    self.env._(
                        "Indica por qué se cancela: elige un motivo o escríbelo."
                    )
                )

    def action_cancel(self):
        self.ensure_one()
        detail = (self.reason or "").strip()
        if self.reason_id.requires_detail and not detail:
            raise UserError(
                self.env._("El motivo «%s» exige explicar el detalle.")
                % self.reason_id.name
            )
        self.request_id.cancel(reason=detail, reason_id=self.reason_id)
        return {"type": "ir.actions.act_window_close"}

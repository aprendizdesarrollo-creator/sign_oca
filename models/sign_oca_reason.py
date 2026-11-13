# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class SignOcaReason(models.Model):
    """Catálogo de motivos por los que una firma se cancela o se rechaza.

    Se comparte entre los dos flujos porque varios motivos sirven para ambos
    (un documento vencido lo puede cancelar quien lo envió o rechazar quien
    debía firmarlo); `usage` decide dónde se ofrece cada uno.
    """

    _name = "sign.oca.reason"
    _description = "Motivo de cancelación o rechazo de firma"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    usage = fields.Selection(
        [
            ("cancel", "Solo cancelación"),
            ("reject", "Solo rechazo"),
            ("both", "Cancelación y rechazo"),
        ],
        default="both",
        required=True,
        string="Se ofrece en",
    )
    description = fields.Text(
        string="Descripción", help="Ayuda interna sobre cuándo usar este motivo."
    )
    requires_detail = fields.Boolean(
        string="Exigir detalle",
        help="Si se marca, hay que explicar además en el campo de texto libre.",
    )
    is_default = fields.Boolean(
        string="Por defecto",
        help="Motivo preseleccionado. Puede haber uno para cancelación y otro "
        "para rechazo.",
    )

    _sql_constraints = [
        ("name_uniq", "unique(name)", "Ya existe un motivo con ese nombre."),
    ]

    @api.constrains("is_default", "usage", "active")
    def _check_single_default(self):
        """Como mucho un motivo por defecto para cada flujo."""
        for record in self.filtered(lambda r: r.is_default and r.active):
            for flujo in ("cancel", "reject"):
                if record.usage not in (flujo, "both"):
                    continue
                otros = self.search(
                    [
                        ("id", "!=", record.id),
                        ("is_default", "=", True),
                        ("usage", "in", [flujo, "both"]),
                    ]
                )
                if otros:
                    raise ValidationError(
                        self.env._(
                            "Ya hay un motivo por defecto para %(flujo)s: %(otro)s. "
                            "Desmárcalo antes de poner este.",
                            flujo=(
                                self.env._("cancelación")
                                if flujo == "cancel"
                                else self.env._("rechazo")
                            ),
                            otro=otros[0].name,
                        )
                    )

    @api.model
    def get_default_reason(self, usage):
        """Motivo preseleccionado para un flujo ('cancel' o 'reject')."""
        return self.search(
            [("is_default", "=", True), ("usage", "in", [usage, "both"])], limit=1
        )

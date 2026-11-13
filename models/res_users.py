# Copyright 2023-2024 Tecnativa - Víctor Martínez
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    sign_oca_signature = fields.Binary(
        string="Firma guardada",
        attachment=True,
        copy=False,
        help="Firma que la persona trazó y guardó para reutilizarla en los "
        "siguientes documentos que tenga que firmar.",
    )

    @property
    def SELF_READABLE_FIELDS(self):
        # cada quien puede consultar su propia firma guardada
        return super().SELF_READABLE_FIELDS + ["sign_oca_signature"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        # y guardarla o reemplazarla sin permisos de administrador
        return super().SELF_WRITEABLE_FIELDS + ["sign_oca_signature"]

    @api.model
    def sign_oca_pending_count(self):
        """Count only the current user's pending signer assignments.

        The previous implementation grouped signers by the removed ``model``
        and ``res_id`` fields.  The systray only needs a count, so keeping the
        query at the signer level avoids reconstructing source-record links.
        """
        domain = [
            ("request_id.state", "=", "0_sent"),
            ("partner_id", "=", self.env.user.partner_id.id),
            ("signed_on", "=", False),
            ("rejected_on", "=", False),
        ]
        return self.env["sign.oca.request.signer"].search_count(domain)

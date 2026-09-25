# Copyright 2023-2024 Tecnativa - Víctor Martínez
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, modules


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
    def sign_oca_request_user_count(self):
        requests = {}
        domain = [
            ("request_id.state", "=", "0_sent"),
            (
                "partner_id",
                "child_of",
                [self.env.user.partner_id.commercial_partner_id.id],
            ),
            ("signed_on", "=", False),
        ]
        signer_model = self.env["sign.oca.request.signer"]
        signer_groups = signer_model.read_group(domain, ["model"], ["model"])
        for signer_group in signer_groups:
            if signer_group["model"]:
                model = signer_group["model"]
                Model = self.env[model].with_user(self.env.user)
                signers = signer_model.search(signer_group.get("__domain"))
                if signers:
                    total_records = Model.with_context(active_test=False).search_count(
                        [("id", "in", signers.mapped("res_id"))]
                    )
                    if total_records > 0:
                        record = self.env[model]
                        model_id = (
                            self.env["ir.model"].sudo().search([("model", "=", model)])
                        )
                        requests[model] = {
                            "id": model_id.id,
                            "name": record._description,
                            "model": model,
                            "icon": modules.module.get_module_icon(
                                record._original_module
                            ),
                            "total_records": total_records,
                        }
            else:
                signers = signer_model.search(signer_group.get("__domain"))
                requests["undefined"] = {
                    "id": False,
                    "name": self.env._("Undefined"),
                    "model": "sign.oca.request",
                    "icon": modules.module.get_module_icon("sign_oca"),
                    "total_records": len(signers),
                }
        return list(requests.values())

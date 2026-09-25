# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from base64 import b64decode, b64encode

from odoo import api, fields, models
from odoo.exceptions import UserError


class SignOcaRequestNewVersion(models.TransientModel):
    _name = "sign.oca.request.new.version"
    _description = "Nueva versión de un documento a firmar"

    request_id = fields.Many2one(
        "sign.oca.request", required=True, readonly=True, ondelete="cascade"
    )
    data = fields.Binary(string="Documento corregido", required=True)
    filename = fields.Char()
    version_note = fields.Text(
        string="Qué cambió",
        required=True,
        help="Queda registrado en la nueva versión y en el historial. "
        "Es obligatorio: quien reciba la nueva versión debe saber qué se "
        "corrigió para no tener que comparar los dos documentos.",
    )
    signed_count = fields.Integer(
        related="request_id.signed_count", string="Firmas ya realizadas"
    )
    signature_policy = fields.Selection(
        [
            ("reset", "Pedir todas las firmas de nuevo"),
            ("keep", "Dar por válidas las firmas ya realizadas"),
        ],
        default="reset",
        required=True,
        string="Firmas anteriores",
        help="El documento cambió: decide si quienes ya firmaron deben volver "
        "a hacerlo sobre el texto nuevo o si su firma anterior se da por buena.",
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        request = self.env["sign.oca.request"].browse(
            self.env.context.get("default_request_id")
        )
        if request and "filename" in fields_list:
            values.setdefault("filename", request.filename)
        return values

    def action_create_version(self):
        """Crea la siguiente versión del documento y la deja en borrador."""
        self.ensure_one()
        source = self.request_id
        note = (self.version_note or "").strip()
        if not note:
            raise UserError(
                self.env._(
                    "Explica qué cambió respecto a la versión anterior. "
                    "Quien reciba la nueva versión necesita saberlo sin tener "
                    "que comparar los dos documentos."
                )
            )
        if source.child_request_ids:
            raise UserError(
                self.env._(
                    "Esta versión ya tiene una versión posterior (%s). "
                    "Crea la nueva versión a partir de la última."
                )
                % ", ".join(source.child_request_ids.mapped("name"))
            )
        keep = self.signature_policy == "keep"
        new_request = source.copy(
            {
                "name": source.name,
                "data": self.data,
                "filename": self.filename or source.filename,
                "state": "1_draft",
                "version": source.version + 1,
                "parent_request_id": source.id,
                "version_signatures_kept": keep,
                "version_note": note,
                "signatory_data": source.signatory_data,
            }
        )
        # copy() ya arrastra los firmantes con su rol; signed_on/signature_hash
        # son copy=False, así que llegan en blanco (útil para "pedir de nuevo").
        self._apply_signature_policy(source, new_request, keep)
        source.message_post(
            body=self.env._(
                "Se creó la versión %(version)s de este documento. %(policy)s",
                version=new_request.version,
                policy=(
                    self.env._("Se conservaron las firmas ya realizadas.")
                    if keep
                    else self.env._("Se pidieron todas las firmas de nuevo.")
                ),
            )
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "sign.oca.request",
            "res_id": new_request.id,
            "view_mode": "form",
        }

    def _apply_signature_policy(self, source, new_request, keep):
        """Aplica al documento nuevo lo decidido sobre las firmas anteriores.

        Con `keep`, quien ya había firmado queda marcado como firmado y su
        firma se vuelve a estampar sobre el PDF nuevo, en la misma posición.
        Sin `keep`, la línea de firma queda en blanco y todos firman de nuevo.
        """
        signatory_data = new_request.signatory_data
        if not keep:
            cleaned = {}
            for key, item in signatory_data.items():
                item = dict(item)
                item["value"] = False
                cleaned[key] = item
            new_request.write({"signatory_data": cleaned})
            # signed_on se copia con el registro (no es copy=False), así que
            # hay que borrarlo a mano para que todos vuelvan a firmar.
            new_request.signer_ids.write(
                {
                    "signed_on": False,
                    "signature_hash": False,
                    "latitude": False,
                    "longitude": False,
                }
            )
            return

        pdf_bytes = b64decode(new_request.data)
        # se empareja por persona y rol, no por orden de la lista
        signed_by_key = {
            (s.partner_id.id, s.role_id.id): s
            for s in source.signer_ids
            if s.signed_on
        }
        for new_signer in new_request.signer_ids:
            old_signer = signed_by_key.get(
                (new_signer.partner_id.id, new_signer.role_id.id)
            )
            if not old_signer:
                continue
            new_signer.write(
                {
                    "signed_on": old_signer.signed_on,
                    "signature_hash": old_signer.signature_hash,
                }
            )
            pdf_bytes = new_signer._stamp_own_items(pdf_bytes, signatory_data)
        new_request.write({"data": b64encode(pdf_bytes)})

# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Metadatos del PDF firmado.

Al enviar a firma se descartan los metadatos del archivo de origen (autor,
programa, rutas, XMP) y se escriben los del documento. Al completarse las
firmas se agregan quién lo envió, quiénes firmaron y cuándo.
"""

import hashlib
import logging
from base64 import b64decode, b64encode
from io import BytesIO

from PyPDF2 import PdfFileReader, PdfFileWriter

from odoo import fields, models

_logger = logging.getLogger(__name__)


class SignOcaRequest(models.Model):
    _inherit = "sign.oca.request"

    def _pdf_metadata(self, final=False):
        """Metadatos que se escriben en el PDF. Sin números de identificación
        ni correos: el PDF circula fuera de Odoo."""
        self.ensure_one()
        record = self.sudo()
        sender = record.user_id or record.create_uid
        metadata = {
            "/Title": record.name or "",
            "/Author": record.company_id.name or "",
            "/Subject": "Documento para firma electrónica",
            "/Creator": "Odoo - Firmas",
            "/Producer": "Odoo - Firmas",
            "/Keywords": "firma electrónica; %s" % (record.name or ""),
            "/SignOcaReference": "%s-%s" % (record.id, record.version),
            "/SignOcaSentBy": sender.name or "",
            "/SignOcaConfidential": "Sí" if record.is_confidential else "No",
        }
        if final:
            signers = []
            for signer in record.signer_ids.filtered("signed_on").sorted("signed_on"):
                line = "%s (%s, %s UTC)" % (
                    signer.partner_id.name,
                    signer.role_id.name,
                    fields.Datetime.to_string(signer.signed_on),
                )
                if signer.signed_by_user_id:
                    line += " firmado por %s con firma autorizada" % (
                        signer.signed_by_user_id.name
                    )
                signers.append(line)
            metadata.update(
                {
                    "/Subject": "Documento firmado electrónicamente por todas las partes",
                    "/SignOcaSigners": "; ".join(signers),
                    "/SignOcaSubscriptionDate": fields.Date.to_string(
                        record.subscription_date
                    )
                    or "",
                }
            )
        return metadata

    def _write_pdf_metadata(self, final=False):
        """Reescribe el PDF con solo sus páginas y los metadatos propios."""
        self.ensure_one()
        record = self.sudo()
        try:
            reader = PdfFileReader(BytesIO(b64decode(record.data)), strict=False)
            if reader.isEncrypted:
                return
            writer = PdfFileWriter()
            # copiar solo las páginas descarta el catálogo de origen (XMP,
            # JavaScript, adjuntos incrustados) y su diccionario de información
            for page in reader.pages:
                writer.addPage(page)
            writer.addMetadata(self._pdf_metadata(final=final))
            buf = BytesIO()
            writer.write(buf)
        except Exception:
            # los metadatos no deben frenar el envío ni la firma
            _logger.exception("No se pudieron escribir los metadatos de %s", record.name)
            return
        pdf = buf.getvalue()
        record.write({"data": b64encode(pdf), "current_hash": hashlib.sha1(pdf).hexdigest()})

    def action_send(self, sign_now=False, message=""):
        for record in self.filtered(lambda r: r.state == "1_draft"):
            record._write_pdf_metadata()
        return super().action_send(sign_now=sign_now, message=message)

    def _post_signed_certificate(self):
        # antes de generar la copia protegida y el certificado, para que
        # ambos salgan del PDF con los metadatos finales
        self._write_pdf_metadata(final=True)
        return super()._post_signed_certificate()

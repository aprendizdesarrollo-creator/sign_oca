# Copyright 2026 SDT Ingeniería
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import logging
from io import BytesIO

from PIL import Image
from PyPDF2 import PdfFileReader

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

# Marcas invisibles que el reporte pone en las esquinas de cada recuadro de
# firma. Al leer el PDF generado se ubican y de ahí salen las coordenadas de
# los campos de firma, así un cambio de diseño o un texto largo no desplaza
# las firmas fuera de su recuadro.
SIGN_BOX_MARKER = "SDTSIGN"
DISCLOSING_BOX = "A"
RECEIVING_BOX = "B"
DATE_BOX = "C"

DEFAULT_MESSAGE = (
    "<p>Cordial saludo,</p>"
    "<p>Adjuntamos el Acuerdo de Confidencialidad (NDA) para su firma. "
    "La firma de la empresa ya está incluida; solo falta la suya.</p>"
)


class SignOcaMassSend(models.Model):
    """Generación y envío masivo de documentos a firmar.

    Una persona autorizada (por ejemplo la jefa de Talento Humano) traza su
    firma una sola vez en el asistente. Por cada destinatario se genera su
    documento en el formato FTGT38, se estampa la firma pre-aprobada y se le
    envía para que firme. El trabajo pesado (PDF y correos) lo hace un cron en
    lotes, con la cola de correo nativa de Odoo.
    """

    _name = "sign.oca.mass.send"
    _inherit = ["mail.thread"]
    _description = "Envío masivo de documentos a firmar"
    _order = "id desc"

    name = fields.Char(
        string="Referencia", default="Nuevo", readonly=True, copy=False, tracking=True
    )
    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("processing", "Generando y enviando"),
            ("done", "Terminado"),
        ],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
        tracking=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    document_type = fields.Selection(
        [("nda", "Acuerdo de Confidencialidad (FTGT38)")],
        string="Documento",
        default="nda",
        required=True,
    )
    recipient_type = fields.Selection(
        [("employee", "Empleados"), ("supplier", "Proveedores")],
        string="Enviar a",
        default="employee",
        required=True,
    )
    department_ids = fields.Many2many(
        "hr.department",
        string="Departamentos",
        help="Ayuda para seleccionar: agregue a la lista los empleados activos "
        "de estos departamentos.",
    )
    employee_ids = fields.Many2many("hr.employee", string="Empleados")
    supplier_ids = fields.Many2many(
        "res.partner",
        "sign_oca_mass_send_supplier_rel",
        string="Proveedores",
        domain="[('supplier_rank', '>', 0)]",
    )
    skip_existing = fields.Boolean(
        string="Omitir a quien ya lo tiene",
        default=True,
        help="No se genera el documento a quien ya tenga uno de este tipo "
        "enviado o firmado.",
    )
    # --- Firma pre-aprobada ------------------------------------------
    signer_user_id = fields.Many2one(
        "res.users",
        string="Firma por la empresa",
        required=True,
        default=lambda self: self.env.user,
        domain="[('share', '=', False)]",
        help="Persona que firma como Parte Reveladora. Su firma se estampa en "
        "todos los documentos de este envío.",
    )
    signer_job_title = fields.Char(string="Cargo de quien firma")
    signature = fields.Binary(
        string="Firma", attachment=True, copy=False
    )
    signer_is_current_user = fields.Boolean(compute="_compute_signer_is_current_user")
    save_signature = fields.Boolean(
        string="Guardar en mi perfil",
        help="Guarda la firma para reutilizarla en próximos documentos. Solo "
        "aplica si quien firma es el usuario actual.",
    )
    signature_date = fields.Datetime(
        string="Firma aprobada el", readonly=True, copy=False
    )
    signature_ip = fields.Char(string="IP de aprobación", readonly=True, copy=False)
    signature_approved_by_id = fields.Many2one(
        "res.users", string="Aprobado desde la sesión de", readonly=True, copy=False
    )
    # --- Contenido del acuerdo (FTGT38) -------------------------------
    disclosing_party = fields.Char(
        string="Parte Reveladora", default="SDT Ingeniería S.A.S.", required=True
    )
    info_classification = fields.Selection(
        [
            ("confidential_restricted", "Confidencial / Restringida"),
            ("confidential", "Confidencial"),
            ("restricted", "Restringido"),
            ("internal", "Uso Interno"),
            ("public", "Público"),
        ],
        string="Información Clasificada Objeto de Protección",
        default="confidential_restricted",
        required=True,
    )
    validity = fields.Char(
        string="Vigencia del Acuerdo",
        default="Mientras dure la relación contractual + 5 años posteriores a "
        "la finalización",
        required=True,
    )
    # Opciones de la hoja "Listas" del Excel FTGT38. Los campos de texto
    # anteriores (exceptions, consequences) quedan solo para los envíos viejos.
    exception_public = fields.Boolean(string="Información pública", default=True)
    exception_known = fields.Boolean(string="Ya conocida", default=True)
    exception_court = fields.Boolean(string="Orden judicial", default=True)
    consequence_type = fields.Selection(
        [
            ("disciplinary", "Proceso Disciplinario SDT Ingenieria"),
            ("legal", "Acciones civiles, penales y administrativas a las que haya lugar"),
        ],
        string="Consecuencias del Incumplimiento",
        default="disciplinary",
        required=True,
    )
    exceptions = fields.Char(string="Excepciones (texto anterior)")
    consequences = fields.Char(string="Consecuencias (texto anterior)")
    applicable_law = fields.Char(
        string="Ley Aplicable y Jurisdicción",
        default="Leyes de la República de Colombia",
        required=True,
    )
    message = fields.Html(string="Mensaje del correo", default=DEFAULT_MESSAGE)
    # --- Resultado ---------------------------------------------------
    line_ids = fields.One2many(
        "sign.oca.mass.send.line", "mass_send_id", string="Destinatarios"
    )
    request_ids = fields.One2many(
        "sign.oca.request", "mass_send_id", string="Solicitudes"
    )
    line_count = fields.Integer(compute="_compute_counts")
    pending_count = fields.Integer(compute="_compute_counts")
    sent_count = fields.Integer(compute="_compute_counts")
    skipped_count = fields.Integer(compute="_compute_counts")
    error_count = fields.Integer(compute="_compute_counts")
    signed_count = fields.Integer(compute="_compute_counts")

    @api.depends("line_ids.state", "request_ids.state")
    def _compute_counts(self):
        for record in self:
            states = record.line_ids.mapped("state")
            record.line_count = len(states)
            record.pending_count = states.count("pending")
            record.sent_count = states.count("sent")
            record.skipped_count = states.count("skipped")
            record.error_count = states.count("error")
            record.signed_count = len(
                record.request_ids.filtered(lambda r: r.state == "2_signed")
            )

    @api.depends("signer_user_id")
    @api.depends_context("uid")
    def _compute_signer_is_current_user(self):
        for record in self:
            record.signer_is_current_user = record.signer_user_id == self.env.user

    @api.onchange("signer_user_id")
    def _onchange_signer_user_id(self):
        user = self.signer_user_id
        employee = user.employee_ids[:1]
        self.signer_job_title = employee.job_title or employee.job_id.name or False
        # La firma guardada solo se ofrece a su dueña: con otra persona
        # seleccionada se podría firmar en su nombre sin que lo sepa.
        if user == self.env.user and user.sign_oca_signature:
            self.signature = user.sign_oca_signature
        else:
            self.signature = False

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

    @api.onchange("recipient_type")
    def _onchange_recipient_type(self):
        self.consequence_type = (
            "legal" if self.recipient_type == "supplier" else "disciplinary"
        )

    # ------------------------------------------------------------------
    # Destinatarios
    # ------------------------------------------------------------------
    def _get_recipient_values(self):
        """Datos de cada destinatario seleccionado, con el motivo si se omite."""
        self.ensure_one()
        if self.recipient_type == "employee":
            return [self._get_employee_values(e) for e in self.employee_ids]
        return [self._get_supplier_values(p) for p in self.supplier_ids]

    def _get_employee_values(self, employee):
        # identificación y contrato son campos de RR. HH.: quien hace el envío
        # puede no tener ese permiso, y los datos van en el propio acuerdo
        employee = employee.sudo()
        partner = employee.work_contact_id
        job = employee.job_title or employee.job_id.name or ""
        quality = "Colaborador"
        contract = employee.contract_id if "contract_id" in employee._fields else False
        contract_type = (contract and contract.contract_type_id.name or "").lower()
        if employee.employee_type in ("contractor", "freelance") or (
            "prestaci" in contract_type
        ):
            quality = "Contratista"
        skip_reason = False
        if not partner:
            skip_reason = self.env._("El empleado no tiene contacto de trabajo.")
        elif not (partner.email or "").strip():
            skip_reason = self.env._("Sin correo electrónico.")
        elif not employee.identification_id:
            skip_reason = self.env._("Sin número de identificación.")
        return {
            "employee_id": employee.id,
            "partner_id": partner.id,
            "recipient_name": employee.name,
            "identification": employee.identification_id or "",
            "job_title": job,
            "quality": quality,
            "skip_reason": skip_reason,
        }

    def _get_supplier_values(self, partner):
        skip_reason = False
        if not (partner.email or "").strip():
            skip_reason = self.env._("Sin correo electrónico.")
        elif not partner.vat:
            skip_reason = self.env._("Sin NIT / identificación.")
        return {
            "partner_id": partner.id,
            "recipient_name": partner.name,
            "identification": partner.vat or "",
            "job_title": "",
            "quality": "Proveedor",
            "skip_reason": skip_reason,
        }

    def _get_existing_partners(self, partners):
        """Destinatarios que ya tienen un documento de este tipo vigente."""
        signers = self.env["sign.oca.request.signer"].sudo().search(
            [
                ("partner_id", "in", partners.ids),
                ("role_id", "=", self.env.ref("sign_oca.sign_role_nda_receiving").id),
                ("request_id.mass_send_id.document_type", "=", self.document_type),
                ("request_id.state", "in", ("0_sent", "2_signed")),
            ]
        )
        return signers.partner_id

    def _build_lines(self):
        self.ensure_one()
        self.line_ids.unlink()
        values = self._get_recipient_values()
        if not values:
            raise UserError(self.env._("Seleccione al menos un destinatario."))
        partners = self.env["res.partner"].browse(
            [v["partner_id"] for v in values if v["partner_id"]]
        )
        existing = (
            self._get_existing_partners(partners) if self.skip_existing else partners.browse()
        )
        signer_partner = self.signer_user_id.partner_id
        seen = set()
        lines = []
        for vals in values:
            reason = vals.pop("skip_reason")
            partner_id = vals["partner_id"]
            if not reason and partner_id == signer_partner.id:
                reason = self.env._("Es quien firma por la empresa.")
            if not reason and partner_id in existing.ids:
                reason = self.env._("Ya tiene este documento enviado o firmado.")
            if not reason and partner_id in seen:
                reason = self.env._("Destinatario repetido.")
            seen.add(partner_id)
            lines.append(
                dict(
                    vals,
                    mass_send_id=self.id,
                    state="skipped" if reason else "pending",
                    note=reason or False,
                )
            )
        return self.env["sign.oca.mass.send.line"].create(lines)

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def action_preview(self):
        """Muestra el documento del primer destinatario, sin enviar nada."""
        self.ensure_one()
        if self.state != "draft":
            raise UserError(self.env._("El envío ya se procesó."))
        lines = self._build_lines()
        line = lines.filtered(lambda l: l.state == "pending")[:1] or lines[:1]
        return self.env.ref("sign_oca.action_report_nda_ftgt38").report_action(line)

    def _exceptions_text(self):
        """Excepciones marcadas, redactadas como en el formato: "Información
        pública, ya conocida u orden judicial"."""
        self.ensure_one()
        partes = [
            texto
            for marcada, texto in (
                (self.exception_public, "Información pública"),
                (self.exception_known, "ya conocida"),
                (self.exception_court, "orden judicial"),
            )
            if marcada
        ]
        if not partes:
            return ""
        if len(partes) == 1:
            texto = partes[0]
        else:
            # "u" delante de palabra que empieza por o: "... u orden judicial"
            conjuncion = "u" if partes[-1][0] in "oO" else "o"
            texto = "%s %s %s" % (", ".join(partes[:-1]), conjuncion, partes[-1])
        return texto[0].upper() + texto[1:]

    def _check_ready(self):
        self.ensure_one()
        if self.state != "draft":
            raise UserError(self.env._("El envío ya se procesó."))
        if not (self.exception_public or self.exception_known or self.exception_court):
            raise UserError(self.env._("Marque al menos una excepción a la confidencialidad."))
        if self.recipient_type == "supplier":
            # Las declaraciones del FTGT38 son para colaboradores (hablan de
            # relación laboral). Un proveedor necesita el texto que dé Legal.
            raise UserError(
                self.env._(
                    "El envío a proveedores está suspendido: el formato FTGT38 "
                    "solo trae las declaraciones para colaboradores. Falta el "
                    "texto aprobado por Legal para proveedores."
                )
            )
        if self.signer_user_id != self.env.user:
            # La firma pre-aprobada la autoriza su dueña desde su propia
            # sesión: si no, cualquiera podría trazar la firma de otra persona
            # y estamparla en todos los documentos.
            raise UserError(
                self.env._(
                    "Solo %(signer)s puede aprobar su firma para este envío. "
                    "Pídale que ingrese con su usuario, trace su firma y genere "
                    "los documentos.",
                    signer=self.signer_user_id.name,
                )
            )
        if not self.signature:
            raise UserError(
                self.env._(
                    "Falta la firma de %s. Trácela en el recuadro antes de "
                    "generar los documentos."
                )
                % self.signer_user_id.name
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
        """Valida, registra la aprobación de la firma y encola el trabajo."""
        self.ensure_one()
        self._check_ready()
        lines = self._build_lines()
        if not lines.filtered(lambda l: l.state == "pending"):
            raise UserError(
                self.env._(
                    "Ningún destinatario puede recibir el documento. Revise los "
                    "motivos en la pestaña Destinatarios."
                )
            )
        ip = (
            request.httprequest.remote_addr
            if request and getattr(request, "httprequest", None)
            else False
        )
        self.write(
            {
                "name": self.env["ir.sequence"].next_by_code("sign.oca.mass.send")
                or self.name,
                "state": "processing",
                "signature_date": fields.Datetime.now(),
                "signature_ip": ip,
                "signature_approved_by_id": self.env.user.id,
            }
        )
        if self.save_signature and self.signer_user_id == self.env.user:
            self.env.user.sudo().sign_oca_signature = self.signature
        self.message_post(
            body=self.env._(
                "Firma de %(signer)s aprobada para %(count)s documentos. "
                "Aprobada desde la sesión de %(user)s (IP %(ip)s).",
                signer=self.signer_user_id.name,
                count=len(lines.filtered(lambda l: l.state == "pending")),
                user=self.env.user.name,
                ip=ip or "-",
            )
        )
        if self.signer_user_id != self.env.user:
            # quien firma debe enterarse de que su firma se usó
            self.message_notify(
                partner_ids=self.signer_user_id.partner_id.ids,
                subject=self.env._("Su firma se utilizó en el envío %s", self.name),
                body=self.env._(
                    "%(user)s generó %(count)s documentos con su firma como "
                    "Parte Reveladora.",
                    user=self.env.user.name,
                    count=len(lines.filtered(lambda l: l.state == "pending")),
                ),
            )
        self.env.ref("sign_oca.ir_cron_sign_oca_mass_send")._trigger()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_retry_errors(self):
        self.ensure_one()
        self.line_ids.filtered(lambda l: l.state == "error").write(
            {"state": "pending", "note": False}
        )
        self.state = "processing"
        self.env.ref("sign_oca.ir_cron_sign_oca_mass_send")._trigger()

    def action_view_requests(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sign_oca.sign_oca_request_act_window"
        )
        action["domain"] = [("mass_send_id", "=", self.id)]
        action["context"] = {"create": False}
        return action

    def _get_signature_for_box(self, box):
        """Firma como data URL, con el lienzo ajustado a la proporción del
        recuadro: sign_oca estira la imagen al tamaño del campo y sin esto la
        firma saldría deformada."""
        self.ensure_one()
        image = Image.open(BytesIO(base64.b64decode(self.signature))).convert("RGBA")
        bbox = image.getbbox()
        if bbox:
            image = image.crop(bbox)
        box_ratio = box["width_pt"] / box["height_pt"]
        width, height = image.size
        if width / height > box_ratio:
            new_size = (width, round(width / box_ratio))
        else:
            new_size = (round(height * box_ratio), height)
        canvas = Image.new("RGBA", new_size, (255, 255, 255, 0))
        canvas.paste(
            image, ((new_size[0] - width) // 2, (new_size[1] - height) // 2)
        )
        buf = BytesIO()
        canvas.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    # ------------------------------------------------------------------
    # Proceso en segundo plano
    # ------------------------------------------------------------------
    @api.model
    def _cron_process(self, batch_size=20):
        """Genera y envía los documentos pendientes en lotes.

        Cada documento va en su propia transacción: si uno falla queda en
        error y el resto sigue. Usa la API de progreso de ir.cron para que
        Odoo vuelva a ejecutar el cron mientras queden pendientes, y deja los
        correos en la cola nativa (mail.mail), que se dispara al final de
        cada lote.
        """
        Line = self.env["sign.oca.mass.send.line"]
        domain = [("state", "=", "pending"), ("mass_send_id.state", "=", "processing")]
        lines = Line.search(domain, limit=batch_size, order="id")
        done = 0
        for line in lines:
            try:
                with self.env.cr.savepoint():
                    line.with_user(line.mass_send_id.create_uid)._process()
            except Exception as error:  # noqa: BLE001
                _logger.exception("Envío masivo: no se pudo procesar la línea %s", line.id)
                line.write({"state": "error", "note": str(error)[:500]})
            self.env.cr.commit()
            done += 1
        if lines:
            self.env.ref("mail.ir_cron_mail_scheduler_action")._trigger()
        finished = self.search([("state", "=", "processing")]).filtered(
            lambda m: not m.line_ids.filtered(lambda l: l.state == "pending")
        )
        for mass in finished:
            mass.state = "done"
            mass.message_post(
                body=self.env._(
                    "Envío terminado: %(sent)s enviados, %(skipped)s omitidos, "
                    "%(error)s con error.",
                    sent=mass.sent_count,
                    skipped=mass.skipped_count,
                    error=mass.error_count,
                ),
                partner_ids=mass.create_uid.partner_id.ids,
            )
        self.env.cr.commit()
        remaining = Line.search_count(domain)
        self.env["ir.cron"]._notify_progress(done=done, remaining=remaining)


class SignOcaMassSendLine(models.Model):
    _name = "sign.oca.mass.send.line"
    _description = "Destinatario de un envío masivo"
    _order = "id"

    mass_send_id = fields.Many2one(
        "sign.oca.mass.send", required=True, ondelete="cascade", index=True
    )
    state = fields.Selection(
        [
            ("pending", "Pendiente"),
            ("sent", "Enviado"),
            ("skipped", "Omitido"),
            ("error", "Error"),
        ],
        default="pending",
        required=True,
    )
    note = fields.Char(string="Detalle")
    employee_id = fields.Many2one("hr.employee", string="Empleado")
    partner_id = fields.Many2one("res.partner", string="Contacto")
    recipient_name = fields.Char(string="Parte Receptora")
    identification = fields.Char(string="Identificación")
    job_title = fields.Char(string="Cargo")
    quality = fields.Char(string="Calidad")
    agreement_number = fields.Char(string="N° de Acuerdo", readonly=True)
    agreement_date = fields.Date(
        related="request_id.subscription_date", string="Fecha de Suscripción"
    )
    request_id = fields.Many2one("sign.oca.request", string="Solicitud", readonly=True)
    request_state = fields.Selection(related="request_id.state", string="Estado firma")

    def _get_report_values(self):
        """Valores que pinta el reporte FTGT38 (también en la vista previa)."""
        self.ensure_one()
        mass = self.mass_send_id
        classification = dict(
            mass._fields["info_classification"]._description_selection(self.env)
        )
        consequences = dict(
            mass._fields["consequence_type"]._description_selection(self.env)
        )
        return {
            "exceptions": mass._exceptions_text(),
            "consequences": consequences.get(mass.consequence_type) or mass.consequences,
            "number": self.agreement_number or "NDA-%s-XXXX" % fields.Date.today().year,
            "date": self.agreement_date,
            "disclosing_party": mass.disclosing_party,
            "classification": classification.get(mass.info_classification),
            "signer_name": mass.signer_user_id.name,
            "signer_job": mass.signer_job_title or "",
        }

    def _render_pdf(self):
        self.ensure_one()
        pdf, _type = self.env["ir.actions.report"]._render_qweb_pdf(
            "sign_oca.action_report_nda_ftgt38", res_ids=self.ids
        )
        return pdf

    @api.model
    def _find_signature_boxes(self, pdf_bytes):
        """Ubica los recuadros de firma a partir de las marcas del reporte.

        Devuelve {'A': {...}, 'B': {...}} con página y posición en porcentaje,
        que es como sign_oca guarda los campos.
        """
        reader = PdfFileReader(BytesIO(pdf_bytes), strict=False)
        points = {}
        for page_number, page in enumerate(reader.pages, start=1):
            box = page.mediabox
            page_width = float(box.width)
            page_height = float(box.height)

            def visitor(text, cm, tm, _font, font_size, page_number=page_number,
                        page_width=page_width, page_height=page_height):
                text = (text or "").strip()
                if not text.startswith(SIGN_BOX_MARKER):
                    return
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                points[text[len(SIGN_BOX_MARKER):]] = (
                    page_number, x, y, page_width, page_height
                )

            page.extract_text(visitor_text=visitor)
        boxes = {}
        for key in (DISCLOSING_BOX, RECEIVING_BOX, DATE_BOX):
            start, end = points.get(key + "1"), points.get(key + "2")
            if key == DATE_BOX and not (start and end):
                # la fecha es opcional: sin su marca simplemente no se escribe
                continue
            if not start or not end or start[0] != end[0]:
                raise UserError(
                    self.env._(
                        "No se encontró el recuadro de firma %s en el documento "
                        "generado. Revise el formato del reporte.",
                        key,
                    )
                )
            page_number, x1, y1, page_width, page_height = start
            _page, x2, y2, _w, _h = end
            # PyPDF2 da la posición al final del texto de cada marca; con letra
            # de 1px el desfase es de unos 3pt y es igual en las dos esquinas,
            # así que el ancho no cambia. La línea base de la marca superior
            # queda apenas por debajo del borde del recuadro.
            top = y1 + 1
            width_pt = x2 - x1
            height_pt = top - y2
            boxes[key] = {
                "page": page_number,
                "position_x": x1 / page_width * 100,
                "position_y": (page_height - top) / page_height * 100,
                "width": width_pt / page_width * 100,
                "height": height_pt / page_height * 100,
                "width_pt": width_pt,
                "height_pt": height_pt,
            }
        return boxes

    def _prepare_request_vals(self, pdf_bytes, boxes):
        self.ensure_one()
        mass = self.mass_send_id
        field = self.env.ref("sign_oca.sign_field_signature")
        roles = {
            DISCLOSING_BOX: self.env.ref("sign_oca.sign_role_nda_disclosing"),
            RECEIVING_BOX: self.env.ref("sign_oca.sign_role_nda_receiving"),
        }
        signatory_data = {}
        for item_id, key in enumerate((DISCLOSING_BOX, RECEIVING_BOX), start=1):
            box = boxes[key]
            signatory_data[item_id] = {
                "id": item_id,
                "field_id": field.id,
                "field_type": "signature",
                "required": True,
                "name": roles[key].name,
                "role_id": roles[key].id,
                "page": box["page"],
                "position_x": box["position_x"],
                "position_y": box["position_y"],
                "width": box["width"],
                "height": box["height"],
                "value": False,
                "default_value": False,
                "placeholder": "",
                "tabindex": item_id,
            }
        name = "%s - %s" % (self.agreement_number, self.recipient_name)
        return {
            "name": name,
            "filename": "%s.pdf" % name,
            "data": base64.b64encode(pdf_bytes),
            "signatory_data": signatory_data,
            "company_id": mass.company_id.id,
            "user_id": mass.create_uid.id,
            "mass_send_id": mass.id,
            "subscription_date_box": boxes.get(DATE_BOX)
            and {
                k: boxes[DATE_BOX][k]
                for k in ("page", "position_x", "position_y", "width", "height")
            },
            "signer_ids": [
                (
                    0,
                    0,
                    {
                        "partner_id": mass.signer_user_id.partner_id.id,
                        "role_id": roles[DISCLOSING_BOX].id,
                    },
                ),
                (
                    0,
                    0,
                    {
                        "partner_id": self.partner_id.id,
                        "role_id": roles[RECEIVING_BOX].id,
                    },
                ),
            ],
        }

    def _process(self):
        """Genera el documento del destinatario, lo firma por la empresa y lo
        envía."""
        self.ensure_one()
        mass = self.mass_send_id
        self.write(
            {
                "agreement_number": self.env["ir.sequence"].next_by_code(
                    "sign.oca.nda"
                ),
            }
        )
        pdf_bytes = self._render_pdf()
        boxes = self._find_signature_boxes(pdf_bytes)
        sign_request = self.env["sign.oca.request"].create(
            self._prepare_request_vals(pdf_bytes, boxes)
        )
        disclosing = sign_request.signer_ids.filtered(
            lambda s: s.role_id == self.env.ref("sign_oca.sign_role_nda_disclosing")
        )
        disclosing._apply_preapproved_signature(
            mass._get_signature_for_box(boxes[DISCLOSING_BOX]),
            signed_on=mass.signature_date,
            ip=mass.signature_ip,
        )
        sign_request.message_post(
            body=self.env._(
                "Firma de %(signer)s como Parte Reveladora pre-aprobada en el "
                "envío masivo %(mass)s el %(date)s (aprobada desde la sesión de "
                "%(user)s, IP %(ip)s).",
                signer=mass.signer_user_id.name,
                mass=mass.name,
                date=fields.Datetime.to_string(mass.signature_date),
                user=mass.signature_approved_by_id.name,
                ip=mass.signature_ip or "-",
            )
        )
        # correo a la cola (mail.mail) en vez de enviarlo dentro del lote
        sign_request.with_context(mail_notify_force_send=False).action_send(
            message=mass.message
        )
        self.write({"request_id": sign_request.id, "state": "sent", "note": False})

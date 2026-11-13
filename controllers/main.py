import time
from collections import OrderedDict
from urllib import parse

from odoo import _, fields, http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request
from odoo.osv import expression

from odoo.addons.base.models.assetsbundle import AssetsBundle
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.portal import pager as portal_pager

from ..models.sign_oca_confidential import UNLOCK_MINUTES

UNLOCK_SESSION_KEY = "sign_oca_unlocked"


class SignController(http.Controller):
    @http.route("/sign_oca/get_assets.<any(css,js):ext>", type="http", auth="public")
    def get_sign_resources(self, ext):
        bundle = "sign_oca.sign_assets"
        files, _ = request.env["ir.qweb"]._get_asset_content(bundle)
        asset = AssetsBundle(bundle, files)
        mock_attachment = getattr(asset, ext)()
        if isinstance(
            mock_attachment, list
        ):  # suppose that CSS asset will not required to be split in pages
            mock_attachment = mock_attachment[0]
        stream = request.env["ir.binary"]._get_stream_from(mock_attachment)
        response = stream.get_response()
        return response


class PortalSign(CustomerPortal):
    def _signer_check_access(self, signer_id, access_token, check_unlock=True):
        """Check the token and prevent a logged-in user signing for someone else.

        Si el documento pide clave o código, `check_unlock` exige además que
        se hayan validado en esta sesión del navegador.
        """
        signer_sudo = self._document_check_access(
            "sign.oca.request.signer", signer_id, access_token
        )
        if (
            not request.env.user._is_public()
            and signer_sudo.partner_id.id != request.env.user.partner_id.id
            and not signer_sudo._get_on_behalf_signature()
        ):
            raise AccessError(_("Este enlace de firma pertenece a otro firmante."))
        if check_unlock and not self._signer_is_unlocked(signer_sudo):
            raise AccessError(_("Primero ingrese la clave del documento."))
        return signer_sudo

    # ------------------------------------------------------------------
    # Clave y código de verificación (documentos confidenciales)
    # ------------------------------------------------------------------
    def _signer_unlock_steps(self, signer_sudo):
        """Pasos validados y aún vigentes para el firmante en esta sesión."""
        entry = request.session.get(UNLOCK_SESSION_KEY, {}).get(str(signer_sudo.id), {})
        now = time.time()
        return {step for step, expiry in entry.items() if expiry > now}

    def _signer_is_unlocked(self, signer_sudo):
        sign_request = signer_sudo.request_id
        if not sign_request._needs_portal_unlock():
            return True
        # el custodio de una firma autorizada ya está autenticado y habilitado;
        # no tiene por qué conocer el número de documento del dueño
        if signer_sudo._get_on_behalf_signature():
            return True
        steps = self._signer_unlock_steps(signer_sudo)
        if sign_request.access_protection != "none" and "key" not in steps:
            return False
        return not sign_request.require_otp or "otp" in steps

    def _mark_signer_step(self, signer_sudo, step):
        data = dict(request.session.get(UNLOCK_SESSION_KEY, {}))
        entry = dict(data.get(str(signer_sudo.id), {}))
        entry[step] = time.time() + UNLOCK_MINUTES * 60
        data[str(signer_sudo.id)] = entry
        # se reasigna para que la sesión se marque como modificada
        request.session[UNLOCK_SESSION_KEY] = data

    def _render_unlock(self, signer_sudo, access_token, error=False, info=False):
        sign_request = signer_sudo.request_id
        steps = self._signer_unlock_steps(signer_sudo)
        needs_key = sign_request.access_protection != "none" and "key" not in steps
        email = signer_sudo.partner_id.email or ""
        user_part, _sep, domain = email.partition("@")
        return request.render(
            "sign_oca.portal_sign_document_unlock",
            {
                "signer": signer_sudo,
                "company": sign_request.company_id,
                "access_token": access_token,
                "step": "key" if needs_key else "otp",
                "protection": sign_request.access_protection,
                "otp_pending": bool(
                    signer_sudo.otp_hash
                    and signer_sudo.otp_expiry
                    and signer_sudo.otp_expiry > fields.Datetime.now()
                ),
                "masked_email": domain and "%s***@%s" % (user_part[:2], domain),
                "locked": signer_sudo._access_locked(),
                "error": error,
                "info": info,
            },
        )

    @http.route(
        ["/sign_oca/unlock/<int:signer_id>/<string:access_token>"],
        type="http",
        auth="public",
        methods=["POST"],
        website=True,
    )
    def sign_oca_unlock(self, signer_id, access_token, step=False, key=False, code=False, **kw):
        try:
            signer_sudo = self._signer_check_access(
                signer_id, access_token, check_unlock=False
            )
        except (AccessError, MissingError):
            return request.redirect("/my")
        sign_request = signer_sudo.request_id
        error = info = False
        if signer_sudo._access_locked():
            error = _(
                "Demasiados intentos fallidos. Espere unos minutos e intente de nuevo."
            )
        elif step == "key":
            if signer_sudo.check_access_key(key):
                self._mark_signer_step(signer_sudo, "key")
                if sign_request.require_otp and signer_sudo.send_access_otp():
                    info = _("Le enviamos un código de verificación a su correo.")
            else:
                error = _("La clave no es correcta.")
        elif step == "send_otp":
            if signer_sudo.send_access_otp():
                info = _("Le enviamos un código de verificación a su correo.")
            else:
                error = _(
                    "No se pudo enviar el código. Si ya lo pidió, espere un "
                    "minuto antes de pedir otro."
                )
        elif step == "otp":
            if signer_sudo.check_access_otp(code):
                self._mark_signer_step(signer_sudo, "otp")
            else:
                error = _("El código no es correcto o ya venció.")
        if self._signer_is_unlocked(signer_sudo):
            signer_sudo._set_action_log("unlock")
            return request.redirect(signer_sudo.access_url)
        if signer_sudo._access_locked() and not error:
            error = _(
                "Demasiados intentos fallidos. Espere unos minutos e intente de nuevo."
            )
        return self._render_unlock(signer_sudo, access_token, error=error, info=info)

    @http.route(
        ["/sign_oca/document/<int:signer_id>/<string:access_token>"],
        type="http",
        auth="public",
        website=True,
    )
    def get_sign_oca_access(self, signer_id, access_token, **kwargs):
        try:
            signer_sudo = self._signer_check_access(
                signer_id, access_token, check_unlock=False
            )
        except (AccessError, MissingError):
            return request.redirect("/my")
        if not self._signer_is_unlocked(signer_sudo):
            return self._render_unlock(signer_sudo, access_token)
        company = signer_sudo.request_id.company_id
        if signer_sudo.signed_on:
            return request.render(
                "sign_oca.portal_sign_document_signed",
                {"signer": signer_sudo, "company": company},
            )
        # Quien rechazó ve la confirmación de su rechazo. Sin esta rama volvía a
        # cargarse el visor de firma y el rechazo parecía no haber ocurrido.
        if signer_sudo.rejected_on:
            return request.render(
                "sign_oca.portal_sign_document_rejected",
                {"signer": signer_sudo, "company": company},
            )
        # El documento ya está cerrado por otra vía: no tiene sentido ofrecer
        # firmarlo, la firma fallaría al enviarse.
        if signer_sudo.request_id.state in ("3_cancel", "4_rejected"):
            return request.render(
                "sign_oca.portal_sign_document_closed",
                {"signer": signer_sudo, "company": company},
            )
        return request.render(
            "sign_oca.portal_sign_document",
            {
                "doc": signer_sudo.request_id,
                "partner": signer_sudo.partner_id,
                "signer": signer_sudo,
                "access_token": access_token,
                "sign_oca_backend_info": {
                    "access_token": access_token,
                    "signer_id": signer_sudo.id,
                    "lang": signer_sudo.partner_id.lang,
                },
            },
        )

    @http.route(
        ["/sign_oca/content/<int:signer_id>/<string:access_token>"],
        type="http",
        auth="public",
        website=True,
    )
    def get_sign_oca_content_access(self, signer_id, access_token):
        try:
            signer_sudo = self._signer_check_access(signer_id, access_token)
        except (AccessError, MissingError):
            return request.redirect("/my")
        return http.Stream.from_binary_field(
            signer_sudo.request_id, "data"
        ).get_response(mimetype="application/pdf")

    @http.route(
        ["/sign_oca/info/<int:signer_id>/<string:access_token>"],
        type="json",
        auth="public",
        website=True,
    )
    def get_sign_oca_info_access(self, signer_id, access_token):
        try:
            signer_sudo = self._signer_check_access(signer_id, access_token)
        except (AccessError, MissingError):
            return request.redirect("/my")
        return signer_sudo.get_info(access_token=access_token)

    @http.route(
        ["/sign_oca/sign/<int:signer_id>/<string:access_token>"],
        type="json",
        auth="public",
        website=True,
    )
    def get_sign_oca_sign_access(
        self, signer_id, access_token, items, latitude=False, longitude=False
    ):
        try:
            signer_sudo = self._signer_check_access(signer_id, access_token)
        except (AccessError, MissingError):
            return request.redirect("/my")
        return signer_sudo.action_sign(
            items, access_token=access_token, latitude=latitude, longitude=longitude
        )

    @http.route(
        ["/sign_oca/save_signature/<int:signer_id>/<string:access_token>"],
        type="json",
        auth="public",
        website=True,
    )
    def sign_oca_save_signature(self, signer_id, access_token, signature=False):
        """Guarda la firma trazada para reutilizarla en próximos documentos."""
        try:
            signer_sudo = self._signer_check_access(signer_id, access_token)
        except (AccessError, MissingError):
            return False
        return signer_sudo.save_signature(signature, access_token=access_token)

    @http.route(
        ["/sign_oca/reject/<int:signer_id>/<string:access_token>"],
        type="json",
        auth="public",
        website=True,
    )
    def get_sign_oca_reject_access(
        self, signer_id, access_token, reason=False, reason_id=False
    ):
        """El firmante rechaza el documento desde el portal."""
        try:
            signer_sudo = self._signer_check_access(signer_id, access_token)
        except (AccessError, MissingError):
            return request.redirect("/my")
        return signer_sudo.action_reject(
            reason=reason, access_token=access_token, reason_id=reason_id
        )

    def get_sign_requests_domain(self, request):
        user = request.env.user
        domain = [
            ("request_id.state", "in", ("0_sent", "2_signed")),
            ("partner_id", "=", user.partner_id.id),
        ]
        if user._is_public():
            return domain
        # y los pendientes de las personas cuyas firmas autorizadas custodia
        owners = (
            request.env["sign.oca.authorized.signature"]
            ._get_available(user)
            .mapped("signer_user_id.partner_id")
        ) - user.partner_id
        if not owners:
            return domain
        return expression.OR(
            [
                domain,
                [
                    ("request_id.state", "=", "0_sent"),
                    ("partner_id", "in", owners.ids),
                    ("signed_on", "=", False),
                    ("rejected_on", "=", False),
                ],
            ]
        )

    def _get_my_sign_requests_searchbar_filters(self):
        searchbar_filters = {
            "all": {"label": _("All"), "domain": []},
            "sent": {
                "label": _("sent"),
                "domain": [("request_id.state", "=", "0_sent")],
            },
            "signed": {
                "label": _("Signed"),
                "domain": [("request_id.state", "=", "2_signed")],
            },
        }
        return searchbar_filters

    def _prepare_sign_portal_rendering_values(self, page=1, sign_page=False, **kwargs):
        # Sorting feature
        searchbar_sortings = {
            "state": {"label": _("Sent to Signed"), "order": "request_id"},
            "reverse_state": {"label": _("Signed to Sent"), "order": "request_id desc"},
            "date": {"label": _("Newest"), "order": "create_date desc"},
            "reverse_date": {"label": _("Oldest"), "order": "create_date"},
        }
        sortby = kwargs.get("sortby", "state")
        order = searchbar_sortings[sortby]["order"]
        # Filtering feature
        searchbar_filters = self._get_my_sign_requests_searchbar_filters()
        filterby = kwargs.get("filterby") or "all"
        domain = searchbar_filters.get(filterby, searchbar_filters["all"])["domain"]
        domain = expression.AND([domain, self.get_sign_requests_domain(request)])
        SignRequests = request.env["sign.oca.request.signer"].sudo()
        pager_values = portal_pager(
            url="/my/sign_requests",
            total=SignRequests.search_count(domain),
            page=page,
            step=self._items_per_page,
            url_args={},
        )
        sign_requests = SignRequests.search(
            domain,
            order=order,
            limit=self._items_per_page,
            offset=pager_values["offset"],
        )
        values = self._prepare_portal_layout_values()
        values.update(
            {
                "sign_requests": sign_requests.sudo() if sign_page else SignRequests,
                "page_name": "My Sign Requests",
                "pager": pager_values,
                "default_url": "/my/sign_requests",
                "searchbar_sortings": searchbar_sortings,
                "searchbar_filters": OrderedDict(sorted(searchbar_filters.items())),
                "sortby": sortby,
                "filterby": filterby,
            }
        )
        return values

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if "sign_oca_count" in counters:
            domain = self.get_sign_requests_domain(request)
            SignRequests = request.env["sign.oca.request.signer"].sudo()
            values["sign_oca_count"] = SignRequests.search_count(domain)
        return values

    @http.route(
        ["/my/sign_requests", "/my/sign_requests/page/<int:page>"],
        type="http",
        auth="public",
        website=True,
    )
    def sign_requests(self, **kwargs):
        values = self._prepare_sign_portal_rendering_values(sign_page=True, **kwargs)
        return request.render(
            "sign_oca.sign_requests",
            values,
        )

    @http.route(
        ["/my/sign/<int:request_id>/download"], type="http", auth="user", website=True
    )
    def portal_download_signed(self, request_id, **kw):
        sign_request = request.env["sign.oca.request"].sudo().browse(request_id)
        if not sign_request.exists():
            return request.not_found()
        current_partner = request.env.user.partner_id
        is_signer = sign_request.signer_ids.filtered(
            lambda signer: signer.partner_id.id == current_partner.id
        )
        if sign_request.user_id.id != request.env.user.id and not is_signer:
            return request.not_found()
        # el responsable descarga sin más; el firmante de un documento con
        # clave debe validarla primero desde su enlace
        if (
            sign_request.user_id.id != request.env.user.id
            and sign_request._needs_portal_unlock()
            and not any(self._signer_is_unlocked(signer) for signer in is_signer)
        ):
            signer = is_signer[:1]
            signer._portal_ensure_token()
            return request.redirect(signer.access_url)
        # Firmado por todos: copia protegida; si no, el documento en curso
        attachment = sign_request._get_download_attachment()
        if not attachment:
            return request.not_found()
        access_token = attachment.generate_access_token()[0]
        ascii_filename = "document.pdf"
        if sign_request.state == "2_signed" and attachment.res_field != "data":
            filename = attachment.name
        else:
            filename = sign_request.name or ascii_filename
        # Ensure .pdf extension
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        utf8_filename = parse.quote(filename)
        params = {
            "download": "true",
            "filename": utf8_filename,
            "access_token": access_token,
        }
        url = f"/web/content/{attachment.id}?{parse.urlencode(params)}"
        return request.redirect(url)

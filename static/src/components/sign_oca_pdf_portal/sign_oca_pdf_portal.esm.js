/** @odoo-module **/
/* global window */

import {App, useRef, whenReady} from "@odoo/owl";
import {_t} from "@web/core/l10n/translation";
import {makeEnv, startServices} from "@web/env";
import SignOcaPdf from "../sign_oca_pdf/sign_oca_pdf.esm.js";
import {getTemplate} from "@web/core/templates";
import {MainComponentsContainer} from "@web/core/main_components_container";
import {rpc} from "@web/core/network/rpc";
import {SignOcaRejectDialog} from "./sign_oca_reject_dialog.esm";
import {startSignItemNavigator} from "./sign_oca_navigator.esm";
import {useService} from "@web/core/utils/hooks";

export class SignOcaPdfPortal extends SignOcaPdf {
    setup() {
        this.rpc = rpc;
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.signOcaFooter = useRef("sign_oca_footer");
        this.signer_id = this.props.signer_id;
        this.access_token = this.props.access_token;
        super.setup(...arguments);
        this.readOnlyMode = false;
    }
    async willStart() {
        this.info = await this.rpc(
            "/sign_oca/info/" + this.signer_id + "/" + this.access_token
        );
    }
    getPdfUrl() {
        return "/sign_oca/content/" + this.signer_id + "/" + this.access_token;
    }
    checkToSign() {
        this.to_sign = this.to_sign_update;
        $(this.signOcaFooter.el).show();
        $("#sign_oca_button")
            .prop("disabled", !this.to_sign_update)
            .attr("aria-disabled", String(!this.to_sign_update));
        $("#sign_oca_required_message").toggle(!this.to_sign_update);
    }
    postIframeFields() {
        super.postIframeFields(...arguments);
        this.checkFilledAll();
        // Is essential to make sure the navigator will never duplicate
        const target = $(
            this.iframe.el.contentDocument.getElementById("viewerContainer")
        );
        const navigator = $(
            this.iframe.el.contentDocument.getElementsByClassName(
                "o_sign_sign_item_navigator"
            )
        );
        const navLine = $(
            this.iframe.el.contentDocument.getElementsByClassName(
                "o_sign_sign_item_navline"
            )
        );
        if (navLine.length === 0) {
            target.append($("<div class='o_sign_sign_item_navline'/>"));
        }
        if (navigator.length === 0) {
            target.append($("<div class='o_sign_sign_item_navigator'/>"));
        }
        // Load navigator
        this.navigate();
        this._ensureReadModeButton();
        this._updateReadOnlyMode();
    }
    _ensureReadModeButton() {
        const contentDocument = this.iframe.el.contentDocument;
        const toolbar = contentDocument.getElementById("toolbarViewerMiddle");
        if (!toolbar || contentDocument.getElementById("signOcaReadMode")) {
            return;
        }
        const button = contentDocument.createElement("button");
        button.id = "signOcaReadMode";
        button.type = "button";
        button.className = "toolbarButton o_sign_oca_read_mode_button";
        button.addEventListener("click", (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            this.toggleReadOnly();
        });
        const label = contentDocument.createElement("span");
        button.appendChild(label);
        const scaleSelect = contentDocument.getElementById("scaleSelectContainer");
        if (scaleSelect && scaleSelect.parentNode === toolbar) {
            toolbar.insertBefore(button, scaleSelect.nextSibling);
        } else {
            toolbar.appendChild(button);
        }
    }
    toggleReadOnly() {
        this.readOnlyMode = !this.readOnlyMode;
        this._updateReadOnlyMode();
    }
    _updateReadOnlyMode() {
        const contentDocument = this.iframe.el.contentDocument;
        if (!contentDocument || !contentDocument.documentElement) {
            return;
        }
        contentDocument.documentElement.classList.toggle(
            "o_sign_oca_readonly",
            this.readOnlyMode
        );
        contentDocument.querySelectorAll(".o_sign_oca_field").forEach((field) => {
            field.hidden = this.readOnlyMode;
        });
        if (this.navigator) {
            this.navigator.toggle(!this.readOnlyMode);
        }
        if (this.signOcaFooter.el) {
            $(this.signOcaFooter.el).toggle(!this.readOnlyMode);
        }
        const button = contentDocument.getElementById("signOcaReadMode");
        if (!button) {
            return;
        }
        const label = this.readOnlyMode ? _t("Volver") : _t("Modo lectura");
        const title = this.readOnlyMode
            ? _t("Volver al modo de firma")
            : _t("Ver el documento sin campos de firma");
        const labelElement = button.querySelector("span");
        if (labelElement) {
            labelElement.textContent = label;
        }
        button.title = title;
        button.setAttribute("aria-label", label);
        button.setAttribute("aria-pressed", String(this.readOnlyMode));
        button.classList.toggle("toggled", this.readOnlyMode);
    }
    async _onClickSign(ev) {
        ev.target.disabled = true;
        const position = await this.getLocation();
        try {
            const action = await this.rpc(
                "/sign_oca/sign/" + this.signer_id + "/" + this.access_token,
                {
                    items: this.info.items,
                    latitude: position && position.coords && position.coords.latitude,
                    longitude: position && position.coords && position.coords.longitude,
                }
            );
            // As we are on frontend env, it is not possible to use do_action(), so we
            // redirect to the corresponding URL or reload the page if the action is not
            // an url.
            if (action && action.type === "ir.actions.act_url") {
                window.location = action.url;
            } else {
                window.location.reload();
            }
        } catch (error) {
            this.notification.add(this._getRpcErrorMessage(error), {
                title: _t("No se pudo enviar el documento"),
                type: "danger",
                sticky: true,
            });
            ev.target.disabled = false;
            this.checkFilledAll();
        }
    }
    /** Guarda la firma trazada en el usuario, para reutilizarla después. */
    async saveSignature(image) {
        try {
            await this.rpc(
                "/sign_oca/save_signature/" + this.signer_id + "/" + this.access_token,
                {signature: image}
            );
            this.info.saved_signature = image;
        } catch {
            // Que no se pueda guardar no debe impedir firmar el documento.
        }
    }
    _onClickReject(ev) {
        ev.preventDefault();
        this.dialog.add(SignOcaRejectDialog, {
            reasons: this.info.reject_reasons || [],
            defaultReasonId: this.info.default_reject_reason_id || false,
            confirm: (reason, reasonId) => this._rejectDocument(reason, reasonId),
        });
    }
    async _rejectDocument(reason, reasonId) {
        $("#sign_oca_reject_button").prop("disabled", true);
        try {
            const action = await this.rpc(
                "/sign_oca/reject/" + this.signer_id + "/" + this.access_token,
                {reason: reason, reason_id: reasonId}
            );
            if (action && action.type === "ir.actions.act_url") {
                window.location = action.url;
            } else {
                window.location.reload();
            }
        } catch (error) {
            this.notification.add(this._getRpcErrorMessage(error), {
                title: _t("No se pudo rechazar el documento"),
                type: "danger",
                sticky: true,
            });
            $("#sign_oca_reject_button").prop("disabled", false);
        }
    }
    _getRpcErrorMessage(error) {
        return (
            (error && error.data && error.data.message) ||
            (error && error.message) ||
            _t("Ocurrió un error. Inténtalo nuevamente.")
        );
    }
    navigate() {
        const target = this.iframe.el.contentDocument.getElementById("viewerContainer");
        this.navigator = startSignItemNavigator(this, target, this.env);
    }
}
SignOcaPdfPortal.template = "sign_oca.SignOcaPdfPortal";
SignOcaPdfPortal.props = {
    access_token: String,
    signer_id: Number,
};
SignOcaPdfPortal.components = {MainComponentsContainer};

export async function initDocumentToSign(document, sign_oca_backend_info) {
    const env = makeEnv();
    await startServices(env);
    await whenReady();
    const app = new App(SignOcaPdfPortal, {
        getTemplate,
        env: env,
        dev: env.debug,
        props: {
            access_token: sign_oca_backend_info.access_token,
            signer_id: sign_oca_backend_info.signer_id,
        },
        translateFn: _t,
        translatableAttributes: ["data-tooltip"],
    });
    await app.mount(document.body);
}
export default {SignOcaPdfPortal, initDocumentToSign};

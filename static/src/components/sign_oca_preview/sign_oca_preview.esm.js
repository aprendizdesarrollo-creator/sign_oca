/** @odoo-module QWeb **/
import {ControlPanel} from "@web/search/control_panel/control_panel";
import SignOcaPdfCommon from "../sign_oca_pdf_common/sign_oca_pdf_common.esm.js";
import {registry} from "@web/core/registry";

export default class SignOcaPreview extends SignOcaPdfCommon {
    setup() {
        this.res_id =
            this.props.action.params.res_id || this.props.action.context.active_id;
        this.model =
            this.props.action.params.res_model ||
            this.props.action.context.active_model;
        super.setup(...arguments);
    }
    postIframeField(item) {
        const signedRoles = (this.info.roles || [])
            .filter((role) => role.signed)
            .map((role) => role.id);
        // Lo de quien ya firmó está impreso en el PDF: pintarlo encima lo
        // duplicaría.
        if (item.value && signedRoles.includes(item.role_id)) {
            const existingItem = this.items[item.id];
            if (existingItem) {
                existingItem.remove();
                delete this.items[item.id];
            }
            return;
        }
        const signatureItem = super.postIframeField(...arguments);
        // Un valor precargado de quien aún no ha firmado todavía no está en el
        // PDF: se muestra dentro del recuadro para que se vea qué saldrá.
        if (item.value && item.field_type === "text" && signatureItem) {
            const el = signatureItem[0];
            el.textContent = item.value;
            el.style.display = "flex";
            el.style.alignItems = "center";
            el.style.paddingLeft = "4px";
            el.style.lineHeight = "1";
            el.style.whiteSpace = "nowrap";
        }
        return signatureItem;
    }
}
SignOcaPreview.template = "sign_oca.SignOcaPreview";
SignOcaPreview.components = {...SignOcaPdfCommon.components, ControlPanel};
SignOcaPreview.props = {
    action: Object,
    "*": {optional: true},
};
registry.category("actions").add("sign_oca_preview", SignOcaPreview);

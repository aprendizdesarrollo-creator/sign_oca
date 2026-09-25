/** @odoo-module Qweb **/
/* global Event */

import {SignOcaSignatureDialog} from "../components/sign_oca_signature_dialog/sign_oca_signature_dialog.esm";
import {registry} from "@web/core/registry";
import {renderToString} from "@web/core/utils/render";

const signatureSignOca = {
    uploadSignature: function (parent, item, signatureItem, data) {
        if (typeof data.signatureImage === "string") {
            item.value = data.signatureImage;
        } else if (Array.isArray(data.signatureImage)) {
            item.value = data.signatureImage[1];
        } else {
            throw new Error(
                "Signature must be an image file or a list of images of this format: 'data:image/png;base64,'"
            );
        }

        if (
            typeof item.value === "string" &&
            !item.value.startsWith("data:image/png;base64,")
        ) {
            item.value = "data:image/png;base64," + item.value;
        }
        parent.postIframeField(item);
        parent.checkFilledAll();
    },
    /**
     * Abre el diálogo de firma de SDT: solo Trazar o Cargar, con la opción de
     * guardar el trazo en el usuario para reutilizarlo.
     */
    openDialog: function (parent, item, signatureItem) {
        parent.dialogService.add(SignOcaSignatureDialog, {
            savedSignature: parent.info.saved_signature || false,
            canSaveSignature: Boolean(parent.info.can_save_signature),
            uploadSignature: (data) =>
                this.uploadSignature(parent, item, signatureItem, data),
            saveSignature: (image) =>
                parent.saveSignature ? parent.saveSignature(image) : Promise.resolve(),
        });
    },
    generate: function (parent, item, signatureItem) {
        var input = $(
            renderToString("sign_oca.sign_iframe_field_signature", {item: item})
        )[0];
        if (item.role_id === parent.info.role_id) {
            signatureItem[0].addEventListener("focus_signature", () =>
                this.openDialog(parent, item, signatureItem)
            );
            input.addEventListener("click", (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                this.openDialog(parent, item, signatureItem);
            });
            input.addEventListener("keydown", (ev) => {
                if ((ev.keyCode || ev.which) !== 9) {
                    return true;
                }
                ev.preventDefault();
                var next_items = Object.values(parent.info.items).filter(
                    (i) =>
                        i.tabindex > item.tabindex && i.role_id === parent.info.role_id
                );
                if (next_items.length > 0) {
                    ev.currentTarget.blur();
                    const nextItem = next_items[0];
                    if (nextItem && parent.items && parent.items[nextItem.id]) {
                        parent.items[nextItem.id].dispatchEvent(
                            new Event("focus_signature")
                        );
                    }
                }
            });
        }
        return input;
    },
    check: function (item) {
        return Boolean(item.value);
    },
};
registry.category("sign_oca").add("signature", signatureSignOca);

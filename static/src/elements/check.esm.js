/** @odoo-module Qweb **/
/* global Event */

import {registry} from "@web/core/registry";
import {renderToString} from "@web/core/utils/render";

const checkSignOca = {
    change: function (value, parent, item) {
        item.value = value;
        parent.checkFilledAll();
    },
    generate: function (parent, item, signatureItem) {
        var input = $(
            renderToString("sign_oca.sign_iframe_field_check", {
                item: item,
                role_id: parent.info.role_id,
            })
        )[0];
        signatureItem[0].addEventListener("focus_signature", () => {
            input.focus();
        });
        input.addEventListener("change", (ev) => {
            this.change(ev.currentTarget.checked, parent, item, signatureItem);
        });
        input.addEventListener("keydown", (ev) => {
            if ((ev.keyCode || ev.which) !== 9) {
                return true;
            }
            ev.preventDefault();
            var next_items = Object.values(parent.info.items)
                .filter(
                    (i) =>
                        i.tabindex > item.tabindex && i.role_id === parent.info.role_id
                )
                .sort((a, b) => a.tabindex - b.tabindex);
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
        return input;
    },
    check: function (item) {
        return item.value === true;
    },
};
registry.category("sign_oca").add("check", checkSignOca);

/** @odoo-module **/

import {Component, useState} from "@odoo/owl";
import {Dialog} from "@web/core/dialog/dialog";

/**
 * Diálogo de rechazo del portal.
 *
 * Ofrece los motivos configurados en Firmas → Configuración. Algunos exigen
 * explicación: en ese caso el botón no se habilita hasta que se escribe.
 */
export class SignOcaRejectDialog extends Component {
    setup() {
        this.state = useState({
            reasonId: this.props.defaultReasonId || false,
            detail: "",
        });
    }

    get reasons() {
        return this.props.reasons || [];
    }

    get selectedReason() {
        return this.reasons.find((r) => r.id === this.state.reasonId);
    }

    get requiresDetail() {
        const reason = this.selectedReason;
        return Boolean(reason && reason.requires_detail);
    }

    /** Sin motivos configurados basta el texto; si hay, se exige elegir uno. */
    get canConfirm() {
        if (this.reasons.length && !this.state.reasonId) {
            return false;
        }
        if (this.requiresDetail && !this.state.detail.trim()) {
            return false;
        }
        if (!this.reasons.length && !this.state.detail.trim()) {
            return false;
        }
        return true;
    }

    selectReason(id) {
        this.state.reasonId = id;
    }

    onConfirm() {
        if (!this.canConfirm) {
            return;
        }
        this.props.confirm(this.state.detail.trim(), this.state.reasonId);
        this.props.close();
    }
}
SignOcaRejectDialog.template = "sign_oca.RejectDialog";
SignOcaRejectDialog.components = {Dialog};
SignOcaRejectDialog.props = {
    close: Function,
    confirm: Function,
    reasons: {type: Array, optional: true},
    defaultReasonId: {type: [Number, Boolean], optional: true},
};
SignOcaRejectDialog.defaultProps = {reasons: [], defaultReasonId: false};

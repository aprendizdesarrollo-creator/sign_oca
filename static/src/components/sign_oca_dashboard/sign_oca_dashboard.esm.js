/** @odoo-module **/

import {Component, onWillStart, useState} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";

/**
 * Tablero del módulo de Firmas: indicadores del estado de los documentos
 * y accesos directos a las listas ya filtradas.
 *
 * Cada KPI es accionable: al pulsarlo abre exactamente el conjunto de
 * registros que está contando, para que el número no sea un callejón sin
 * salida.
 */
export class SignOcaDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({data: null, loading: true});
        onWillStart(() => this.loadData());
    }

    async loadData() {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "sign.oca.request",
            "get_dashboard_data",
            []
        );
        this.state.loading = false;
    }

    get cards() {
        const d = this.state.data;
        if (!d) {
            return [];
        }
        return [
            {
                key: "mine",
                value: d.my_pending_count,
                label: "Mis firmas pendientes",
                sub: "Documentos que esperan por ti",
                color: "mine",
                icon: "fa-pencil-square-o",
                target: {type: "mine"},
            },
            {
                key: "sent",
                value: d.counts.sent,
                label: "En proceso",
                sub: "Esperando firma",
                color: "primary",
                icon: "fa-paper-plane",
                target: {type: "requests", domain: [["state", "=", "0_sent"]]},
            },
            {
                key: "pending_signers",
                value: d.pending_signers,
                label: "Firmantes pendientes",
                sub: "Personas que faltan por firmar",
                color: "warning",
                // Font Awesome 4 (el que trae Odoo 18) no tiene fa-user-clock
                icon: "fa-hourglass-half",
                target: {type: "signers"},
            },
            {
                key: "signed_by_me",
                value: d.my_signed_count,
                label: "Firmados por mí",
                sub: "Documentos que ya firmaste",
                color: "success",
                icon: "fa-check-circle",
                target: {
                    type: "requests",
                    domain: [
                        [
                            "signer_ids",
                            "any",
                            [
                                ["partner_id", "=", d.my_partner_id],
                                ["signed_on", "!=", false],
                            ],
                        ],
                    ],
                },
            },
            {
                key: "rejected",
                value: d.counts.rejected,
                label: "Rechazados",
                sub: "Devueltos por un firmante",
                color: "danger",
                icon: "fa-times-circle",
                target: {type: "requests", domain: [["state", "=", "4_rejected"]]},
            },
            {
                key: "draft",
                value: d.counts.draft,
                label: "Sin enviar",
                sub: "En borrador",
                color: "secondary",
                icon: "fa-file-o",
                target: {type: "requests", domain: [["state", "=", "1_draft"]]},
            },
            {
                key: "rate",
                value: d.rate + "%",
                label: "Tasa de firma",
                sub: "Firmados sobre cerrados",
                color: "info",
                icon: "fa-pie-chart",
                target: {type: "grouped"},
            },
        ];
    }

    /** Abre el detalle que hay detrás de cada indicador. */
    openCard(card) {
        const target = card.target;
        if (!target) {
            return;
        }
        if (target.type === "signers") {
            return this.action.doAction({
                type: "ir.actions.act_window",
                name: "Firmantes pendientes",
                res_model: "sign.oca.request.signer",
                views: [
                    [false, "list"],
                    [false, "form"],
                ],
                domain: [
                    ["signed_on", "=", false],
                    ["rejected_on", "=", false],
                    ["request_id.state", "=", "0_sent"],
                ],
            });
        }
        if (target.type === "mine") {
            return this.action.doAction({
                type: "ir.actions.act_window",
                name: "Mis firmas pendientes",
                res_model: "sign.oca.request.signer",
                views: [
                    [false, "list"],
                    [false, "form"],
                ],
                domain: [
                    ["partner_id", "=", this.state.data.my_partner_id],
                    ["signed_on", "=", false],
                    ["rejected_on", "=", false],
                    ["request_id.state", "=", "0_sent"],
                ],
            });
        }
        if (target.type === "department") {
            return this.action.doAction({
                type: "ir.actions.act_window",
                name: "Firmas de " + target.name,
                res_model: "sign.oca.request.signer",
                views: [
                    [false, "list"],
                    [false, "form"],
                ],
                domain: [
                    ["partner_id", "in", target.partner_ids],
                    ["request_id.state", "in", ["0_sent", "2_signed"]],
                ],
            });
        }
        if (target.type === "grouped") {
            return this.action.doAction({
                type: "ir.actions.act_window",
                name: "Documentos por estado",
                res_model: "sign.oca.request",
                views: [
                    [false, "list"],
                    [false, "form"],
                ],
                context: {group_by: ["state"]},
            });
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: card.label,
            res_model: "sign.oca.request",
            views: [
                [false, "list"],
                [false, "form"],
            ],
            domain: target.domain,
        });
    }

    /** Permite accionar los KPI también con teclado. */
    onCardKeydown(ev, card) {
        if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            this.openCard(card);
        }
    }

    openRequest(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sign.oca.request",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    /** Abre el documento en el portal, listo para firmar. */
    signNow(doc) {
        this.action.doAction({type: "ir.actions.act_url", url: doc.url, target: "self"});
    }

    openDepartment(dep) {
        this.openCard({
            target: {
                type: "department",
                name: dep.name,
                partner_ids: dep.partner_ids,
            },
        });
    }

    newRequest() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Nuevo documento",
            res_model: "sign.oca.request",
            views: [[false, "form"]],
            target: "current",
        });
    }
}
SignOcaDashboard.template = "sign_oca.Dashboard";
SignOcaDashboard.props = {
    "*": true,
};

registry.category("actions").add("sign_oca_dashboard", SignOcaDashboard);

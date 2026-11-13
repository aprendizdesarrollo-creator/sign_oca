/** @odoo-module **/

import {Component, onWillStart, useState} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";

export class SignOcaDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            data: null,
            loading: true,
            refreshing: false,
        });
        onWillStart(() => this.loadData());
    }

    async loadData() {
        const initialLoad = !this.state.data;
        this.state.loading = initialLoad;
        this.state.refreshing = !initialLoad;
        try {
            this.state.data = await this.orm.call(
                "sign.oca.request",
                "get_dashboard_data",
                []
            );
        } finally {
            this.state.loading = false;
            this.state.refreshing = false;
        }
    }

    get cards() {
        const d = this.state.data;
        if (!d) {
            return [];
        }
        const sentDomain = [["state", "=", "0_sent"]];
        if (!d.can_view_all_documents) {
            sentDomain.push(["user_id", "=", d.my_user_id]);
        }
        const pendingSignerDomain = [
            ["signed_on", "=", false],
            ["rejected_on", "=", false],
            ["request_id.state", "=", "0_sent"],
        ];
        if (!d.can_view_all_documents) {
            pendingSignerDomain.push(["request_id.user_id", "=", d.my_user_id]);
        }
        return [
            {
                key: "mine",
                value: d.my_pending_count,
                label: "Pendientes de su firma",
                sub: "Documentos que usted debe firmar",
                color: "mine",
                icon: "fa-pencil-square-o",
                target: {type: "mine"},
            },
            {
                key: "sent",
                value: d.counts.sent,
                label: d.can_view_all_documents
                    ? "Todos los documentos en proceso"
                    : "Mis envíos en proceso",
                sub: d.can_view_all_documents
                    ? "Esperando firmas pendientes"
                    : "Esperando firmas de otras personas",
                color: "primary",
                icon: "fa-paper-plane",
                creatorOnly: true,
                target: {type: "requests", domain: sentDomain},
            },
            {
                key: "pending_signers",
                value: d.pending_signers,
                label: "Firmantes pendientes",
                sub: d.can_view_all_documents
                    ? "En todos los documentos"
                    : "En sus documentos enviados",
                color: "warning",
                icon: "fa-hourglass-half",
                creatorOnly: true,
                target: {type: "signers", domain: pendingSignerDomain},
            },
            {
                key: "my_sent",
                value: d.my_sent_count,
                label: "Mis documentos enviados",
                sub: "Todos sus envíos, pendientes o finalizados",
                color: "info",
                icon: "fa-files-o",
                creatorOnly: true,
                target: {
                    type: "requests",
                    domain: [
                        ["child_request_ids", "=", false],
                        ["user_id", "=", d.my_user_id],
                        [
                            "state",
                            "in",
                            ["0_sent", "2_signed", "3_cancel", "4_rejected"],
                        ],
                    ],
                },
            },
            {
                key: "all_documents",
                value: d.all_documents_count,
                label: "Todos los documentos",
                sub: "En cualquier estado",
                color: "info",
                icon: "fa-folder-open-o",
                adminOnly: true,
                target: {
                    type: "requests",
                    domain: [["child_request_ids", "=", false]],
                },
            },
            {
                key: "signed_by_me",
                value: d.my_signed_count,
                label: "Firmados por usted",
                sub: "Documentos que usted ya firmó",
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
                label: "Documentos rechazados",
                sub: "Solicitudes devueltas por un firmante",
                color: "danger",
                icon: "fa-times-circle",
                creatorOnly: true,
                target: {
                    type: "requests",
                    domain: [["state", "=", "4_rejected"]],
                },
            },
            {
                key: "draft",
                value: d.counts.draft,
                label: "Borradores",
                sub: "Documentos creados que aún no se han enviado",
                color: "secondary",
                icon: "fa-file-o",
                adminOnly: true,
                target: {
                    type: "requests",
                    domain: [["state", "=", "1_draft"]],
                },
            },
        ].filter(
            (card) =>
                (!card.creatorOnly || d.can_create_documents) &&
                (!card.adminOnly || d.can_view_all_documents)
        );
    }

    get pendingTotals() {
        const pending = this.state.data?.pending || [];
        const totals = pending.reduce(
            (result, document) => {
                result.signers += Number(document.signers || 0);
                result.signed += Number(document.signed || 0);
                return result;
            },
            {signers: 0, signed: 0}
        );
        const pendingSigners = Math.max(totals.signers - totals.signed, 0);
        return {
            pendingSigners,
        };
    }

    activityTone(document) {
        const state = (document.state_label || "").toLowerCase();
        if (state.includes("firm")) {
            return "success";
        }
        if (state.includes("rech")) {
            return "danger";
        }
        if (state.includes("borr")) {
            return "secondary";
        }
        return "primary";
    }

    openRequestList(domain, context = {}) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sign.oca.request",
            views: [[false, "list"], [false, "form"]],
            target: "current",
            domain: domain || [],
            context,
        });
    }

    openSignerList(domain, context = {}) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sign.oca.request.signer",
            views: [
                [false, "kanban"],
                [false, "list"],
                [false, "form"],
                [false, "activity"],
            ],
            target: "current",
            domain: domain || [],
            context,
        });
    }

    openCard(card) {
        if (!card || !this.state.data) {
            return;
        }
        const target = card.target;
        if (target.type === "mine") {
            return this.openRequestList([
                ["state", "=", "0_sent"],
                [
                    "signer_ids",
                    "any",
                    [
                        ["partner_id", "=", this.state.data.my_partner_id],
                        ["signed_on", "=", false],
                        ["rejected_on", "=", false],
                    ],
                ],
            ]);
        }
        if (target.type === "signers") {
            return this.openSignerList(target.domain || []);
        }
        return this.openRequestList(target.domain || []);
    }

    openSentDocuments() {
        this.openCard(this.cards.find((card) => card.key === "sent"));
    }

    onCardKeydown(event, card) {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            this.openCard(card);
        }
    }

    onRequestKeydown(event, requestId) {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            this.openRequest(requestId);
        }
    }

    onDepartmentKeydown(event, department) {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            this.openDepartment(department);
        }
    }

    openRequest(requestId) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sign.oca.request",
            res_id: requestId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    signNow(request) {
        if (request.url) {
            window.location.href = request.url;
            return;
        }
        return this.openRequest(request.id);
    }

    openDepartment(department) {
        return this.openRequestList(department.domain || []);
    }

    newRequest() {
        if (!this.state.data?.can_create_documents) {
            return;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sign.oca.request",
            views: [[false, "form"]],
            target: "current",
        });
    }
}

SignOcaDashboard.template = "sign_oca.Dashboard";
registry.category("actions").add("sign_oca_dashboard", SignOcaDashboard);

from lxml import etree

from odoo import api, models
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG
from odoo.addons.base.models.res_users import name_selection_groups


class ResGroups(models.Model):
    _inherit = "res.groups"

    _SIGN_PROFILE_XMLIDS = (
        "sign_oca.sign_oca_group_user",
        "sign_oca.sign_oca_group_creator",
        "sign_oca.sign_oca_group_manager",
        "sign_oca.sign_oca_group_admin",
    )
    _SIGN_CONFIDENTIAL_XMLID = "sign_oca.sign_oca_group_confidential"
    _SIGN_MASS_SEND_XMLID = "sign_oca.sign_oca_group_mass_send"

    @api.model
    def _get_sign_profile_groups(self):
        groups_model = self.env["res.groups"].sudo()
        profiles = groups_model.browse()
        for xmlid in self._SIGN_PROFILE_XMLIDS:
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if not group:
                return groups_model.browse()
            profiles |= group.sudo()
        return profiles

    @api.model
    def _get_sign_confidential_group(self):
        group = self.env.ref(
            self._SIGN_CONFIDENTIAL_XMLID, raise_if_not_found=False
        )
        return group.sudo() if group else self.env["res.groups"].sudo().browse()

    @api.model
    def _get_sign_mass_send_group(self):
        """Return the independent manual permission for mass sending."""
        group = self.env.ref(
            self._SIGN_MASS_SEND_XMLID, raise_if_not_found=False
        )
        if group:
            return group.sudo()

        groups_model = self.env["res.groups"].sudo()
        sign_category = self.env.ref(
            "sign_oca.sign_oca_module_category", raise_if_not_found=False
        )
        if not sign_category:
            return groups_model.browse()
        candidates = groups_model.search(
            [
                ("category_id", "=", sign_category.id),
                "|",
                ("name", "ilike", "masivo"),
                ("name", "ilike", "mass send"),
            ]
        )
        profile_ids = set(self._get_sign_profile_groups().ids)
        return candidates.filtered(lambda candidate: candidate.id not in profile_ids)[:1]

    @api.model
    def get_groups_by_application(self):
        """Keep four Sign profiles as a selector and two controls as checks.

        The standard Odoo classifier turns a category into checkboxes as soon
        as it contains an independent group. Firmas needs the four inherited
        profiles to remain mutually exclusive while confidentiality and mass
        sending stay independently assignable.
        """
        result = super().get_groups_by_application()
        profiles = self._get_sign_profile_groups()
        if not profiles:
            return result

        confidential = self._get_sign_confidential_group()
        mass_send = self._get_sign_mass_send_group()
        controlled = profiles | confidential | mass_send
        controlled_ids = set(controlled.ids)
        sign_category = self.env.ref(
            "sign_oca.sign_oca_module_category", raise_if_not_found=False
        )
        if not sign_category:
            return result

        ordered_profiles = profiles.sorted(
            key=lambda group: len(group.trans_implied_ids & profiles)
        )
        sign_category_name = (sign_category.sequence or 0, "SIGN")
        independent = confidential | mass_send
        rebuilt = []
        inserted = False

        for app, kind, groups, category_name in result:
            sign_groups = groups.filtered(lambda group: group.id in controlled_ids)
            if sign_groups:
                if not inserted:
                    rebuilt.append(
                        (sign_category, "selection", ordered_profiles, sign_category_name)
                    )
                    if independent:
                        rebuilt.append(
                            (sign_category, "boolean", independent, sign_category_name)
                        )
                    inserted = True
                remaining = groups - sign_groups
                if remaining:
                    rebuilt.append((app, kind, remaining, category_name))
            else:
                rebuilt.append((app, kind, groups, category_name))

        if not inserted:
            rebuilt.append(
                (sign_category, "selection", ordered_profiles, sign_category_name)
            )
            if independent:
                rebuilt.append(
                    (sign_category, "boolean", independent, sign_category_name)
                )
        return rebuilt

    @api.model
    def _update_user_groups_view(self):
        """Regenerate and present all Sign controls in one visible section."""
        # During an install/upgrade Odoo intentionally writes a temporary
        # ``groups_id`` placeholder. Generate the real view with that context
        # removed; otherwise the placeholder survives the upgrade and users
        # see the raw group table.
        clean_context = dict(self.env.context)
        clean_context.pop("install_filename", None)
        clean_context.pop(MODULE_UNINSTALL_FLAG, None)
        result = super(
            ResGroups, self.with_context(clean_context)
        )._update_user_groups_view()

        view = self.env.ref("base.user_groups_view", raise_if_not_found=False)
        profiles = self._get_sign_profile_groups()
        confidential = self._get_sign_confidential_group()
        mass_send = self._get_sign_mass_send_group()
        if not view or not profiles or not confidential or not mass_send:
            return result

        selector_name = name_selection_groups(profiles.ids)
        confidential_name = f"in_group_{confidential.id}"
        mass_send_name = f"in_group_{mass_send.id}"
        root = etree.fromstring((view.arch or "").encode())
        visible_fields = [
            node for node in root.iter("field") if node.get("invisible") != "True"
        ]
        selector_nodes = [
            node for node in visible_fields if node.get("name") == selector_name
        ]
        confidential_nodes = [
            node for node in visible_fields if node.get("name") == confidential_name
        ]
        mass_send_nodes = [
            node for node in visible_fields if node.get("name") == mass_send_name
        ]
        if (
            len(selector_nodes) != 1
            or len(confidential_nodes) != 1
            or len(mass_send_nodes) != 1
        ):
            return result

        selector_node = selector_nodes[0]
        confidential_node = confidential_nodes[0]
        mass_send_node = mass_send_nodes[0]
        target_parent = selector_node.getparent()
        if target_parent is None:
            return result

        selector_node.set("string", "Perfil de Firmas")
        confidential_node.set(
            "string", "Documentos confidenciales: ver y gestionar todos"
        )
        mass_send_node.set("string", "Envío masivo de documentos")

        old_parents = {
            node.getparent()
            for node in (selector_node, confidential_node, mass_send_node)
            if node.getparent() is not None
        }
        for node in (selector_node, confidential_node, mass_send_node):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

        for offset, node in enumerate(
            (selector_node, confidential_node, mass_send_node)
        ):
            target_parent.insert(offset, node)

        # Remove only containers that became empty after moving the controls;
        # unrelated CRM and other application fields remain untouched.
        for parent in old_parents:
            if parent is None or parent is target_parent:
                continue
            if not parent.xpath(".//field[not(@invisible='True')]"):
                grand_parent = parent.getparent()
                if grand_parent is not None:
                    grand_parent.remove(parent)

        new_arch = etree.tostring(root, pretty_print=True, encoding="unicode")
        if new_arch != view.arch:
            view.with_context(lang=None).write({"arch": new_arch})
        return result

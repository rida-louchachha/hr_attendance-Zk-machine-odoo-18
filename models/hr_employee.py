# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class HrEmployee(models.Model):
    """Extend hr.employee with a Biometric Device ID used by ZKTeco terminals."""
    _inherit = "hr.employee"

    device_id_num = fields.Char(
        string="Biometric Device ID",
        help="User ID on the ZKTeco device (the numeric 'User ID' shown on the terminal).",
        index=True,
        copy=False,
        tracking=True,
    )

    _sql_constraints = [
        (
            "uniq_employee_device_id_per_company",
            "unique(company_id, device_id_num)",
            "Another employee already uses this Biometric Device ID in the same company.",
        ),
    ]

    @api.onchange("device_id_num")
    def _onchange_device_id_num_strip(self):
        if self.device_id_num:
            self.device_id_num = self.device_id_num.strip()

    @api.constrains("device_id_num")
    def _check_device_id_num_format(self):
        for emp in self:
            if not emp.device_id_num:
                continue
            if not re.fullmatch(r"\d{1,10}", emp.device_id_num):
                raise ValidationError(
                    _("Biometric Device ID must be numeric (1–10 digits). You entered: %s") % emp.device_id_num
                )

    # --- QoL search by device ID ---
    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=80):
        args = args or []
        domain = ["|", ("name", operator, name), ("device_id_num", "ilike", name)]
        recs = self.search(domain + args, limit=limit)
        # Avoid calling name_get: build (id, display_name) pairs directly
        return [(rec.id, rec.display_name) for rec in recs]

    @api.model
    def _name_search(self, name="", args=None, operator="ilike", limit=80, name_get_uid=None):
        args = args or []
        return self._search(
            ["|", ("name", operator, name), ("device_id_num", "ilike", name)] + args,
            limit=limit,
            access_rights_uid=name_get_uid,
        )

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
        # Avoid two employees with the same device ID in the same company.
        # (If you truly need duplicates—for multi-device reuse—remove this or scope it differently.)
        (
            "uniq_employee_device_id_per_company",
            "unique(company_id, device_id_num)",
            "Another employee already uses this Biometric Device ID in the same company.",
        ),
    ]

    @api.onchange("device_id_num")
    def _onchange_device_id_num_strip(self):
        """Trim whitespace to avoid hidden duplicates and mistakes."""
        if self.device_id_num:
            self.device_id_num = self.device_id_num.strip()

    @api.constrains("device_id_num")
    def _check_device_id_num_format(self):
        """
        Enforce a sensible format. Most ZKTeco User IDs are numeric.
        If your deployment uses alphanumeric IDs, relax this check.
        """
        for emp in self:
            if not emp.device_id_num:
                continue
            # Enforce digits only (allow leading zeros, length 1..10 is typical; adjust as needed)
            if not re.fullmatch(r"\d{1,10}", emp.device_id_num):
                raise ValidationError(
                    _(
                        "Biometric Device ID must be numeric (1–10 digits). "
                        "You entered: %s"
                    )
                    % emp.device_id_num
                )

    # --- Quality of life: make searching by device ID easy everywhere ---
    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=80):
        args = args or []
        domain = ["|", ("name", operator, name), ("device_id_num", "ilike", name)]
        recs = self.search(domain + args, limit=limit)
        return recs.name_get()

    @api.model
    def _name_search(self, name, args=None, operator="ilike", limit=80, name_get_uid=None):
        args = args or []
        return self._search(
            ["|", ("name", operator, name), ("device_id_num", "ilike", name)] + args,
            limit=limit,
            access_rights_uid=name_get_uid,
        )

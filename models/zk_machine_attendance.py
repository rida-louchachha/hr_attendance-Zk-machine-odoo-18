# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ZKMachineAttendance(models.Model):
    """
    Raw, append-only table for ZKTeco punches pulled via SDK.

    IMPORTANT:
    - This model is for raw device logs. It does NOT inherit hr.attendance.
    - Your sync logic (action_download_attendance) is responsible for
      creating/updating hr.attendance records (check-in/out pairs).
    """
    _name = "zk.machine.attendance"
    _description = "ZKTeco Raw Attendance"
    _order = "punching_time desc, id desc"

    # ---- Core identifiers ----
    employee_id = fields.Many2one(
        "hr.employee",
        string="Employee",
        required=True,
        index=True,
        ondelete="cascade",
        help="Matched employee for this device user ID.",
    )
    device_id_num = fields.Char(
        string="Biometric Device ID",
        required=True,
        index=True,
        help="The user ID on the ZKTeco device (numeric on the terminal).",
    )

    # ---- Raw punch metadata from the device ----
    punching_time = fields.Datetime(
        string="Punching Time",
        required=True,
        index=True,
        help="Original timestamp reported by the device (stored in UTC).",
    )
    # Device 'punch' numeric code (0=in, 1=out, etc.). Optional but handy for audits.
    punch = fields.Integer(
        string="Punch Code",
        help="Device punch code as-is (0=in, 1=out, etc.).",
    )
    punch_type = fields.Selection(
        [
            ("0", "Check In"),
            ("1", "Check Out"),
            ("2", "Break Out"),
            ("3", "Break In"),
            ("4", "Overtime In"),
            ("5", "Overtime Out"),
            ("255", "Duplicate"),
        ],
        string="Punching Type",
        help="Mapped punch type returned by the device/driver.",
    )
    attendance_type = fields.Selection(
        [
            ("1", "Finger"),
            ("15", "Face"),
            ("2", "Type_2"),
            ("3", "Password"),
            ("4", "Card"),
            ("255", "Duplicate"),
        ],
        string="Category",
        help="Biometric method detected by the device.",
    )
    address_id = fields.Many2one(
        "res.partner",
        string="Working Address",
        help="Optional working location associated with this punch.",
    )

    # Optional: link the source device config (handy when you have multiple devices)
    device_ref_id = fields.Many2one(
        "biometric.device.details",
        string="Source Device",
        help="Odoo device record from which this log was pulled.",
    )

    # ---- Constraints ----
    _sql_constraints = [
        (
            "uniq_device_punch_ts",
            "unique(device_id_num, punching_time)",
            "Duplicate punch detected for the same device user at the same timestamp.",
        ),
    ]

    @api.constrains("punching_time")
    def _check_punching_time_not_future(self):
        """Prevent obviously future dates (helps catch TZ mistakes)."""
        now = fields.Datetime.now()
        for rec in self:
            # Allow small clock skews (~10 minutes) if you want; here we forbid any future.
            if rec.punching_time and rec.punching_time > now:
                raise ValidationError(
                    _("Punching Time cannot be in the future (got: %s).") % rec.punching_time
                )

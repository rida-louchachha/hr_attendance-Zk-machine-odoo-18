# -*- coding: utf-8 -*-
from odoo import fields, models, tools


class DailyAttendance(models.Model):
    """Read-only daily view of raw ZK logs."""
    _name = "daily.attendance"
    _description = "Daily Attendance Report"
    _auto = False
    _order = "punching_time desc"

    # View fields (must match SELECT)
    employee_id = fields.Many2one("hr.employee", string="Employee", readonly=True, index=True)
    punching_day = fields.Datetime(string="Date", readonly=True, help="Date/time the row was written")
    address_id = fields.Many2one("res.partner", string="Working Address", readonly=True)
    attendance_type = fields.Selection(
        [
            ("1", "Finger"),
            ("15", "Face"),
            ("2", "Type_2"),
            ("3", "Password"),
            ("4", "Card"),
        ],
        string="Category",
        readonly=True,
        help="Attendance detecting method",
    )
    punch_type = fields.Selection(
        [
            ("0", "Check In"),
            ("1", "Check Out"),
            ("2", "Break Out"),
            ("3", "Break In"),
            ("4", "Overtime In"),
            ("5", "Overtime Out"),
        ],
        string="Punching Type",
        readonly=True,
        help="Raw punch type returned by device",
    )
    punching_time = fields.Datetime(string="Punching Time", readonly=True, index=True)
    # Optional convenience columns (kept for UI filters; not filled by this view)
    check_in = fields.Datetime(readonly=True)
    check_out = fields.Datetime(readonly=True)

    def init(self):
        """(Re)create SQL view. Uses write_date as a stable 'day marker' and shows raw punch timestamp."""
        tools.drop_view_if_exists(self._cr, self._table)
        self._cr.execute(
            """
            CREATE OR REPLACE VIEW %s AS (
                SELECT
                    MIN(z.id)              AS id,
                    z.employee_id          AS employee_id,
                    z.write_date           AS punching_day,
                    z.address_id           AS address_id,
                    z.attendance_type      AS attendance_type,
                    z.punch_type           AS punch_type,
                    z.punching_time        AS punching_time
                FROM zk_machine_attendance z
                JOIN hr_employee e ON e.id = z.employee_id
                GROUP BY
                    z.employee_id,
                    z.write_date,
                    z.address_id,
                    z.attendance_type,
                    z.punch_type,
                    z.punching_time
            )
            """ % self._table
        )

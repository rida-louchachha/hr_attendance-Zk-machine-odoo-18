# -*- coding: utf-8 -*-
import datetime
import logging
import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Try to import pyzk once, fail gracefully with a clear message.
try:
    from zk import ZK, const  # noqa: F401
    _ZK_AVAILABLE = True
    _ZK_ERROR = None
except Exception as e:  # ImportError or anything environment-related
    _ZK_AVAILABLE = False
    _ZK_ERROR = str(e)


class BiometricDeviceDetails(models.Model):
    """Configuration and operations for a ZKTeco device."""
    _name = "biometric.device.details"
    _description = "Biometric Device Details"

    # ---- Config fields ----
    name = fields.Char(required=True)
    device_ip = fields.Char(required=True)
    port_number = fields.Integer(default=4370)
    working_address = fields.Many2one('res.partner', string="Working Address")
    device_password = fields.Integer(string="Comm Key / Password", default=0)

    auto_sync = fields.Boolean(string="Auto Sync", default=True, help="Include this device in the scheduler download.")
    last_sync_at = fields.Datetime(string="Last Sync", readonly=True)

    # Optional: keep time aligned from user/device TZ if you like
    # device_tz = fields.Char(string="Device Timezone", default="UTC")

    # ---- Internal helpers ----
    def _require_zk(self):
        """Ensure pyzk is installed in the server environment."""
        if not _ZK_AVAILABLE:
            raise UserError(_(
                "Missing Python library 'pyzk'. "
                "Ask your administrator to install it (pip install pyzk).\n\nDetails: %s"
            ) % _ZK_ERROR)

    def _safe_disconnect(self, conn):
        """Disconnect without raising if the connection is already closed/broken."""
        try:
            if conn and hasattr(conn, "disconnect"):
                conn.disconnect()
        except Exception as e:
            _logger.warning("Safe disconnect failed: %s", e)

    def _connect(self):
        """Create a ZK connection using current record configuration."""
        self._require_zk()
        zk = ZK(
            self.device_ip,
            port=self.port_number or 4370,
            timeout=30,
            password=self.device_password or 0,
            force_udp=False,
            ommit_ping=False,
        )
        return zk.connect()

    # ---- UI actions ----
    def action_test_connection(self):
        conn = None
        try:
            conn = self._connect()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "message": _("Successfully Connected to Biometric Device"),
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as e:
            raise ValidationError(_("Connection failed: %s") % e)
        finally:
            self._safe_disconnect(conn)

    def action_set_timezone(self):
        """Push the current user's local time to the device."""
        conn = None
        try:
            conn = self._connect()
            user_tz = self.env.user.tz or "UTC"
            now_utc = fields.Datetime.now()  # naive UTC in Odoo
            # Make timezone-aware in UTC, then convert to user's TZ
            current = pytz.utc.localize(now_utc).astimezone(pytz.timezone(user_tz))
            conn.set_time(current)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "message": _("Successfully set the device time"),
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as e:
            raise UserError(_("Failed to set device timezone: %s") % e)
        finally:
            self._safe_disconnect(conn)

    def action_clear_attendance(self):
        """Clear logs on device and raw table in Odoo."""
        conn = None
        try:
            conn = self._connect()
            has_data = bool(conn.get_attendance())
            if has_data:
                conn.clear_attendance()
                self.env.cr.execute("DELETE FROM zk_machine_attendance")
                msg, t = _("Attendance data cleared successfully"), "success"
            else:
                msg, t = _("No attendance data found to clear"), "warning"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"message": msg, "type": t, "sticky": False},
            }
        except Exception as e:
            raise ValidationError(_("Failed to clear attendance: %s") % e)
        finally:
            self._safe_disconnect(conn)

    def _resolve_employee_for_log(self, log_user_id: int, users_map: dict):
        """
        1) Match hr.employee by device_id_num == log_user_id
        2) Else match by device user name (exact, case-insensitive). If one match:
             set employee.device_id_num = log_user_id
        3) Else match by unique ilike name; if one:
             set employee.device_id_num = log_user_id
        4) Else create a new employee with that name and device_id_num
        """
        HrEmployee = self.env['hr.employee'].sudo()

        # 1) direct match
        emp = HrEmployee.search([('device_id_num', '=', str(log_user_id))], limit=1)
        if emp:
            return emp

        dev_user = users_map.get(log_user_id)
        dev_name = (dev_user and (dev_user.name or '').strip()) or ''

        if dev_name:
            # 2) exact case-insensitive
            candidates = HrEmployee.search([('name', 'ilike', dev_name)], limit=10)
            exact = [e for e in candidates if (e.name or '').strip().casefold() == dev_name.casefold()]
            if len(exact) == 1:
                emp = exact[0]
                if not emp.device_id_num:
                    emp.write({'device_id_num': str(log_user_id)})
                return emp

            # 3) unique ilike
            if not exact and len(candidates) == 1:
                emp = candidates[0]
                if not emp.device_id_num:
                    emp.write({'device_id_num': str(log_user_id)})
                return emp

        # 4) create
        return HrEmployee.create({
            'name': dev_name or str(log_user_id),
            'device_id_num': str(log_user_id),
        })

    @api.model
    def cron_download(self):
        """Run auto-sync for all enabled devices in a single recordset call."""
        if not _ZK_AVAILABLE:
            _logger.error("pyzk not available; skipping cron_download. Details: %s", _ZK_ERROR)
            return
        devices = self.search([('auto_sync', '=', True)])
        if devices:
            devices.with_context(from_cron=True).action_download_attendance()

    def action_download_attendance(self):
        """
        Pull logs from device and build hr.attendance with overlap-safety.

        Enforcement added:
          - Each log's user_id must exist as biometric.device.user on THIS device
            AND be linked to an hr.employee.
          - The linked employee's name must contain at least two words.
          - We do NOT auto-create employees here. If something isn't linked, we
            raise a UserError and stop.

        Other protections preserved:
          - No duplicate lines at the same time/day (±5s grace).
          - One open span max per employee per day.
          - Min duration guard, cool-down after OUT, span extension on OUT, etc.
        """
        import datetime
        import pytz

        self._require_zk()
        ZkLog = self.env["zk.machine.attendance"].sudo()
        HrAtt = self.env["hr.attendance"].sudo()
        Bdu = self.env["biometric.device.user"].sudo()

        IN_CODES = {0, 3, 4}  # Check In, Break In, Overtime In
        OUT_CODES = {1, 2, 5}  # Check Out, Break Out, Overtime Out

        DUP_GRACE_SEC = 5  # ±5s same-side duplicate collapse
        MIN_DURATION_SEC = 30  # discard spans shorter than this when closing
        COOLDOWN_AFTER_OUT = 10  # wait N seconds before opening a new IN after a close

        DEVICE_DEFAULT_TZ = "Africa/Casablanca"
        tzname = getattr(self, "device_tz", False) or DEVICE_DEFAULT_TZ
        try:
            dev_tz = pytz.timezone(tzname)
        except Exception:
            dev_tz = pytz.timezone(DEVICE_DEFAULT_TZ)

        # --- small helpers -------------------------------------------------------
        def _to_utc_pair(local_dt):
            """Return (UTC string, UTC datetime) for a device-local naive datetime."""
            utc_dt = dev_tz.localize(local_dt, is_dst=None).astimezone(pytz.utc)
            s = fields.Datetime.to_string(utc_dt)
            return s, fields.Datetime.to_datetime(s)

        def _tokens(s):
            s = (s or "").replace("_", " ").replace("-", " ")
            return [t for t in s.split() if t]

        def _day_bounds(dt_utc):
            day_start = dt_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + datetime.timedelta(days=1)
            return fields.Datetime.to_string(day_start), fields.Datetime.to_string(day_end)

        def _find_span_covering_ts(eid, ts_dt):
            """
            Return an attendance that ALREADY covers ts_dt (same day):
            check_in <= ts < check_out (or open span whose check_in <= ts).
            """
            day_start, day_end = _day_bounds(ts_dt)
            recs = HrAtt.search([
                ('employee_id', '=', eid),
                ('check_in', '>=', day_start),
                ('check_in', '<', day_end),
            ], order='check_in desc')
            for r in recs:
                ci = r.check_in and fields.Datetime.to_datetime(r.check_in)
                co = r.check_out and fields.Datetime.to_datetime(r.check_out)
                if not ci:
                    continue
                if co:
                    if ci <= ts_dt < co:
                        return r
                else:  # open span today
                    if ci <= ts_dt:
                        return r
            return None

        # -------------------------------------------------------------------------
        conn = None
        try:
            conn = self._connect()
            conn.disable_device()

            # We only allow building attendance for users that are **already** known
            # in Odoo under biometric.device.user and properly linked to an employee.
            # That keeps HR identities clean and avoids single-word names.
            zk_logs = conn.get_attendance() or []
            if not zk_logs:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {"message": _("No attendance records found on the device"),
                               "type": "warning", "sticky": False},
                }

            # get all device users once for better messages (optional)
            dev_users = {u.user_id: u for u in (conn.get_users() or [])}

            # Process chronologically
            try:
                zk_logs.sort(key=lambda r: r.timestamp)
            except Exception:
                pass

            # state caches
            open_att = {}  # emp_id -> open hr.attendance or None
            last_in_ts = {}  # emp_id -> datetime
            last_out_ts = {}  # emp_id -> datetime
            last_closed_out = {}  # emp_id -> datetime

            # seed opens
            for rec in HrAtt.search([('check_out', '=', False)]):
                open_att[rec.employee_id.id] = rec

            count_upserted = 0

            for log in zk_logs:
                ts_str, ts_dt = _to_utc_pair(log.timestamp)
                win_start = ts_dt - datetime.timedelta(seconds=DUP_GRACE_SEC)
                win_end = ts_dt + datetime.timedelta(seconds=DUP_GRACE_SEC)

                p_type_str = str(getattr(log, "punch", ""))  # raw punch code
                try:
                    punch_code = int(p_type_str)
                except Exception:
                    punch_code = None

                uid_str = str(log.user_id)

                # -------- NEW: resolve only through biometric.device.user ----------
                bdu = Bdu.search([('device_id', '=', self.id), ('user_id', '=', uid_str)], limit=1)
                if not bdu or not bdu.employee_id:
                    # Build clear message with optional device name and user name
                    u = dev_users.get(log.user_id)
                    shown = (u and (u.name or '').strip()) or uid_str
                    raise UserError(_(
                        "Device user '%s' (ID %s) on device '%s' is not linked to an Employee.\n"
                        "Open the device users, link (or create) the employee with a full first & last name, "
                        "then run the download again."
                    ) % (shown, uid_str, self.name))

                emp = bdu.employee_id

                # Enforce two-word employee name
                if len(_tokens(emp.name)) < 2:
                    raise UserError(_(
                        "Employee '%s' linked to device user ID %s must have at least a first and last name.\n"
                        "Please correct the name, then retry."
                    ) % (emp.name or "", uid_str))

                # keep device_id_num consistent (do not overwrite if different; only fill if empty)
                if not emp.device_id_num:
                    emp.write({'device_id_num': uid_str})

                eid = emp.id
                if eid not in open_att:
                    open_att[eid] = None
                if eid not in last_closed_out:
                    last_closed = HrAtt.search([('employee_id', '=', eid), ('check_out', '!=', False)],
                                               order='check_out desc', limit=1)
                    last_closed_out[eid] = last_closed.check_out and fields.Datetime.to_datetime(last_closed.check_out)

                # Upsert raw table for analysis
                raw = ZkLog.search([("device_id_num", "=", uid_str), ("punching_time", "=", ts_str)], limit=1)
                raw_vals = {
                    "employee_id": eid,
                    "device_id_num": uid_str,
                    "punching_time": ts_str,
                    "attendance_type": str(getattr(log, "status", "")),
                    "punch_type": p_type_str,
                    "punch": punch_code,
                    "device_ref_id": self.id,
                    "address_id": self.working_address.id if getattr(self, "working_address", False) else False,
                }
                (raw and raw.write(raw_vals)) or ZkLog.create(raw_vals)
                count_upserted += 1

                # -------------------- IN (open) --------------------
                if punch_code in IN_CODES:
                    li = last_in_ts.get(eid)
                    if li and abs((ts_dt - li).total_seconds()) <= DUP_GRACE_SEC:
                        continue  # duplicate IN

                    cover = _find_span_covering_ts(eid, ts_dt)
                    if cover:
                        if not cover.check_out:
                            open_att[eid] = cover
                        last_in_ts[eid] = ts_dt
                        continue

                    if open_att[eid]:
                        last_in_ts[eid] = ts_dt
                        continue

                    lc = last_closed_out.get(eid)
                    if lc and (ts_dt - lc) <= datetime.timedelta(seconds=COOLDOWN_AFTER_OUT):
                        last_in_ts[eid] = ts_dt
                        continue

                    same_in = HrAtt.search([
                        ('employee_id', '=', eid),
                        ('check_in', '>=', fields.Datetime.to_string(win_start)),
                        ('check_in', '<=', fields.Datetime.to_string(win_end)),
                    ], limit=1)
                    if same_in:
                        if not same_in.check_out:
                            open_att[eid] = same_in
                        last_in_ts[eid] = ts_dt
                        continue

                    open_att[eid] = HrAtt.create({'employee_id': eid, 'check_in': ts_str})
                    last_in_ts[eid] = ts_dt

                # -------------------- OUT (close) --------------------
                elif punch_code in OUT_CODES:
                    lo = last_out_ts.get(eid)
                    if lo and abs((ts_dt - lo).total_seconds()) <= DUP_GRACE_SEC:
                        continue  # duplicate OUT

                    att = open_att.get(eid)

                    if not att:
                        cover = _find_span_covering_ts(eid, ts_dt)
                        if cover:
                            last_out_ts[eid] = ts_dt
                            continue

                        day_start, day_end = _day_bounds(ts_dt)
                        last_today = HrAtt.search([
                            ('employee_id', '=', eid),
                            ('check_in', '>=', day_start),
                            ('check_in', '<', day_end),
                        ], order='check_in desc', limit=1)

                        if last_today and last_today.check_in:
                            ci = fields.Datetime.to_datetime(last_today.check_in)
                            co = last_today.check_out and fields.Datetime.to_datetime(last_today.check_out)
                            if ts_dt >= ci and (not co or ts_dt > co):
                                adj_out = ts_dt
                                if co and adj_out <= co:
                                    adj_out = co + datetime.timedelta(seconds=1)
                                dur = (adj_out - ci).total_seconds()
                                if not MIN_DURATION_SEC or dur >= MIN_DURATION_SEC:
                                    last_today.write({'check_out': fields.Datetime.to_string(adj_out)})
                                    last_closed_out[eid] = adj_out
                        last_out_ts[eid] = ts_dt
                        continue

                    ci_dt = att.check_in and fields.Datetime.to_datetime(att.check_in)
                    if ci_dt:
                        out_dt = ts_dt
                        if out_dt <= ci_dt:
                            out_dt = ci_dt + datetime.timedelta(seconds=1)

                        duration = (out_dt - ci_dt).total_seconds()
                        if MIN_DURATION_SEC and duration < MIN_DURATION_SEC:
                            att.unlink()
                            open_att[eid] = None
                            last_out_ts[eid] = ts_dt
                            continue

                        att.write({'check_out': fields.Datetime.to_string(out_dt)})
                        last_closed_out[eid] = out_dt
                        open_att[eid] = None

                    last_out_ts[eid] = ts_dt

            # success toast
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "message": _("Download complete. Upserted %s records.") % count_upserted,
                    "type": "success" if count_upserted else "warning",
                    "sticky": False,
                },
            }

        except ValidationError:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"message": _("Attendance download completed (overlaps skipped)."),
                           "type": "success", "sticky": False},
            }
        except Exception as e:
            raise UserError(_("Error downloading attendance: %s") % e)
        finally:
            self._safe_disconnect(conn)

    def action_restart_device(self):
        conn = None
        try:
            conn = self._connect()
            conn.restart()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "message": _("Device restart command sent successfully"),
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as e:
            raise UserError(_("Failed to restart device: %s") % e)
        finally:
            self._safe_disconnect(conn)

    def action_open_device_users(self):
        """Sync users for THIS device, then open the list filtered to it."""
        self.ensure_one()
        # reuse the users model's sync, but scoped to this device
        self.env['biometric.device.user'].with_context(sync_devices=[self.id]).action_sync_users()
        return {
            "type": "ir.actions.act_window",
            "name": _("Device Users"),
            "res_model": "biometric.device.user",
            "view_mode": "list,form",
            "domain": [("device_id", "=", self.id)],
            "context": {
                "default_device_id": self.id,
                "search_default_device_id": self.id,
            },
        }
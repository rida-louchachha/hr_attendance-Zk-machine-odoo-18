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
        Pull logs from device:
        - UPSERT raw rows in zk.machine.attendance (create or update if same key),
        - create/update hr.attendance as in/out pairs (Breaks behave like check in/out),
        - employee resolution by device_id_num -> name -> create.
        """
        self._require_zk()
        ZkLog = self.env["zk.machine.attendance"].sudo()
        HrAtt = self.env["hr.attendance"].sudo()
        HrEmployee = self.env["hr.employee"].sudo()

        IN_CODES = {0, 3, 4}  # Check In, Break In, Overtime In
        OUT_CODES = {1, 2, 5}  # Check Out, Break Out, Overtime Out
        DUP_GRACE = 5  # seconds to collapse repeated same-side punches

        conn = None
        try:
            conn = self._connect()
            conn.disable_device()

            users = {u.user_id: u for u in (conn.get_users() or [])}
            logs = conn.get_attendance() or []
            if not logs:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {"message": _("No attendance records found on the device"),
                               "type": "warning", "sticky": False},
                }

            DEVICE_DEFAULT_TZ = "Africa/Casablanca"
            tzname = getattr(self, "device_tz", False) or DEVICE_DEFAULT_TZ

            last_in_ts, last_out_ts = {}, {}
            count_upserted = 0

            for log in logs:
                # ----- time (device local -> UTC) -----
                try:
                    dev_tz = pytz.timezone(tzname)
                except Exception:
                    dev_tz = pytz.timezone(DEVICE_DEFAULT_TZ)
                local_dt = dev_tz.localize(log.timestamp, is_dst=None)
                utc_dt = local_dt.astimezone(pytz.utc)
                ts = fields.Datetime.to_string(utc_dt)
                ts_dt = fields.Datetime.to_datetime(ts)

                # ----- raw values -----
                att_type = str(getattr(log, "status", ""))  # finger/face/card…
                p_type_str = str(getattr(log, "punch", ""))  # '0'..'5'
                try:
                    punch_code = int(p_type_str)
                except (TypeError, ValueError):
                    punch_code = None

                # ----- employee resolution -----
                dev_user_id_str = str(log.user_id)
                emp = HrEmployee.search([("device_id_num", "=", dev_user_id_str)], limit=1)
                if not emp:
                    dev_user = users.get(log.user_id)
                    dev_name = (dev_user and (dev_user.name or "").strip()) or ""
                    if dev_name:
                        emp = HrEmployee.search([("name", "=", dev_name)], limit=1) or \
                              HrEmployee.search([("name", "ilike", dev_name)], limit=1)
                        if emp and not emp.device_id_num:
                            emp.write({"device_id_num": dev_user_id_str})
                if not emp:
                    dev_user = users.get(log.user_id)
                    new_name = (dev_user.name if dev_user and dev_user.name else dev_user_id_str)
                    emp = HrEmployee.create({"name": new_name, "device_id_num": dev_user_id_str})

                # ----- UPSERT raw log -----
                raw = ZkLog.search([
                    ("device_id_num", "=", dev_user_id_str),
                    ("punching_time", "=", ts),
                ], limit=1)
                raw_vals = {
                    "employee_id": emp.id,
                    "device_id_num": dev_user_id_str,
                    "punching_time": ts,
                    "attendance_type": att_type,
                    "punch_type": p_type_str,
                    "punch": punch_code,
                    "device_ref_id": self.id,
                    "address_id": self.working_address.id if getattr(self, "working_address", False) else False,
                }
                (raw and raw.write(raw_vals)) or ZkLog.create(raw_vals)

                # ----- hr.attendance logic (Breaks == Check Ins/Outs) -----
                win_start = fields.Datetime.to_string(ts_dt - datetime.timedelta(seconds=DUP_GRACE))
                win_end = fields.Datetime.to_string(ts_dt + datetime.timedelta(seconds=DUP_GRACE))

                if punch_code in IN_CODES:
                    # duplicate-IN grace
                    li = last_in_ts.get(emp.id)
                    if li and abs((ts_dt - li).total_seconds()) <= DUP_GRACE:
                        _logger.debug("Skip duplicate IN (grace) for emp %s @ %s", emp.id, ts)
                    else:
                        # CHANGED: if an IN arrives while another IN is already open -> ignore (no split)
                        open_att = HrAtt.search([
                            ('employee_id', '=', emp.id),
                            ('check_out', '=', False),
                        ], order='check_in desc', limit=1)

                        same_in = HrAtt.search([
                            ('employee_id', '=', emp.id),
                            ('check_in', '>=', win_start),
                            ('check_in', '<=', win_end),
                        ], limit=1)

                        if not open_att and not same_in:
                            try:
                                HrAtt.create({'employee_id': emp.id, 'check_in': ts})
                            except ValidationError:
                                _logger.debug("Conflicting IN for emp %s @ %s", emp.id, ts)

                    last_in_ts[emp.id] = ts_dt

                elif punch_code in OUT_CODES:
                    # duplicate-OUT grace
                    lo = last_out_ts.get(emp.id)
                    if lo and abs((ts_dt - lo).total_seconds()) <= DUP_GRACE:
                        _logger.debug("Skip duplicate OUT (grace) for emp %s @ %s", emp.id, ts)
                    else:
                        open_att = HrAtt.search([
                            ('employee_id', '=', emp.id),
                            ('check_out', '=', False),
                        ], order='check_in desc', limit=1)

                        if open_att:
                            if not (open_att.check_out and win_start <= fields.Datetime.to_string(
                                    open_att.check_out) <= win_end):
                                if not open_att.check_in or ts_dt >= open_att.check_in:
                                    try:
                                        open_att.write({'check_out': ts})
                                    except ValidationError:
                                        _logger.debug("Conflicting OUT for emp %s @ %s", emp.id, ts)
                        else:
                            # CHANGED: do NOT create zero-length pairs for stray OUTs
                            _logger.debug("Stray OUT for emp %s @ %s -> ignored (no open IN).", emp.id, ts)

                    last_out_ts[emp.id] = ts_dt

                count_upserted += 1

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "message": _("Download complete. Upserted %s records.") % count_upserted,
                    "type": "success" if count_upserted else "warning",
                    "sticky": False,
                },
            }

        except ValidationError as ve:
            _logger.info("Attendance duplicates/overlaps skipped: %s", ve)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"message": _("Attendance download completed (duplicates skipped)."),
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

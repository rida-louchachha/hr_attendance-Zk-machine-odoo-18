# -*- coding: utf-8 -*-
################################################################################
#
#    louchachha Technologies Pvt. Ltd.
#    Copyright (C) 2025-TODAY louchachha Technologies(<https://github.com/rida-louchachha>).
#    Author: rida louchachha (https://github.com/rida-louchachha)
#
#    This program is free software: you can modify
#    it under the terms of the GNU Affero General Public License (AGPL) as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
################################################################################
import datetime
import logging
import subprocess
import sys
import pytz
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


# Force import with installation attempt
def install_pyzk():
    """Install pyzk library if not available"""
    try:
        from zk import ZK, const
        return True, None
    except ImportError:
        _logger.warning("pyzk library not found. Attempting to install...")
        try:
            # Try to install pyzk
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyzk"])
            _logger.info("pyzk library installed successfully")
            # Try import again
            from zk import ZK, const
            return True, None
        except subprocess.CalledProcessError:
            error_msg = "Failed to install pyzk automatically. Please install manually using: pip install pyzk"
            _logger.error(error_msg)
            return False, error_msg
        except ImportError:
            error_msg = "pyzk installation failed. Please install manually using: pip install pyzk"
            _logger.error(error_msg)
            return False, error_msg


# Attempt to install and import
ZK_IMPORTED, IMPORT_ERROR = install_pyzk()

if ZK_IMPORTED:
    from zk import ZK, const
else:
    _logger.error("Pyzk library not available: %s", IMPORT_ERROR)


class BiometricDeviceDetails(models.Model):
    """Model for configuring and connect the biometric device with odoo"""
    _name = 'biometric.device.details'
    _description = 'Biometric Device Details'

    name = fields.Char(string='Name', required=True, help='Record Name')
    device_ip = fields.Char(string='Device IP', required=True,
                            help='The IP address of the Device')
    port_number = fields.Integer(string='Port Number', required=True,
                                 help="The Port Number of the Device")
    address_id = fields.Many2one('res.partner', string='Working Address',
                                 help='Working address of the partner')
    company_id = fields.Many2one('res.company', string='Company',
                                 default=lambda self: self.env.user.company_id.id,
                                 help='Current Company')

    def _check_zk_import(self):
        """Check if pyzk is installed and available"""
        if not ZK_IMPORTED:
            raise UserError(_(
                "Pyzk module is required but not available.\n\n"
                "Please install it manually using:\n"
                "pip install pyzk\n\n"
                "Or contact your system administrator to install this Python library."
            ))

    def _safe_disconnect(self, conn):
        """Safely disconnect from device without raising errors"""
        try:
            if conn and hasattr(conn, 'disconnect'):
                conn.disconnect()
                _logger.info("Device disconnected successfully")
        except Exception as e:
            _logger.warning("Safe disconnect failed: %s", e)
            # Ignore disconnect errors as they're not critical

    def device_connect(self, zk):
        """Function for connecting the device with Odoo"""
        self._check_zk_import()
        try:
            conn = zk.connect()
            return conn
        except Exception as e:
            _logger.error("Device connection error: %s", e)
            return False

    def action_test_connection(self):
        """Checking the connection status"""
        self._check_zk_import()

        try:
            zk = ZK(self.device_ip, port=self.port_number, timeout=30,
                    password=False, ommit_ping=False)
            conn = self.device_connect(zk)
            if conn:
                self._safe_disconnect(conn)
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'message': 'Successfully Connected to Biometric Device',
                        'type': 'success',
                        'sticky': False
                    }
                }
            else:
                raise ValidationError(_('Unable to connect to the biometric device. '
                                        'Please check the IP address and port number.'))
        except Exception as error:
            _logger.error("Connection test failed: %s", error)
            raise ValidationError(_('Connection failed: %s') % error)

    def action_set_timezone(self):
        """Function to set user's timezone to device"""
        self._check_zk_import()

        for info in self:
            machine_ip = info.device_ip
            zk_port = info.port_number
            conn = None
            try:
                zk = ZK(machine_ip, port=zk_port, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
                conn = self.device_connect(zk)
                if conn:
                    user_tz = self.env.context.get('tz') or self.env.user.tz or 'UTC'
                    user_timezone_time = pytz.utc.localize(fields.Datetime.now())
                    user_timezone_time = user_timezone_time.astimezone(
                        pytz.timezone(user_tz))
                    conn.set_time(user_timezone_time)
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': 'Successfully Set the Device Time',
                            'type': 'success',
                            'sticky': False
                        }
                    }
                else:
                    raise UserError(_("Cannot connect to device. Please check connection and try again."))
            except Exception as e:
                _logger.error("Timezone setting failed: %s", e)
                raise UserError(_("Failed to set device timezone: %s") % e)
            finally:
                # Always try to disconnect
                self._safe_disconnect(conn)

    def action_clear_attendance(self):
        """Method to clear record from the zk.machine.attendance model and from the device"""
        self._check_zk_import()
        for info in self:
            conn = None
            try:
                machine_ip = info.device_ip
                zk_port = info.port_number

                zk = ZK(machine_ip, port=zk_port, timeout=30,
                        password=0, force_udp=False, ommit_ping=False)

                conn = self.device_connect(zk)
                if conn:
                    conn.enable_device()
                    clear_data = conn.get_attendance()
                    if clear_data:
                        # Clearing data in the device
                        conn.clear_attendance()
                        # Clearing data from attendance log
                        self._cr.execute("""delete from zk_machine_attendance""")
                        return {
                            'type': 'ir.actions.client',
                            'tag': 'display_notification',
                            'params': {
                                'message': 'Attendance data cleared successfully',
                                'type': 'success',
                                'sticky': False
                            }
                        }
                    else:
                        return {
                            'type': 'ir.actions.client',
                            'tag': 'display_notification',
                            'params': {
                                'message': 'No attendance data found to clear',
                                'type': 'warning',
                                'sticky': False
                            }
                        }
                else:
                    raise UserError(
                        _('Unable to connect to Attendance Device. Please use '
                          'Test Connection button to verify.'))
            except Exception as error:
                _logger.error("Clear attendance failed: %s", error)
                raise ValidationError(_('Failed to clear attendance: %s') % error)
            finally:
                self._safe_disconnect(conn)

    @api.model
    def cron_download(self):
        """Cron method to download attendance from all machines"""
        if not ZK_IMPORTED:
            _logger.error("Pyzk not available. Skipping attendance download.")
            return

        machines = self.env['biometric.device.details'].search([])
        for machine in machines:
            try:
                machine.action_download_attendance()
            except Exception as e:
                _logger.error("Failed to download attendance from machine %s: %s", machine.name, e)

    def action_download_attendance(self):
        """Function to download attendance records from the device"""
        self._check_zk_import()
        _logger.info("Downloading attendance from biometric device: %s", self.name)

        zk_attendance = self.env['zk.machine.attendance']
        hr_attendance = self.env['hr.attendance']

        for info in self:
            machine_ip = info.device_ip
            zk_port = info.port_number
            conn = None

            try:
                zk = ZK(machine_ip, port=zk_port, timeout=15,
                        password=0, force_udp=False, ommit_ping=False)
                conn = self.device_connect(zk)

                if not conn:
                    raise UserError(
                        _('Unable to connect to device. Please check network connection and device parameters.'))

                conn.disable_device()  # Device Cannot be used during this time.
                users = conn.get_users()
                attendance_data = conn.get_attendance()

                if not attendance_data:
                    # No attendance data found - return success message instead of error
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': 'No attendance records found on the device',
                            'type': 'warning',
                            'sticky': False
                        }
                    }

                attendance_count = 0
                for attendance in attendance_data:
                    atten_time = attendance.timestamp
                    local_tz = pytz.timezone(
                        self.env.user.partner_id.tz or 'GMT')
                    local_dt = local_tz.localize(atten_time, is_dst=None)
                    utc_dt = local_dt.astimezone(pytz.utc)
                    utc_dt = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                    atten_time = datetime.datetime.strptime(
                        utc_dt, "%Y-%m-%d %H:%M:%S")
                    atten_time = fields.Datetime.to_string(atten_time)

                    for user in users:
                        if user.user_id == attendance.user_id:
                            employee = self.env['hr.employee'].search(
                                [('device_id_num', '=', attendance.user_id)])

                            if not employee:
                                # Create new employee if not found
                                employee = self.env['hr.employee'].create({
                                    'device_id_num': attendance.user_id,
                                    'name': user.name
                                })
                                _logger.info("Created new employee: %s", user.name)

                            # Check for duplicate attendance
                            duplicate_atten = zk_attendance.search([
                                ('device_id_num', '=', attendance.user_id),
                                ('punching_time', '=', atten_time)
                            ])

                            if not duplicate_atten:
                                # Create attendance record
                                zk_attendance.create({
                                    'employee_id': employee.id,
                                    'device_id_num': attendance.user_id,
                                    'attendance_type': str(attendance.status),
                                    'punch_type': str(attendance.punch),
                                    'punching_time': atten_time,
                                    'address_id': info.address_id.id
                                })
                                attendance_count += 1

                                # Update HR attendance
                                if attendance.punch == 0:  # check-in
                                    # Check if there's already an open attendance
                                    open_attendance = hr_attendance.search([
                                        ('employee_id', '=', employee.id),
                                        ('check_out', '=', False)
                                    ], order='check_in desc', limit=1)

                                    if not open_attendance:
                                        hr_attendance.create({
                                            'employee_id': employee.id,
                                            'check_in': atten_time
                                        })
                                    else:
                                        _logger.warning("Open attendance found for employee %s, skipping check-in",
                                                        employee.name)

                                elif attendance.punch == 1:  # check-out
                                    # Find open attendance record
                                    open_attendance = hr_attendance.search([
                                        ('employee_id', '=', employee.id),
                                        ('check_out', '=', False)
                                    ], order='check_in desc', limit=1)

                                    if open_attendance:
                                        open_attendance.write({
                                            'check_out': atten_time
                                        })
                                    else:
                                        # Create check-in/check-out pair if no open record found
                                        hr_attendance.create({
                                            'employee_id': employee.id,
                                            'check_in': atten_time,
                                            'check_out': atten_time
                                        })
                                        _logger.warning(
                                            "No open attendance found for employee %s, created check-in/out pair",
                                            employee.name)

                _logger.info("Successfully downloaded %s attendance records", attendance_count)

                if attendance_count > 0:
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': f'Successfully downloaded {attendance_count} attendance records',
                            'type': 'success',
                            'sticky': False
                        }
                    }
                else:
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'message': 'No new attendance records found to download',
                            'type': 'warning',
                            'sticky': False
                        }
                    }

            except Exception as e:
                _logger.error("Attendance download failed: %s", e)
                raise UserError(_('Error downloading attendance: %s') % e)
            finally:
                # Always try to disconnect, even if there's an error
                self._safe_disconnect(conn)

    def action_restart_device(self):
        """For restarting the device"""
        self._check_zk_import()
        conn = None

        try:
            zk = ZK(self.device_ip, port=self.port_number, timeout=15,
                    password=0, force_udp=False, ommit_ping=False)
            conn = self.device_connect(zk)
            if conn:
                conn.restart()
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'message': 'Device restart command sent successfully',
                        'type': 'success',
                        'sticky': False
                    }
                }
            else:
                raise UserError(_("Unable to connect to device"))
        except Exception as e:
            _logger.error("Device restart failed: %s", e)
            raise UserError(_("Failed to restart device: %s") % e)
        finally:
            self._safe_disconnect(conn)
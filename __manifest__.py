# -*- coding: utf-8 -*-
################################################################################
#
#    louchachha Technologies Pvt. Ltd.
#    Copyright (C) 2025-TODAY louchachha Technologies(<https://www.cybrosys.com>).
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
{
    'name': 'Biometric Device Integration Zk machine',
    'version': '18.0.1.0.0',
    'category': 'Human Resources',
    'summary': "Integrating Biometric Device (Model: ZKteco uFace 202) With HR"
               "Attendance (Face + Thumb)",
    'description': "This module integrates Odoo with the biometric"
                   "device(Model: ZKteco uFace 202),odoo18,odoo,hr,attendance",
    'author': 'Rida Louchachha',
    'company': 'Rida Louchachha',
    'maintainer': 'Rida Louchachha',
    'website': "https://github.com/rida-louchachha",
    'depends': ['base_setup', 'hr_attendance'],
    'external_dependencies': {
        'python': ['pyzk'],
    },
    'data': [
        'security/ir.model.access.csv',
        'security/zk_rules.xml',
        'data/ir_cron.xml',
        'views/biometric_device_details_views.xml',
        'views/hr_employee_views.xml',
        'views/daily_attendance_views.xml',
        'views/biometric_device_attendance_menus.xml',
    ],
    'images': ['static/description/banner.jpg'],
    'license': 'AGPL-3',
    'installable': True,
    'auto_install': False,
    'application': False,
}

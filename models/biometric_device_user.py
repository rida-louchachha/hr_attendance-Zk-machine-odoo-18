# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    from zk import ZK, const
    _ZK_AVAILABLE = True
    _ZK_ERROR = None
except Exception as e:
    _ZK_AVAILABLE = False
    _ZK_ERROR = str(e)


class BiometricDeviceUser(models.Model):
    _name = "biometric.device.user"
    _description = "ZKTeco Device User"
    _rec_name = "display_name"
    _sql_constraints = [
        ('device_uid_unique', 'unique(device_id, user_id)',
         'This device + User ID combination already exists.')
    ]

    # --------------------
    # FIELDS
    # --------------------
    device_id = fields.Many2one(
        "biometric.device.details",
        required=True, ondelete="cascade",
        string="Device"
    )
    user_id = fields.Char(required=True, index=True, string="User ID")
    employee_id = fields.Many2one("hr.employee", string="Employee", ondelete="set null", index=True)

    name = fields.Char(required=True)
    password = fields.Char(string="Password / PIN")
    privilege = fields.Selection([
        ('0', 'User'),
        ('1', 'Admin'),
        ('2', 'Supervisor'),
    ], default='0', string="Privilege")
    card_no = fields.Char(string="Card Number")
    enabled = fields.Boolean(default=True)

    fingerprint_count = fields.Integer(string="Fingerprints", readonly=True)
    face_count = fields.Integer(string="Faces", readonly=True)

    last_seen = fields.Datetime(readonly=True, help="Last time this user was read from the device.")
    display_name = fields.Char(compute="_compute_display_name", store=False)

    fingerprint_templates_json = fields.Binary(
        string="Fingerprint Templates (JSON)", attachment=True
    )
    face_templates_json = fields.Binary(
        string="Face Templates (JSON)", attachment=True
    )

    sync_state = fields.Selection(
        [
            ('synced', 'On Device'),
            ('needs_push', 'Needs Push'),
            ('unknown', 'Unknown'),
        ],
        string="Sync Status",
        compute="_compute_sync_state",
        default='unknown',
        store=False
    )

    # --------------------
    # COMPUTES
    # --------------------
    @api.depends('last_seen')
    def _compute_sync_state(self):
        for rec in self:
            rec.sync_state = 'synced' if rec.last_seen else 'needs_push'

    @api.depends('user_id', 'name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"[{rec.user_id}] {rec.name or ''}".strip()

    # --------------------
    # HELPERS
    # --------------------
    def _require_zk(self):
        if not _ZK_AVAILABLE:
            raise UserError(_(
                "Missing Python library 'pyzk'. "
                "Ask your administrator to install it (pip install pyzk).\n\nDetails: %s"
            ) % _ZK_ERROR)

    def _connect(self):
        self._require_zk()
        self.ensure_one()
        return self.device_id._connect()

    def _map_privilege(self, raw) -> str:
        try:
            val = int(raw)
        except Exception:
            return '0'
        if val in (1, 14, 15):
            return '1'
        if val == 2:
            return '2'
        return '0'

    def _tokens(self, s: str):
        s = (s or "").replace("_", " ").replace("-", " ")
        return [t for t in s.split() if t]

    def _clean_full_name(self, s: str) -> str:
        """Normalize for display: collapse spaces & title case."""
        s = " ".join(self._tokens(s or ""))
        return s.title() if s else s

    def _name_key(self, s: str) -> str:
        """Canonical key for whole-name equality (case/space-insensitive)."""
        return " ".join(self._tokens(s)).casefold()

    def _ensure_two_word_name(self, s: str, field_label=_("Name")) -> str:
        s2 = " ".join(self._tokens(s or ""))
        if len(self._tokens(s2)) < 2:
            raise UserError(_("%s must contain at least two words (first & last).") % field_label)
        return s2.title()

    def _bootstrap_mode(self):
        HrEmployee = self.env['hr.employee'].sudo()
        employees = HrEmployee.search([])
        if not employees:
            return True
        non_admin = employees.filtered(lambda e: (e.name or '').strip().lower() not in ('administrator', 'admin'))
        return len(non_admin) == 0

    def _normalize_bio_id(self, s: str) -> str:
        """Trim, remove all spaces, drop leading zeros (keep '0' if all zeros)."""
        s = (s or "").strip()
        s = "".join(s.split())
        s = s.lstrip("0") or "0"
        return s

    # --------------------
    # ORM HOOKS
    # --------------------
    @api.model
    def create(self, vals):
        # Enforce two words unless explicitly allowed (used by device auto-sync).
        if 'name' in vals and not self.env.context.get('allow_single_word_name'):
            vals['name'] = self._ensure_two_word_name(vals['name'])
        return super().create(vals)

    def write(self, vals):
        if 'name' in vals and not self.env.context.get('allow_single_word_name'):
            vals['name'] = self._ensure_two_word_name(vals['name'])
        return super().write(vals)

    # --------------------
    # MAIN BUTTONS
    # --------------------
    def action_sync_users(self):
        """
        Sync users <-> employees

        PULL (ZK -> Odoo):
          - Upsert biometric.device.user (BDU) for every ZK user
          - Link priority: by device_id_num (exact).
          - Else (only if NOT bootstrap) by exact whole-name BUT ONLY if BOTH sides have >= 2 words.
          - In bootstrap (only admin in Odoo): create hr.employee ONLY if the device user's name has >= 2 words.
            Otherwise, DO NOT create; just upsert BDU and leave unlinked.

        ODOO-ONLY employees (no device_id_num):
          - Create/Update a BDU row for the *same employee* (reuse if it already exists) with a reserved numeric
            user_id and last_seen=False (-> needs_push). No more duplicate BDUs per employee on repeated syncs.
          - If context push_new=True, also push to device and set last_seen (synced).
        """
        self._require_zk()
        HrEmployee = self.env['hr.employee'].sudo()
        Bdu = self.env['biometric.device.user'].sudo()

        device_ids = self.env.context.get('sync_devices')
        if device_ids:
            devices = self.env['biometric.device.details'].browse(device_ids)
        else:
            devices = self.mapped('device_id') if self else self.env['biometric.device.details'].search([])

        if not devices:
            raise UserError(_("No devices configured."))

        push_new = bool(self.env.context.get('push_new'))
        created_bdu = updated_bdu = 0
        emp_created = emp_linked_id = emp_linked_name = 0
        bdu_created_for_emp = bdu_updated_for_emp = 0
        pushed_new_users = 0

        # helpers
        def _tokens(s):
            s = (s or "").replace("_", " ").replace("-", " ")
            return [t for t in s.split() if t]

        def _name_key(s):
            s = (s or "").strip()
            s = " ".join(_tokens(s))
            return s.casefold()

        # Bootstrap?
        all_emps = HrEmployee.search([])
        non_admin_emps = all_emps.filtered(lambda e: (e.name or '').strip().lower() not in ('admin', 'administrator'))
        only_admin_present = (len(non_admin_emps) == 0)

        for device in devices:
            conn = None
            try:
                conn = device._connect()
                conn.disable_device()

                zk_users = conn.get_users() or []

                # Index device users
                by_uid = {str(getattr(u, 'user_id')): u for u in zk_users}
                by_name = {}
                for u in zk_users:
                    nm_key = _name_key(getattr(u, 'name', '') or str(getattr(u, 'user_id')))
                    by_name.setdefault(nm_key, []).append(u)

                # ------- A) PULL: upsert BDU & strictly link
                for u in zk_users:
                    uid_str = str(getattr(u, 'user_id'))
                    full_name = self._clean_full_name(getattr(u, 'name', "") or "")
                    raw_priv = getattr(u, "privilege", 0)
                    card = getattr(u, "card", "") or getattr(u, "cardno", "") or getattr(u, "card_num", "")

                    # Upsert BDU (allow single word during raw sync)
                    vals_bdu = {
                        "device_id": device.id,
                        "user_id": uid_str,
                        "name": full_name,
                        "password": (getattr(u, "password", "") or "").strip(),
                        "privilege": self._map_privilege(raw_priv),
                        "card_no": str(card or ""),
                        "enabled": True,
                        "last_seen": fields.Datetime.now(),
                    }
                    Bdu_ctx = Bdu.with_context(allow_single_word_name=True)
                    rec = Bdu_ctx.search([("device_id", "=", device.id), ("user_id", "=", uid_str)], limit=1)
                    if rec:
                        rec.with_context(allow_single_word_name=True).write(vals_bdu)
                        updated_bdu += 1
                    else:
                        rec = Bdu_ctx.create(vals_bdu)
                        created_bdu += 1

                    # Priority 1: link by ID (always allowed)
                    emp = HrEmployee.search([('device_id_num', '=', uid_str)], limit=1)
                    if emp:
                        if full_name and (not emp.name or emp.name.strip().lower() == uid_str.lower()):
                            emp.write({'name': full_name})
                        if not rec.employee_id:
                            rec.employee_id = emp.id
                        emp_linked_id += 1
                        continue

                    # Bootstrap: create employee ONLY if device name has >= 2 words, and not already present by name
                    if only_admin_present:
                        if len(_tokens(full_name)) >= 2:
                            # avoid creating duplicates across repeated syncs in bootstrap
                            existing_emp = HrEmployee.search([('name', '=ilike', full_name)], limit=1)
                            if existing_emp:
                                existing_emp.write({'device_id_num': uid_str})
                                rec.employee_id = existing_emp.id
                                emp_linked_name += 1
                            else:
                                emp = HrEmployee.create({'name': full_name, 'device_id_num': uid_str})
                                rec.employee_id = emp.id
                                emp_created += 1
                        # else: skip creating/linking — leave BDU unlinked
                        continue

                    # Non-bootstrap fallback: exact whole-name link ONLY if device name has >= 2 words
                    if len(_tokens(full_name)) >= 2:
                        cands = HrEmployee.search([('device_id_num', '=', False)])
                        exact = cands.filtered(
                            lambda e: len(_tokens(e.name)) >= 2 and _name_key(e.name) == _name_key(full_name)
                        )
                        if len(exact) == 1:
                            emp = exact[0]
                            emp.write({'device_id_num': uid_str})
                            rec.employee_id = emp.id
                            emp_linked_name += 1
                        elif len(exact) > 1:
                            _logger.info("Multiple employees share normalized name '%s'; leaving unlinked.", full_name)
                    # if < 2 words: do nothing (no creation, no linking)

                # ------- B) Odoo-only employees -> create/update ONE BDU per employee (needs_push)
                emps_no_dev = HrEmployee.search([('device_id_num', '=', False)])
                emps_no_dev = emps_no_dev.filtered(
                    lambda e: (e.name or '').strip().lower() not in ('admin', 'administrator')
                )

                used_uids = set()
                for k in by_uid.keys():
                    try:
                        used_uids.add(int(k))
                    except Exception:
                        pass
                # also prevent collisions with existing BDUs on this device
                for b_uid in Bdu.search([('device_id', '=', device.id)]).mapped('user_id'):
                    try:
                        used_uids.add(int(b_uid))
                    except Exception:
                        pass

                def next_free_uid():
                    cand = max(used_uids) + 1 if used_uids else 1
                    while cand in used_uids:
                        cand += 1
                    used_uids.add(cand)
                    return cand

                Bdu_ctx_emp = Bdu.with_context(allow_single_word_name=True)

                for emp in emps_no_dev:
                    nm_clean = self._clean_full_name(emp.name)
                    nm_key = _name_key(emp.name)

                    # ---- NEW: if a BDU for this employee & device already exists, reuse/update it
                    existing_bdu_for_emp = Bdu_ctx_emp.search([
                        ('device_id', '=', device.id),
                        ('employee_id', '=', emp.id),
                    ], limit=1)

                    if existing_bdu_for_emp:
                        uid_for_emp = existing_bdu_for_emp.user_id
                        # make sure it's in the used set to avoid being reused elsewhere
                        try:
                            used_uids.add(int(uid_for_emp))
                        except Exception:
                            pass

                        vals_emp_bdu = {
                            "device_id": device.id,
                            "user_id": uid_for_emp,
                            "name": nm_clean or uid_for_emp,
                            "password": "",
                            "privilege": '0',
                            "card_no": "",
                            "enabled": True,
                            "last_seen": False,  # -> needs_push
                            "employee_id": emp.id,
                        }
                        existing_bdu_for_emp.with_context(allow_single_word_name=True).write(vals_emp_bdu)
                        bdu_updated_for_emp += 1
                        # optional push below will use this uid
                    else:
                        # No BDU yet for this employee -> decide a UID
                        uid_for_emp = None
                        # Try link by name only when BOTH have >= 2 words
                        if len(_tokens(emp.name)) >= 2:
                            dev_matches = [
                                du for du in by_name.get(nm_key, [])
                                if len(_tokens(getattr(du, 'name', ''))) >= 2
                            ]
                            if dev_matches:
                                uid_for_emp = str(getattr(dev_matches[0], 'user_id'))

                        if not uid_for_emp:
                            uid_for_emp = str(next_free_uid())

                        vals_emp_bdu = {
                            "device_id": device.id,
                            "user_id": uid_for_emp,
                            "name": nm_clean or uid_for_emp,
                            "password": "",
                            "privilege": '0',
                            "card_no": "",
                            "enabled": True,
                            "last_seen": False,  # -> needs_push
                            "employee_id": emp.id,
                        }
                        Bdu_ctx_emp.create(vals_emp_bdu)
                        bdu_created_for_emp += 1

                    # Optional push
                    if push_new:
                        try:
                            conn.set_user(
                                uid=int(uid_for_emp),
                                name=emp.name or uid_for_emp,
                                privilege=0,
                                password="",
                                card=0,
                            )
                            pushed_new_users += 1
                            emp.write({'device_id_num': uid_for_emp})
                            rec_now = Bdu.search([
                                ('device_id', '=', device.id),
                                ('user_id', '=', uid_for_emp)
                            ], limit=1)
                            if rec_now:
                                rec_now.write({'last_seen': fields.Datetime.now()})
                        except Exception as e:
                            _logger.error("Failed to push user for employee %s on %s: %s",
                                          emp.name, device.name, e)

            finally:
                device._safe_disconnect(conn)

        # Final toast
        msg = "\n".join(filter(None, [
            _("Users sync completed."),
            _("BDU Created from device: %s") % created_bdu if created_bdu else "",
            _("BDU Updated from device: %s") % updated_bdu if updated_bdu else "",
            _("Employees created (bootstrap): %s") % emp_created if emp_created else "",
            _("Employees linked by ID: %s") % emp_linked_id if emp_linked_id else "",
            _("Employees linked by Name: %s") % emp_linked_name if emp_linked_name else "",
            _("BDU created for Odoo-only employees (needs push): %s") % bdu_created_for_emp if bdu_created_for_emp else "",
            _("BDU updated for Odoo-only employees: %s") % bdu_updated_for_emp if bdu_updated_for_emp else "",
            _("New users pushed to device now: %s") % pushed_new_users if pushed_new_users else "",
        ]))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": msg, "type": "success", "sticky": False},
        }

    def action_check_on_device(self):
        self.ensure_one()
        self.device_id._require_zk()
        conn = None
        try:
            conn = self.device_id._connect()
            conn.disable_device()
            zk_users = conn.get_users() or []
            found = any(str(u.user_id) == str(self.user_id) for u in zk_users)
            if found:
                self.sudo().write({'last_seen': fields.Datetime.now()})
        except Exception as e:
            raise UserError(_("Check on device failed: %s") % e)
        finally:
            self.device_id._safe_disconnect(conn)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": _("Checked on device."), "type": "success", "sticky": False},
        }

    def action_create_employee(self):
        """Link to an existing hr.employee or create a new one (two-word name required)."""
        self.ensure_one()
        if self.employee_id:
            raise UserError(_("This device user is already linked to %s.") % self.employee_id.name)

        uid_raw = (self.user_id or "").strip()
        if not uid_raw:
            raise UserError(_("This device user has no User ID."))

        full_name = self._ensure_two_word_name(self.name or uid_raw, field_label=_("Employee name"))
        name_key = self._name_key(full_name)

        HrEmployee = self.env['hr.employee'].sudo().with_context(active_test=False)

        # 1) by device_id_num (try raw and raw without leading zeros)
        emp = HrEmployee.search(['|',
                                 ('device_id_num', '=', uid_raw),
                                 ('device_id_num', '=', uid_raw.lstrip('0'))],
                                limit=1)
        if emp:
            if not emp.name or self._name_key(emp.name) == self._name_key(uid_raw):
                emp.write({'name': full_name})
            self.sudo().write({'employee_id': emp.id})
            return {
                "type": "ir.actions.act_window",
                "res_model": "biometric.device.user",
                "view_mode": "form",
                "res_id": self.id,
                "target": "current",
            }

        # 2) exact whole-name (=ilike) — no substrings
        emps_exact = HrEmployee.search([('name', '=ilike', full_name)])
        if len(emps_exact) == 1:
            emp = emps_exact
            if not emp.device_id_num:
                emp.write({'device_id_num': uid_raw})
            self.sudo().write({'employee_id': emp.id})
            return {
                "type": "ir.actions.act_window",
                "res_model": "biometric.device.user",
                "view_mode": "form",
                "res_id": self.id,
                "target": "current",
            }
        elif len(emps_exact) > 1:
            raise UserError(_("Multiple employees share the exact name '%s'. Please link manually.") % full_name)

        # 3) last chance: ilike candidates, then filter by normalized equality
        candidates = HrEmployee.search([('name', 'ilike', full_name)])
        matches = candidates.filtered(lambda e: self._name_key(e.name) == name_key)

        if len(matches) == 1:
            emp = matches
            if not emp.device_id_num:
                emp.write({'device_id_num': uid_raw})
            self.sudo().write({'employee_id': emp.id})
            return {
                "type": "ir.actions.act_window",
                "res_model": "biometric.device.user",
                "view_mode": "form",
                "res_id": self.id,
                "target": "current",
            }
        elif len(matches) > 1:
            raise UserError(_("Multiple normalized whole-name matches for '%s'. Please link manually.") % full_name)

        # 4) create new employee
        emp_new = HrEmployee.create({'name': full_name, 'device_id_num': uid_raw})
        self.sudo().write({'employee_id': emp_new.id})
        return {
            "type": "ir.actions.act_window",
            "res_model": "biometric.device.user",
            "view_mode": "form",
            "res_id": self.id,
            "target": "current",
        }

    def action_push_to_device(self):
        """
        Push/update users on the device with multiple fallbacks:
          - validate two-word name
          - sanitize to ASCII and <= 24 chars
          - try several set_user payload variants (different firmwares)
          - on failure: delete user + short wait + retry variants
          - verify by reading users that the UID exists before marking success
        """
        import time

        self._require_zk()

        def _sanitize_name(n: str) -> str:
            n = " ".join((n or "").strip().split()).title()
            n_ascii = n.encode("ascii", "ignore").decode("ascii")
            return n_ascii[:24] or "User"

        def _variants(uid_int: int, dev_name: str, priv_num: int, password: str, card_int: int, user_id_str: str):
            """
            Return a list of kwargs dicts to try with conn.set_user().
            Order matters: start with richer payloads, end with minimal.
            """
            pwd = password or ""
            v = [
                # 1) Full payload (works on many SFace/IFace)
                dict(uid=uid_int, name=dev_name, privilege=priv_num, password=pwd,
                     group_id=1, user_id=user_id_str, card=card_int),

                # 2) Full minus card (some fail on non-zero card)
                dict(uid=uid_int, name=dev_name, privilege=priv_num, password=pwd,
                     group_id=1, user_id=user_id_str),

                # 3) No group_id
                dict(uid=uid_int, name=dev_name, privilege=priv_num, password=pwd,
                     user_id=user_id_str, card=card_int),

                # 4) user_id as int (some firmwares oddly prefer this)
                dict(uid=uid_int, name=dev_name, privilege=priv_num, password=pwd,
                     group_id=1, user_id=uid_int, card=card_int),

                # 5) Minimal common signature
                dict(uid=uid_int, name=dev_name, privilege=priv_num, password=pwd),

                # 6) Minimal + card
                dict(uid=uid_int, name=dev_name, privilege=priv_num, password=pwd, card=card_int),
            ]
            # Ensure all names are present
            for d in v:
                d.setdefault("name", dev_name)
            return v

        for rec in self:
            # Validate name and sanitize for device
            valid_two_words = self._ensure_two_word_name(rec.name, field_label=_("Device user name"))
            dev_name = _sanitize_name(valid_two_words)

            # Prepare arguments
            try:
                uid_int = int(rec.user_id)
            except Exception:
                raise UserError(_("User ID must be numeric to push to the device: %s") % (rec.user_id,))

            user_id_str = str(uid_int)
            priv_num = 1 if rec.privilege == '1' else (2 if rec.privilege == '2' else 0)

            card_str = (rec.card_no or "").strip()
            try:
                card_int = int(card_str) if card_str else 0
            except Exception:
                card_int = 0

            conn = None
            try:
                conn = rec._connect()
                conn.disable_device()

                # Helper: attempt a list of variants
                def try_matrix(delete_first: bool = False) -> bool:
                    if delete_first:
                        try:
                            conn.delete_user(uid=uid_int)
                        except Exception:
                            pass
                        # small wait helps some models settle
                        time.sleep(0.2)

                    for payload in _variants(uid_int, dev_name, priv_num, rec.password or "", card_int, user_id_str):
                        try:
                            conn.set_user(**payload)
                            # verify
                            users = conn.get_users() or []
                            if any(str(u.user_id) == str(uid_int) for u in users):
                                return True
                            # Some firmwares store only internal uid; fallback verification
                            if any(getattr(u, 'uid', None) == uid_int for u in users):
                                return True
                        except Exception as e:
                            _logger.debug("set_user variant failed (%s) payload=%s", e, payload)
                            continue
                    return False

                # First try without deleting
                ok = try_matrix(delete_first=False)
                if not ok:
                    # Fallback: delete then re-create
                    ok = try_matrix(delete_first=True)

                if not ok:
                    raise UserError(_("Push user failed for [%s] on %s: Can't set user")
                                    % (rec.user_id, rec.device_id.name))

                # Success → mark as seen
                rec.sudo().write({'last_seen': fields.Datetime.now()})

            except UserError:
                raise
            except Exception as e:
                _logger.error("Push user failed [%s] on %s: %s", rec.user_id, rec.device_id.name, e)
                raise UserError(_("Push user failed for [%s] on %s: %s")
                                % (rec.user_id, rec.device_id.name, e))
            finally:
                try:
                    if conn:
                        try:
                            conn.enable_device()
                        except Exception:
                            pass
                finally:
                    rec.device_id._safe_disconnect(conn)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": _("User(s) pushed to device."), "type": "success", "sticky": False},
        }

    def action_delete_on_device(self):
        self._require_zk()
        for rec in self:
            conn = None
            try:
                conn = rec._connect()
                conn.disable_device()
                conn.delete_user(uid=int(rec.user_id))
                rec.sudo().write({'last_seen': False})
            except Exception as e:
                _logger.error("Delete user on device failed [%s] on %s: %s", rec.user_id, rec.device_id.name, e)
                raise UserError(_("Delete on device failed for [%s] on %s: %s") % (rec.user_id, rec.device_id.name, e))
            finally:
                rec.device_id._safe_disconnect(conn)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": _("User(s) deleted on device."), "type": "success", "sticky": False},
        }

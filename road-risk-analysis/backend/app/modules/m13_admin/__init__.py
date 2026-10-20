"""
Module 13 — Configuration, Audit, Reporting & Administration (RRAP-13).

  routes/platform_settings.py   /platform-settings — runtime settings sections
  routes/audit_logs.py          /audit-logs — audit trail (RRAP-13-006)
  models/                       system settings, audit log

The settings registry itself (app/core/settings_service.py) and the audit
helpers (app/core/audit.py) are shared by every module and live in core.
"""

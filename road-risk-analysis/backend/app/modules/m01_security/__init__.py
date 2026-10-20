"""
Module 1 — Platform Security, Access & User Management (RRAP-01).

  routes/auth.py, users.py, roles.py   /auth (login, MFA, password, mobile
                                       accounts, Google sign-in), /users, /roles
  auth_service.py                      sessions, role seeding, admin bootstrap
  captcha.py, google_signin.py         CAPTCHA verification, Google ID tokens
  models/user.py, auth_token.py        users / roles / permissions, sessions & tokens
  schemas.py                           auth, user, role, API-client and audit schemas

Shared security primitives (hashing, permission catalogue, request guards)
live in app/core/security.py, app/core/permissions.py and app/api/deps.py.
"""

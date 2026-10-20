"""
Functional modules of the platform, one package per module of the CAMTRACK
requirements (RRAP-NN-xxx = module NN). See backend/README.md for the map.

Each package follows the same layout (only what the module needs):
  routes/     FastAPI routers (mounted by app/api/router.py)
  models/     SQLAlchemy tables (registered in app/core/db/registry.py)
  schemas.py  pydantic request / response bodies
  *.py        services and domain logic; sub-packages for larger engines

Not built yet: Module 4 (Road Risk Assessment, ARR) and Module 9 (Driver
Behaviour Analytics) — no package until there is code.
"""

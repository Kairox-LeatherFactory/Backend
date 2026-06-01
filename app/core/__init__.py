"""
core — infrastructure shared by every module.

    config.py    environment-driven settings (sync + async DB URLs, JWT secret)
    database.py  the SYNC and ASYNC SQLAlchemy engines + session factories
    enums.py     centralised enumerations (UserRole, WageType, RunStatus, ShipMode)
    models.py    reusable mixins (UUID PK, timestamps) + portable GUID type
    security.py  self-issued JWT auth, password hashing, RBAC dependencies

Nothing here imports from app.modules, keeping the dependency graph acyclic.
"""

"""
Leather Factory Intelligence Platform — application package.

A modular FastAPI monolith that turns the factory's paper-and-WhatsApp workflow
into a real-time, traceable system. Layout:

    app/
      core/      cross-cutting infrastructure (config, db, enums, security, mixins)
      modules/   one self-contained package per business domain
      main.py    the FastAPI entrypoint that wires modules together

Persistence is async (asyncpg + SQLAlchemy 2.0 async) for the live API; a
separate sync engine backs Alembic migrations and scripts/seed.py.
"""

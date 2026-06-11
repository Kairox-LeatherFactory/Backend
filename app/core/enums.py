"""
================================================================================
core/enums.py — Centralised enumerations shared across every module
================================================================================

PURPOSE
    A single home for every fixed value-set the system uses. Roles, wage types,
    payroll-run statuses and ship modes were previously scattered across
    security.py and individual module models. Centralising them here means:
      - one import path everywhere: `from app.core.enums import UserRole`
      - no circular imports (this file imports nothing from the app)
      - the database, the auth layer, and the API schemas all agree on the exact
        same string values.

WHY str-Enums
    Each enum subclasses `str`, so `UserRole.MANAGER == "manager"` is True and the
    value serialises straight to JSON and stores cleanly in a Postgres column.

ROLE MODEL (maps directly onto the factory's org chart)
    DIRECT_MANAGER     Superuser. Sees everything, approves orders, manages users
                       and clients, runs payroll. (The "Direct Manager" in the
                       workflow doc, Stage 2.)
    CUTTING_MANAGER    Logs cutting-side production operations.
    STITCHING_MANAGER  Logs fusing -> lining-stitch -> final-finish operations.
    EMPLOYEE           A shop-floor worker. Can view their own work / wages.
    CLIENT             An external client logging in to track their own orders.
    VIEWER             Read-only office staff (accountant, HR) — no data entry.

    Adding a role = add a line here; the rest of the app picks it up.
================================================================================
"""
import enum


class UserRole(str, enum.Enum):
    DIRECT_MANAGER = "direct_manager"
    CUTTING_MANAGER = "cutting_manager"
    STITCHING_MANAGER = "stitching_manager"
    EMPLOYEE = "employee"
    CLIENT = "client"
    VIEWER = "viewer"
    SUPERVISOR = "supervisor"      # may PROXY check-in daily-wage workers + add them

    @classmethod
    def manager_roles(cls) -> set["UserRole"]:
        """Roles allowed to enter production data (besides the superuser)."""
        return {cls.CUTTING_MANAGER, cls.STITCHING_MANAGER}


class WageType(str, enum.Enum):
    """How an employee is paid. A property of the PERSON, set explicitly —
    NOT inferred from designation (TAILOR/CUTTER appear in both pay schemes)."""
    MONTHLY = "monthly"
    PIECE_RATE = "piece_rate"      # paid per piece produced; floor workers marked via attendance


class RunStatus(str, enum.Enum):
    """Lifecycle of a payroll run. A CLOSED run is a frozen snapshot."""
    OPEN = "open"
    CLOSED = "closed"


class ShipMode(str, enum.Enum):
    """How a finished purchase order leaves the factory. Sea = profitable,
    Air = margin-eroding (the central financial risk in the workflow)."""
    SEA = "sea"
    AIR = "air"

"""
users — the single, centralised authentication domain.

ONE table (app_user) logs in everyone — managers, shop-floor employees, external
clients, and read-only viewers — and the `role` column drives authorisation.
Employees and clients link to their role-specific records via employee_id /
client_id. The direct manager provisions client logins here.
"""

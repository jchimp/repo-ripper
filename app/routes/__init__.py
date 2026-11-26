"""HTTP routes, one module per area.

Every router except `auth_routes` and `health` is included in main.py with the
`require_user` dependency, so a new route is private unless it is added to one
of those two modules on purpose.
"""

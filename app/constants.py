ROLES = [
    "SUPER_ADMIN",
    "ADMIN",
    "DISTRICT_ADMIN",
    "BLOCK_ADMIN",
    "CLF_ADMIN",
    "CADRE_CC",
    "CLF_MANAGER",  # legacy role, kept temporarily for backward compatibility
    "PG_DATA_ENTRY",
    "VALIDATOR",
    "VIEWER",
]


VALIDATOR_LEVELS = [
    "state",
    "district",
    "block",
    "clf",
]


ROLE_LABELS = {
    "SUPER_ADMIN": "Super Admin",
    "ADMIN": "State Admin",
    "DISTRICT_ADMIN": "District Admin",
    "BLOCK_ADMIN": "Block Admin",
    "CLF_ADMIN": "CLF Admin",
    "CADRE_CC": "Cadre (CC)",
    "CLF_MANAGER": "CLF Manager",
    "PG_DATA_ENTRY": "PG Data Entry",
    "VALIDATOR": "Validator",
    "VIEWER": "Viewer",
}


ROLE_DESCRIPTIONS = {
    "SUPER_ADMIN": "System-level administrator with full access.",
    "ADMIN": "State-level administrator with state-wide monitoring and management access.",
    "DISTRICT_ADMIN": "District-level administrator with district-wide monitoring access.",
    "BLOCK_ADMIN": "Block-level administrator responsible for CLF creation, PG assignment, validation, and supervision.",
    "CLF_ADMIN": "CLF-level login responsible for operational management of assigned PGs.",
    "CADRE_CC": "Cadre or Community Coordinator with assigned PG access.",
    "CLF_MANAGER": "Legacy CLF manager role kept for older records.",
    "PG_DATA_ENTRY": "PG-level login responsible for PG data entry and form submissions.",
    "VALIDATOR": "Validation role for approval workflows.",
    "VIEWER": "Read-only viewer role.",
}


# Roles that can access admin-style dashboards
ADMIN_DASHBOARD_ROLES = [
    "SUPER_ADMIN",
    "ADMIN",
    "DISTRICT_ADMIN",
    "BLOCK_ADMIN",
]


# Roles that can access CLF dashboard
CLF_DASHBOARD_ROLES = [
    "CLF_ADMIN",
    "CLF_MANAGER",
]


# Roles that can access PG dashboard
PG_DASHBOARD_ROLES = [
    "PG_DATA_ENTRY",
    "CADRE_CC",
]


# Roles allowed to create PG master records.
# Important: CLF_ADMIN must NOT be added here.
PG_CREATION_ROLES = [
    "BLOCK_ADMIN",
    "DISTRICT_ADMIN",
    "ADMIN",
    "SUPER_ADMIN",
]


# Roles allowed to create CLF Admin login.
CLF_ADMIN_CREATION_ROLES = [
    "BLOCK_ADMIN",
]


# Roles allowed to assign PGs to CLF.
CLF_PG_ASSIGNMENT_ROLES = [
    "BLOCK_ADMIN",
]


# Roles allowed to approve/reject PG Registration and Membership Registration forms.
PG_REGISTRATION_VALIDATION_ROLES = [
    "BLOCK_ADMIN",
]


# Roles with mostly monitoring/surveillance access over CLF/PG hierarchy.
MONITORING_ROLES = [
    "SUPER_ADMIN",
    "ADMIN",
    "DISTRICT_ADMIN",
    "BLOCK_ADMIN",
]


# New registration validation statuses
VALIDATION_STATUSES = [
    "draft",
    "submitted",
    "approved",
    "rejected",
    "resubmitted",
]


VALIDATION_STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted",
    "approved": "Approved",
    "rejected": "Rejected",
    "resubmitted": "Resubmitted",
}


# Registration form types for validation workflow
REGISTRATION_FORM_TYPES = [
    "pg_registration",
    "member_registration",
]


REGISTRATION_FORM_TYPE_LABELS = {
    "pg_registration": "PG Registration",
    "member_registration": "Membership Registration",
}
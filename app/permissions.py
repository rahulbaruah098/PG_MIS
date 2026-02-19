"""Role → permission mapping for PG MIS.

Keep roles backwards compatible with existing `roles_required()` usage.
New code can use `permissions_required()` for fine-grained checks.
"""

# Canonical roles (existing + requested CRCTA/ANS/CRCITARD style)
ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ADMIN = "ADMIN"
ROLE_DISTRICT_ADMIN = "DISTRICT_ADMIN"
ROLE_BLOCK_ADMIN = "BLOCK_ADMIN"
ROLE_CLF_MANAGER = "CLF_MANAGER"
ROLE_PG_DATA_ENTRY = "PG_DATA_ENTRY"

# New / requested field-style roles (map to permissions)
ROLE_CRCTA = "CRCTA"        # Cluster Resource Coordinator (Tech/Agri)
ROLE_ANS = "ANS"            # Area/Agri/Accounts Support (project specific)
ROLE_CRCITARD = "CRCITARD"  # IT/ARD support role

# Permission strings
P_VIEW_DASHBOARD = "view:dashboard"
P_VIEW_PG = "view:pg"
P_CREATE_PG = "create:pg"
P_EDIT_PG = "edit:pg"
P_SUBMIT_PG = "submit:pg"
P_APPROVE_PG = "approve:pg"
P_UPLOAD_DOCS = "upload:documents"

P_FINANCE_EDIT = "edit:finance"
P_FINANCE_VIEW = "view:finance"
P_LOAN_EDIT = "edit:loans"

P_REPORT_VIEW = "view:reports"
P_REPORT_EXPORT = "export:reports"
P_MPR_GENERATE = "generate:mpr"

P_AUDIT_VIEW = "view:audit"
P_CHANGE_APPROVE = "approve:changes"
P_NOTIFICATIONS_VIEW = "view:notifications"

# Default permissions per role.
ROLE_PERMISSIONS = {
    ROLE_SUPER_ADMIN: {"*"},
    ROLE_ADMIN: {P_VIEW_DASHBOARD, P_VIEW_PG, P_CREATE_PG, P_EDIT_PG, P_SUBMIT_PG, P_APPROVE_PG,
                 P_UPLOAD_DOCS, P_FINANCE_VIEW, P_FINANCE_EDIT, P_LOAN_EDIT,
                 P_REPORT_VIEW, P_REPORT_EXPORT, P_MPR_GENERATE, P_AUDIT_VIEW, P_CHANGE_APPROVE, P_NOTIFICATIONS_VIEW},
    ROLE_DISTRICT_ADMIN: {P_VIEW_DASHBOARD, P_VIEW_PG, P_EDIT_PG, P_APPROVE_PG, P_UPLOAD_DOCS,
                          P_FINANCE_VIEW, P_REPORT_VIEW, P_REPORT_EXPORT, P_NOTIFICATIONS_VIEW},
    ROLE_BLOCK_ADMIN: {P_VIEW_DASHBOARD, P_VIEW_PG, P_EDIT_PG, P_UPLOAD_DOCS,
                       P_FINANCE_VIEW, P_REPORT_VIEW, P_NOTIFICATIONS_VIEW},
    ROLE_CLF_MANAGER: {P_VIEW_DASHBOARD, P_VIEW_PG, P_CREATE_PG, P_EDIT_PG, P_SUBMIT_PG, P_UPLOAD_DOCS,
                       P_FINANCE_VIEW, P_FINANCE_EDIT, P_LOAN_EDIT, P_REPORT_VIEW, P_NOTIFICATIONS_VIEW},
    ROLE_PG_DATA_ENTRY: {P_VIEW_PG, P_CREATE_PG, P_EDIT_PG, P_SUBMIT_PG, P_UPLOAD_DOCS,
                         P_FINANCE_VIEW, P_REPORT_VIEW, P_NOTIFICATIONS_VIEW},

    # Requested roles - conservative defaults (view + support, no approval)
    ROLE_CRCTA: {P_VIEW_DASHBOARD, P_VIEW_PG, P_EDIT_PG, P_UPLOAD_DOCS, P_REPORT_VIEW, P_NOTIFICATIONS_VIEW},
    ROLE_ANS: {P_VIEW_DASHBOARD, P_VIEW_PG, P_FINANCE_VIEW, P_REPORT_VIEW, P_REPORT_EXPORT, P_NOTIFICATIONS_VIEW},
    ROLE_CRCITARD: {P_VIEW_DASHBOARD, P_VIEW_PG, P_UPLOAD_DOCS, P_REPORT_VIEW, P_NOTIFICATIONS_VIEW},
}

def has_permission(role: str, perm: str) -> bool:
    perms = ROLE_PERMISSIONS.get(role, set())
    return "*" in perms or perm in perms

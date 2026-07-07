"""
app/core/page_registry.py

Single source of truth for which permission each protected page requires.

This is the clean design: a page REQUIRES a permission; a role GRANTS
permissions. "Which roles can access page X" is then derived — any role
whose permission dict contains X's required permission. No parallel
page->role table that could drift out of sync with the role definitions.

To gate a new page:
  1. Add an entry here.
  2. That's it — the access matrix and any permission check pick it up.

`required_permission = None` means "any authenticated user" (no special perm).
The 'admin' permission is a superuser key: a role with admin=True implicitly
satisfies every page (mirrors RoleChecker, where role 'admin' bypasses checks).
"""

from typing import Optional
from pydantic import BaseModel


class PageDefinition(BaseModel):
    key: str                          # stable id, e.g. "admin_analytics"
    label: str                        # human name, e.g. "Analytics Dashboard"
    path: str                         # route, e.g. "/admin/analytics"
    required_permission: Optional[str]  # permission key, or None for any user
    description: str


# The canonical list of protected pages. Order here = display order.
PAGE_REGISTRY: list[PageDefinition] = [
    PageDefinition(
        key="admin_control",
        label="Control Panel",
        path="/admin/control",
        required_permission="admin",
        description="Full moderation workspace — users, posts, roles, reports.",
    ),
    PageDefinition(
        key="admin_analytics",
        label="Analytics Dashboard",
        path="/admin/analytics",
        required_permission="view_analytics",
        description="Live platform metrics and snapshot trend charts.",
    ),
    PageDefinition(
        key="admin_activity",
        label="Admin Activity",
        path="/admin/activity",
        required_permission="view_audit",
        description="Audit trail of moderation actions.",
    ),
    PageDefinition(
        key="reports",
        label="Reports Queue",
        path="/admin/control#reports",
        required_permission="view_reports",
        description="Read and resolve user-submitted content reports.",
    ),
    PageDefinition(
        key="moderation",
        label="Content Moderation",
        path="/admin/control#posts",
        required_permission="moderate",
        description="Delete posts and comments, resolve reports.",
    ),
    PageDefinition(
        key="category_mgmt",
        label="Category Management",
        path="/admin/control#categories",
        required_permission="manage_tags",
        description="Approve or reject category suggestions.",
    ),
    PageDefinition(
        key="upload",
        label="Create Publication",
        path="/post/upload",
        required_permission="publish",
        description="Upload and publish a new map/visual.",
    ),
]


# The canonical list of permission keys the UI can toggle on a role.
# Keeps frontend and backend in lockstep (mirror of the frontend PERM_KEYS).
PERMISSION_CATALOG = [
    {"key": "admin",          "label": "Admin (superuser)",   "desc": "Full access to every page and action."},
    {"key": "publish",        "label": "Publish Posts",       "desc": "Create and edit own publications."},
    {"key": "moderate",       "label": "Moderate Content",    "desc": "Delete comments/posts, resolve reports."},
    {"key": "manage_tags",    "label": "Manage Categories",   "desc": "Approve or reject category suggestions."},
    {"key": "view_reports",   "label": "View Reports",        "desc": "Read access to the reports queue."},
    {"key": "view_analytics", "label": "View Analytics",      "desc": "Access the analytics dashboard."},
    {"key": "view_audit",     "label": "View Audit Log",      "desc": "Access the admin activity trail."},
]


def role_can_access(permissions: dict, required: Optional[str]) -> bool:
    """
    Does a role with the given permissions dict satisfy a page requirement?
    - No requirement -> any authenticated user passes.
    - admin permission -> passes everything (superuser).
    - otherwise -> the specific permission must be truthy.
    """
    if required is None:
        return True
    if permissions.get("admin") is True:
        return True
    return permissions.get(required) is True
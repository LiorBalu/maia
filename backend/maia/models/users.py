"""Dashboard users: roles (staff / user), tenant access and password history."""

from typing import Dict, List, Optional

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    EmailField,
    EmbeddedDocument,
    EmbeddedDocumentField,
    ListField,
    StringField,
)
from odmantic import Field, Model
from pydantic import BaseModel


class User(Model):
    """odmantic view of a dashboard user (used by the Basic Auth of the docs)."""
    email: str = Field(unique=True)
    password: str
    is_staff: bool = False
    role: str = "user"
    name: str = "name"
    phone_number: Optional[str] = None
    tenants_name: List[str] = []

    model_config = {"collection": "users"}


# Password history
class PasswordHistoryEntry(EmbeddedDocument):
    """A previous password hash (reuse check)."""
    hash = StringField(required=True)


STAFF_ROLE = "staff"
TENANT_MEMBER_ROLE = "user"
ALLOWED_USER_ROLES = (STAFF_ROLE, TENANT_MEMBER_ROLE)


class UserDocument(Document):
    """A dashboard user: email, password hash, role (staff / user), tenant permissions and password history.
    """
    name = StringField(required=False, default="name")
    email = EmailField(required=True, unique=True)
    password = StringField(required=True)
    is_staff = BooleanField(default=False)
    # "staff" = internal operator (full access), "user" = tenant member.
    # `is_staff` mirrors `role == "staff"` so JWT consumers and permission checks can test one boolean.
    role = StringField(choices=ALLOWED_USER_ROLES, default=TENANT_MEMBER_ROLE)
    tenants_name = ListField(StringField())
    phone_number = StringField(required=False)
    color = StringField(default="#e5c7eb", description="Color associated with the user, used for UI purposes")

    permissions = DictField(ListField(StringField()))
    last_login = StringField(default="", description="Last login time of the user, stored as a string in ISO format")

    password_history = ListField(EmbeddedDocumentField(PasswordHistoryEntry), default=[])
    password_last_changed_at = DateTimeField()

    meta = {"collection": "users"}

    def _build_full_access_permissions(self) -> Dict[str, List[str]]:
        """Full access (grant_access + min_access) on every tenant of tenants_name, keyed by tenant name."""
        return {member_tenant_name: ["grant_access", "min_access"] for member_tenant_name in self.tenants_name}

    def __init__(self, *args, **kwargs):
        """Default the permissions to full access on tenants_name, and promote is_staff=True to the staff role."""
        super().__init__(*args, **kwargs)
        if not self.permissions:
            self.permissions = self._build_full_access_permissions()
        if kwargs.get("role") is None and kwargs.get("is_staff"):
            self.role = STAFF_ROLE

    def _normalize_email(self):
        """Store the email stripped and lowercased (login and every lookup normalize to lowercase)."""
        if self.email:
            self.email = self.email.strip().lower()

    def _reconcile_role_and_staff_flag(self):
        """Reset an unknown role to the member role, promote a member carrying is_staff=True to staff, then derive
        is_staff from the role (this order is load-bearing).
        """
        if self.role not in ALLOWED_USER_ROLES:
            self.role = TENANT_MEMBER_ROLE
        if self.is_staff and self.role == TENANT_MEMBER_ROLE:
            self.role = STAFF_ROLE
        self.is_staff = self.role == STAFF_ROLE

    def clean(self):
        """Keep `role` and the `is_staff` flag consistent.

        A document that carries `is_staff=True` with the default role is promoted to staff; in
        every other case the explicit role is the source of truth and `is_staff` is derived from it.

        Emails are stored lowercase: login and every lookup normalize to lowercase, so a
        mixed-case document would be unreachable (impossible login and password reset).
        """
        self._normalize_email()
        self._reconcile_role_and_staff_flag()

    meta = {"collection": "users"}


class UserRequest(BaseModel):
    """Body of user registration."""
    name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    phone_number: Optional[str] = None
    color: Optional[str] = None
    is_staff: Optional[bool] = False
    role: Optional[str] = None
    permissions: Optional[Dict[str, List[str]]] = None
    tenants_name: Optional[List[str]] = []

    class Config:
        """Example payload shown in the API docs."""
        json_schema_extra = {
            "example": {
                "email": "sun@maia.local",
                "password": "123456",
                "name": "name",
                "phone_number": "972500000000",
                "is_staff": False,
                "permissions": {},
                "tenants_name": ["sun-cosmetics"],
            }
        }

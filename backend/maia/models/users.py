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


ROLE_STAFF = "staff"
ROLE_USER = "user"
USER_ROLES = (ROLE_STAFF, ROLE_USER)


class UserDocument(Document):
    """A dashboard user: email, password hash, role (staff / user), tenant permissions and password history.
    """
    name = StringField(required=False, default="name")
    email = EmailField(required=True, unique=True)
    password = StringField(required=True)
    is_staff = BooleanField(default=False)
    # "staff" = internal operator (full access), "user" = tenant member.
    # `is_staff` mirrors `role == "staff"` so JWT consumers and permission checks can test one boolean.
    role = StringField(choices=USER_ROLES, default=ROLE_USER)
    tenants_name = ListField(StringField())
    phone_number = StringField(required=False)
    color = StringField(default="#e5c7eb", description="Color associated with the user, used for UI purposes")

    permissions = DictField(ListField(StringField()))
    last_login = StringField(default="", description="Last login time of the user, stored as a string in ISO format")

    password_history = ListField(EmbeddedDocumentField(PasswordHistoryEntry), default=[])
    password_last_changed_at = DateTimeField()

    meta = {"collection": "users"}

    def __init__(self, *args, **kwargs):
        """Default the permissions to full access on tenants_name, and promote is_staff=True to the staff role."""
        super().__init__(*args, **kwargs)
        if not self.permissions:
            self.permissions = {tenant_name: ["grant_access", "min_access"] for tenant_name in self.tenants_name}
        if kwargs.get("role") is None and kwargs.get("is_staff"):
            self.role = ROLE_STAFF

    def clean(self):
        """Keep `role` and the `is_staff` flag consistent.

        A document that carries `is_staff=True` with the default role is promoted to staff; in
        every other case the explicit role is the source of truth and `is_staff` is derived from it.

        Emails are stored lowercase: login and every lookup normalize to lowercase, so a
        mixed-case document would be unreachable (impossible login and password reset).
        """
        if self.email:
            self.email = self.email.strip().lower()
        if self.role not in USER_ROLES:
            self.role = ROLE_USER
        if self.is_staff and self.role == ROLE_USER:
            self.role = ROLE_STAFF
        self.is_staff = self.role == ROLE_STAFF

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

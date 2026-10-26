"""Authentication and authorization dependencies: JWT validation, roles, which tenants a user may read or administer."""

import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, OAuth2PasswordBearer, SecurityScopes
from jose import JWTError, jwt
from werkzeug.security import check_password_hash

from maia.models.users import ROLE_PARTNER, ROLE_USER, User, UserDocument
from maia.models.tenants import TenantDocument
from maia.config import ALGORITHM, JWT_SECRET

TENANT_NAME_REQUIRED_DETAIL = "You need to specify a tenant name"


def user_manages_tenant(user, tenant_name: Optional[str]) -> bool:
    """Return True if the user administers `tenant_name` without being a member of it.

    Staff always manage every tenant. A partner manages only the tenants he created
    (``creator_email``) or that were assigned to him (``managed_by``) — he is
    deliberately absent from the tenant's ``access_users`` / his own ``permissions``.
    """
    if getattr(user, "is_staff", False):
        return True
    if not tenant_name or getattr(user, "role", None) != ROLE_PARTNER:
        return False
    tenant = TenantDocument.objects(name=tenant_name).only("creator_email", "managed_by").first()
    if not tenant:
        return False
    return tenant.creator_email == user.email or user.email in (tenant.managed_by or [])


def user_managed_tenant_names(user) -> list:
    """Tenant names a partner administers. Empty for non-partners (staff use a full query)."""
    if getattr(user, "role", None) != ROLE_PARTNER:
        return []
    from mongoengine.queryset.visitor import Q

    tenants = TenantDocument.objects(Q(creator_email=user.email) | Q(managed_by=user.email)).only("name")
    return [tenant.name for tenant in tenants]


def administrable_tenant_names(user) -> Optional[set]:
    """Tenants whose users the actor may administer. ``None`` means unrestricted (staff)."""
    if getattr(user, "is_staff", False):
        return None
    if getattr(user, "role", None) == ROLE_PARTNER:
        return set(user_managed_tenant_names(user))
    return set((user.permissions or {}).keys())


def assert_can_administer_user(actor, tenants: Optional[list] = None, requested_role: Optional[str] = None) -> None:
    """Guard against privilege escalation when a non-staff actor manages users.

    A partner (or a member holding ``grant_access``) may only create/update users that are
    plain tenant members, and only inside the tenants he administers. Only staff can mint
    staff or partner accounts.
    """
    if getattr(actor, "is_staff", False):
        return
    if requested_role and requested_role != ROLE_USER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not authorized to grant this role")
    allowed = administrable_tenant_names(actor)
    out_of_scope = sorted({tenant for tenant in (tenants or []) if tenant not in allowed})
    if out_of_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not administer the following tenants: {', '.join(out_of_scope)}",
        )


security = HTTPBasic()


# Dependencies for JWT and Basic Auth
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


async def authenticate_user_token(
    token: str = Depends(oauth2_scheme), tenant_name: Optional[str] = None
) -> UserDocument:  # TODO add here if user is not own of the tenant name
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        # Emails are stored lowercase; tokens minted before the normalization may carry mixed case.
        user = UserDocument.objects(email=email.lower() if email else email).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if tenant_name:
            if not user.is_staff:
                if tenant_name not in list(user.permissions.keys()) and not user_manages_tenant(user, tenant_name):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this tenant"
                    )

        return user
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_token(
    token: str = Depends(oauth2_scheme), tenant_name: Optional[str] = None
) -> UserDocument:  # TODO add here if user is not own of the tenant name
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        # Emails are stored lowercase; tokens minted before the normalization may carry mixed case.
        user = UserDocument.objects(email=email.lower() if email else email).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if tenant_name:
            if not user.is_staff:
                if tenant_name not in list(user.permissions.keys()) and not user_manages_tenant(user, tenant_name):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this tenant"
                    )

        return user
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _passes_without_tenant_scope(user: UserDocument, security_scopes: SecurityScopes, tenant_name: Optional[str]) -> bool:
    """Return True when the actor is granted access before any tenant-level check."""
    if user.is_staff and "staff" in security_scopes.scopes:
        return True
    # A partner has full authority over the tenants he administers, and passes any endpoint
    # explicitly opened to partners (tenant creation, listing his own portfolio).
    if user.is_partner and tenant_name and user_manages_tenant(user, tenant_name):
        return True
    return user.is_partner and "partner" in security_scopes.scopes


def _resolve_tenant_name(
    user: UserDocument, security_scopes: SecurityScopes, tenant_name: Optional[str]
) -> Optional[str]:
    """Fill in the implicit tenant of a single-tenant member, or reject an ambiguous request."""
    if tenant_name or "staff" in security_scopes.scopes:
        return tenant_name
    if user.is_staff or len(user.tenants_name) > 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=TENANT_NAME_REQUIRED_DETAIL)
    if len(user.tenants_name) != 1:
        return tenant_name
    resolved = user.tenants_name[0]
    if not isinstance(resolved, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=TENANT_NAME_REQUIRED_DETAIL)
    return resolved


def _assert_scopes_granted(user: UserDocument, security_scopes: SecurityScopes, tenant_name: Optional[str]) -> None:
    """Reject the request when the member lacks any of the required tenant permissions."""
    if not security_scopes.scopes:
        return
    # No implicit grant: reaching this point without an entry for the tenant means no rights.
    user_permissions = user.permissions.get(tenant_name, [])
    required = [sc for sc in security_scopes.scopes if sc != "staff"]
    if "grant_access" in user_permissions or all(perm in user_permissions for perm in required):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have the required permissions")


def verify_token_extended(
    security_scopes: SecurityScopes, token: str = Depends(oauth2_scheme), tenant_name: Optional[str] = None
) -> UserDocument:
    user = verify_token(token, tenant_name)

    if _passes_without_tenant_scope(user, security_scopes, tenant_name):
        return user

    tenant_name = _resolve_tenant_name(user, security_scopes, tenant_name)

    # Staff keep unrestricted access on tenant-scoped endpoints declared without the "staff" scope.
    if user.is_staff:
        return user

    _assert_scopes_granted(user, security_scopes, tenant_name)
    return user


async def verify_basic_auth(credentials: HTTPBasicCredentials = Depends(security)):
    from maia.services.mongo import manager_engine

    user = await manager_engine.find_one(User, User.email == credentials.username)
    if user is None:
        if credentials.username == os.getenv("MAIA_DOCS_USER") and credentials.password == os.getenv("MAIA_DOCS_PASSWORD"):
            return User(email=os.getenv("MAIA_DOCS_USER"), password=os.getenv("MAIA_DOCS_PASSWORD"), is_staff=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not check_password_hash(user.password, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

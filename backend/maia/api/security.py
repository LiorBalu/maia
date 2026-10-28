"""Authentication and authorization dependencies: JWT validation, roles, which tenants a user may read or administer."""

import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, OAuth2PasswordBearer, SecurityScopes
from jose import JWTError, jwt
from werkzeug.security import check_password_hash

from maia.models.users import ROLE_USER, User, UserDocument
from maia.config import ALGORITHM, JWT_SECRET

TENANT_NAME_REQUIRED_DETAIL = "You need to specify a tenant name"


def administrable_tenant_names(user) -> Optional[set]:
    """Tenants whose users the actor may administer. ``None`` means unrestricted (staff)."""
    if getattr(user, "is_staff", False):
        return None
    return set((user.permissions or {}).keys())


def assert_can_administer_user(actor, tenants: Optional[list] = None, requested_role: Optional[str] = None) -> None:
    """Guard against privilege escalation when a non-staff actor manages users.

    A member holding ``grant_access`` may only create/update users that are plain tenant
    members, and only inside the tenants they administer. Only staff can mint staff accounts.
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
) -> UserDocument:
    """FastAPI dependency: decode the Bearer JWT and load the user; with a tenant_name, a non-staff user must have
    access to that tenant (permissions).
    """
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
                if tenant_name not in list(user.permissions.keys()):
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
) -> UserDocument:
    """Same check as authenticate_user_token, as a sync function (used by verify_token_extended)."""
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
                if tenant_name not in list(user.permissions.keys()):
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


def _passes_without_tenant_scope(user: UserDocument, security_scopes: SecurityScopes) -> bool:
    """Return True when the actor is granted access before any tenant-level check (staff on a staff endpoint)."""
    return user.is_staff and "staff" in security_scopes.scopes


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
    """FastAPI dependency with scopes: staff pass everywhere, a member needs the required permissions on the tenant
    (implicit tenant for single-tenant members).
    """
    user = verify_token(token, tenant_name)

    if _passes_without_tenant_scope(user, security_scopes):
        return user

    tenant_name = _resolve_tenant_name(user, security_scopes, tenant_name)

    # Staff keep unrestricted access on tenant-scoped endpoints declared without the "staff" scope.
    if user.is_staff:
        return user

    _assert_scopes_granted(user, security_scopes, tenant_name)
    return user


async def verify_basic_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Basic Auth of the API docs: a dashboard user, or the MAIA_DOCS_USER / MAIA_DOCS_PASSWORD pair from the
    environment.
    """
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

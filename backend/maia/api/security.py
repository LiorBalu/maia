"""Authentication and authorization dependencies: JWT validation, roles, which tenants a user may read or administer."""

import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, OAuth2PasswordBearer, SecurityScopes
from jose import JWTError, jwt
from werkzeug.security import check_password_hash

from maia.models.users import TENANT_MEMBER_ROLE, User, UserDocument
from maia.config import JWT_SIGNING_ALGORITHM, JWT_SIGNING_SECRET

MISSING_TENANT_NAME_DETAIL = "You need to specify a tenant name"


def collect_administrable_tenant_names(acting_user) -> Optional[set]:
    """Tenants whose users the actor may administer. ``None`` means unrestricted (staff)."""
    if getattr(acting_user, "is_staff", False):
        return None
    return set((acting_user.permissions or {}).keys())


def verify_can_administer_user(acting_user, tenant_names: Optional[list] = None, role_to_grant: Optional[str] = None) -> None:
    """Guard against privilege escalation when a non-staff actor manages users.

    A member holding ``grant_access`` may only create/update users that are plain tenant
    members, and only inside the tenants they administer. Only staff can mint staff accounts.
    """
    if getattr(acting_user, "is_staff", False):
        return
    if role_to_grant and role_to_grant != TENANT_MEMBER_ROLE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not authorized to grant this role")
    permitted_tenant_names = collect_administrable_tenant_names(acting_user)
    out_of_scope_tenant_names = sorted({tenant_name for tenant_name in (tenant_names or []) if tenant_name not in permitted_tenant_names})
    if out_of_scope_tenant_names:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not administer the following tenants: {', '.join(out_of_scope_tenant_names)}",
        )


basic_auth_scheme = HTTPBasic()


# Dependencies for JWT and Basic Auth
bearer_token_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


async def authenticate_current_user(
    bearer_token: str = Depends(bearer_token_scheme), tenant_name: Optional[str] = None
) -> UserDocument:
    """FastAPI dependency: decode the Bearer JWT and load the user; with a tenant_name, a non-staff user must have
    access to that tenant (permissions).
    """
    return load_user_from_token(bearer_token, tenant_name)


def _load_user_from_token_payload(token_payload: dict) -> UserDocument:
    """The dashboard user named by the token's sub claim; 401 when no user has that email."""
    subject_email: str = token_payload.get("sub")
    # Emails are stored lowercase; tokens minted before the normalization may carry mixed case.
    current_user = UserDocument.objects(email=subject_email.lower() if subject_email else subject_email).first()
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


def _verify_tenant_access(current_user: UserDocument, tenant_name: Optional[str]) -> None:
    """With a tenant_name, a non-staff user needs that tenant among their permissions (403 otherwise); staff and
    requests without a tenant pass.
    """
    if tenant_name:
        if not current_user.is_staff:
            if tenant_name not in list(current_user.permissions.keys()):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this tenant"
                )


def load_user_from_token(
    bearer_token: str = Depends(bearer_token_scheme), tenant_name: Optional[str] = None
) -> UserDocument:
    """The check behind authenticate_current_user, as a sync function (also used by authorize_current_user)."""
    try:
        token_payload = jwt.decode(bearer_token, JWT_SIGNING_SECRET, algorithms=[JWT_SIGNING_ALGORITHM])
        current_user = _load_user_from_token_payload(token_payload)
        _verify_tenant_access(current_user, tenant_name)
        return current_user
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _is_staff_on_staff_endpoint(current_user: UserDocument, endpoint_scopes: SecurityScopes) -> bool:
    """Return True when the actor is granted access before any tenant-level check (staff on a staff endpoint)."""
    return current_user.is_staff and "staff" in endpoint_scopes.scopes


def _resolve_effective_tenant_name(
    current_user: UserDocument, endpoint_scopes: SecurityScopes, requested_tenant_name: Optional[str]
) -> Optional[str]:
    """Fill in the implicit tenant of a single-tenant member, or reject an ambiguous request."""
    if requested_tenant_name or "staff" in endpoint_scopes.scopes:
        return requested_tenant_name
    if current_user.is_staff or len(current_user.tenants_name) > 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MISSING_TENANT_NAME_DETAIL)
    if len(current_user.tenants_name) != 1:
        return requested_tenant_name
    sole_tenant_name = current_user.tenants_name[0]
    if not isinstance(sole_tenant_name, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MISSING_TENANT_NAME_DETAIL)
    return sole_tenant_name


def _verify_tenant_permissions_granted(current_user: UserDocument, endpoint_scopes: SecurityScopes, effective_tenant_name: Optional[str]) -> None:
    """Reject the request when the member lacks any of the required tenant permissions."""
    if not endpoint_scopes.scopes:
        return
    # No implicit grant: reaching this point without an entry for the tenant means no rights.
    granted_permissions = current_user.permissions.get(effective_tenant_name, [])
    required_permissions = [scope for scope in endpoint_scopes.scopes if scope != "staff"]
    if "grant_access" in granted_permissions or all(permission in granted_permissions for permission in required_permissions):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have the required permissions")


def authorize_current_user(
    endpoint_scopes: SecurityScopes, bearer_token: str = Depends(bearer_token_scheme), tenant_name: Optional[str] = None
) -> UserDocument:
    """FastAPI dependency with scopes: staff pass everywhere, a member needs the required permissions on the tenant
    (implicit tenant for single-tenant members).
    """
    current_user = load_user_from_token(bearer_token, tenant_name)

    if _is_staff_on_staff_endpoint(current_user, endpoint_scopes):
        return current_user

    effective_tenant_name = _resolve_effective_tenant_name(current_user, endpoint_scopes, tenant_name)

    # Staff keep unrestricted access on tenant-scoped endpoints declared without the "staff" scope.
    if current_user.is_staff:
        return current_user

    _verify_tenant_permissions_granted(current_user, endpoint_scopes, effective_tenant_name)
    return current_user


def _resolve_env_docs_user(basic_credentials: HTTPBasicCredentials) -> User:
    """The ad-hoc staff User of the MAIA_DOCS_USER / MAIA_DOCS_PASSWORD pair when the Basic Auth credentials match
    it; 401 otherwise (also when the pair is not set).
    """
    if basic_credentials.username == os.getenv("MAIA_DOCS_USER") and basic_credentials.password == os.getenv("MAIA_DOCS_PASSWORD"):
        return User(email=os.getenv("MAIA_DOCS_USER"), password=os.getenv("MAIA_DOCS_PASSWORD"), is_staff=True)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_docs_user(basic_credentials: HTTPBasicCredentials = Depends(basic_auth_scheme)):
    """Basic Auth of the API docs: a dashboard user, or the MAIA_DOCS_USER / MAIA_DOCS_PASSWORD pair from the
    environment.
    """
    from maia.services.mongo import odmantic_engine

    dashboard_user = await odmantic_engine.find_one(User, User.email == basic_credentials.username)
    if dashboard_user is None:
        return _resolve_env_docs_user(basic_credentials)
    if not check_password_hash(dashboard_user.password, basic_credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return dashboard_user

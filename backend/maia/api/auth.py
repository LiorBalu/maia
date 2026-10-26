"""Auth API: login, dashboard users (staff / partner / member), own profile, the OAuth2 token used by /docs."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from werkzeug.security import check_password_hash, generate_password_hash

from maia.models.users import User, UserDocument, UserRequest
from maia.models.tenants import TenantDocument
from maia.api.security import (
    administrable_tenant_names,
    assert_can_administer_user,
    authenticate_user_token,
    user_managed_tenant_names,
    verify_token_extended,
)
from maia.services.passwords import update_user_password
from maia.config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, JWT_SECRET

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/users", responses={403: {"description": "User not authorized"}})
async def get_users(u: User = Depends(authenticate_user_token)):
    if u.is_staff:
        users = UserDocument.objects()
    elif getattr(u, "is_partner", False):
        # A partner only sees the members of the tenants he administers.
        managed = user_managed_tenant_names(u)
        users = UserDocument.objects(is_staff=False, tenants_name__in=managed) if managed else []
    else:
        raise HTTPException(status_code=403, detail="User not authorized")

    def serialize_value(value):
        """Recursively serialize values, converting datetime to ISO format"""
        if isinstance(value, datetime):
            return value.isoformat()
        elif isinstance(value, list):
            return [serialize_value(item) for item in value]
        elif isinstance(value, dict):
            return {k: serialize_value(v) for k, v in value.items()}
        else:
            return value

    def serialize_user(user):
        user_dict = {key: value for key, value in user.to_mongo().to_dict().items() if key not in ["password", "_id"]}
        # Recursively convert datetime objects to ISO format strings
        return {key: serialize_value(value) for key, value in user_dict.items()}

    return JSONResponse(content=[serialize_user(user) for user in users])


def _validate_new_user_payload(user: UserRequest) -> None:
    """Reject an incomplete registration payload or a password that fails the policy."""
    if not user.name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not user.email:
        raise HTTPException(status_code=400, detail="Email is required")
    if not user.password:
        raise HTTPException(status_code=400, detail="Password is required")

    # Password validation: at least 8 characters, 1 uppercase letter, 1 special character
    import re

    password_regex = r'^(?=.*[A-Z])(?=.*[!@#$%^&*(),.?":{}|<>])[A-Za-z\d!@#$%^&*(),.?":{}|<>]{8,}$'
    if not re.match(password_regex, user.password):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least 8 characters, 1 uppercase letter and 1 special character",
        )


def _user_document_fields(user: UserRequest) -> dict:
    """Keep only real ``UserDocument`` fields from the request payload."""
    user_dict = user.model_dump(mode="python")
    if user_dict.get("role") is None:
        user_dict.pop("role", None)
    # Keep only real ``UserDocument`` fields.
    return {key: value for key, value in user_dict.items() if key in UserDocument._fields}


@router.post("/register")
async def create_user(
    user: UserRequest, u: UserDocument = Security(verify_token_extended, scopes=["staff", "partner", "grant_access"])
):
    _validate_new_user_payload(user)

    if user.tenants_name:
        tenants = TenantDocument.objects(name__in=user.tenants_name)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found")
    # A partner (or a member with grant_access) may only create plain members of his own tenants.
    requested_role = user.role or ("staff" if user.is_staff else None)
    assert_can_administer_user(u, list(user.tenants_name or []) + list((user.permissions or {}).keys()), requested_role)
    if not u.is_staff:
        user.is_staff = False
        user.role = "user"
    password = generate_password_hash(user.password)
    user_dict = _user_document_fields(user)
    user_dict["email"] = user_dict["email"].strip().lower()
    user_doc = UserDocument.objects(email=user_dict["email"]).first()
    if user_doc:
        raise HTTPException(status_code=400, detail="User already exists")
    user_doc = UserDocument(**user_dict)
    user_doc.password = password
    user_doc.password_last_changed_at = datetime.utcnow()
    user_doc.save()
    if user.tenants_name:
        for tenant in tenants:
            tenant.access_users.append(user_doc.email)
            tenant.save()
            tenant.reload()
    # user_model = User(password=password, email=user.email, is_staff=False)
    # await manager_engine.save(user_model)
    token = create_access_token(data=_build_user_token_data(user_doc))
    return {"token": token, "user": user}


@router.delete("/users/{email}")
async def delete_user(
    email: str, u: UserDocument = Security(verify_token_extended, scopes=["staff", "partner", "grant_access"])
):
    user_instance = UserDocument.objects(email=email.lower()).first()
    if not user_instance:
        raise HTTPException(status_code=404, detail="User not found")
    if not u.is_staff:
        # A partner may remove plain members of the tenants he administers, nobody else.
        allowed_tenants = administrable_tenant_names(u) or set()
        is_own_member = bool(
            set(user_instance.permissions or {}).intersection(allowed_tenants)
            or set(user_instance.tenants_name or []).intersection(allowed_tenants)
        )
        if not u.is_partner or user_instance.is_staff or user_instance.is_partner or not is_own_member:
            raise HTTPException(status_code=403, detail="User not authorized")
    user_instance.delete()
    return {"message": "User deleted"}


class LoginRequest(BaseModel):
    email: str
    password: str


class UserProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    color: Optional[str] = None
    password: Optional[str] = None


@router.put("/profile")
async def update_profile(profile_data: UserProfileUpdateRequest, u: User = Depends(authenticate_user_token)):
    """Allow users to update their own profile information"""
    user_instance = UserDocument.objects(email=u.email).first()
    if not user_instance:
        raise HTTPException(status_code=404, detail="User not found")

    # Update only allowed fields
    update_data = profile_data.dict(exclude_unset=True)

    for field, value in update_data.items():
        # Password change handled by password service
        if field == "password" and value:
            update_user_password(user_instance, value)
        elif field in ["name", "phone_number", "color"]:
            setattr(user_instance, field, value)

    user_instance.save()

    # Generate a new token with updated user data
    token = create_access_token(data=_build_user_token_data(user_instance))

    # Return updated user data (excluding sensitive information)
    user_dict = user_instance.to_mongo().to_dict()
    user_dict.pop("password", None)
    user_dict.pop("_id", None)

    return {"token": token, "user": user_dict}


# Utility function to create JWT tokens
@router.post("/login")
async def login_email_password(user: LoginRequest):
    """Email + password login; returns the JWT the dashboard sends as Bearer."""
    user_doc = UserDocument.objects(email=user.email.lower()).first()
    if not user_doc or not check_password_hash(user_doc.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_doc.last_login = str(datetime.now(timezone.utc))
    user_doc.save()
    token = create_access_token(data=_build_user_token_data(user_doc))
    return {"token": token, "user": user_doc.to_json(), "device_verified": True}


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)
    return encoded_jwt


def _build_user_token_data(user_doc) -> dict:
    """Build JWT payload with identity, role, and tenant permissions.

    Keeping `tenants` and `permissions` in the token lets any service that
    verifies the same JWT authorize without a DB roundtrip.
    """
    return {
        "sub": user_doc.email,
        "is_staff": user_doc.is_staff,
        # "staff" | "partner" | "user". The list of tenants a partner administers is
        # deliberately NOT embedded: it changes on every tenant creation and would go stale.
        "role": getattr(user_doc, "role", None) or ("staff" if user_doc.is_staff else "user"),
        "tenants": list(user_doc.tenants_name or []),
        "permissions": dict(user_doc.permissions or {}),
    }


@router.post("/token", include_in_schema=False)
async def get_token(form_data: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 password flow token endpoint — used by API docs UIs (Elements, RapiDoc)."""
    user_doc = UserDocument.objects(email=form_data.username.lower()).first()
    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not check_password_hash(user_doc.password, form_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data=_build_user_token_data(user_doc))
    return {"access_token": access_token, "token_type": "bearer"}

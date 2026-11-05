"""Auth API: login, dashboard users (staff / member), own profile, the OAuth2 token used by /docs."""

import logging
import re
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
    verify_can_administer_user,
    authenticate_current_user,
    authorize_current_user,
)
from maia.services.passwords import rotate_user_password
from maia.config import ACCESS_TOKEN_LIFETIME_MINUTES, JWT_SIGNING_ALGORITHM, JWT_SIGNING_SECRET

router = APIRouter()
logger = logging.getLogger(__name__)


def _serialize_json_safe(value):
    """Recursively serialize values, converting datetime to ISO format"""
    if isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, list):
        return [_serialize_json_safe(item) for item in value]
    elif isinstance(value, dict):
        return {nested_key: _serialize_json_safe(nested_value) for nested_key, nested_value in value.items()}
    else:
        return value


def _extract_user_fields_without_password_and_id(user_doc: UserDocument) -> dict:
    """The stored fields of a user document without `password` (the hash) and `_id`; every other field stays,
    the password history included.
    """
    return {key: value for key, value in user_doc.to_mongo().to_dict().items() if key not in ["password", "_id"]}


def _serialize_user_record(user_doc: UserDocument) -> dict:
    """User document as a JSON-safe dict, without the password hash and the Mongo id."""
    user_fields = _extract_user_fields_without_password_and_id(user_doc)
    # Recursively convert datetime objects to ISO format strings
    return {key: _serialize_json_safe(value) for key, value in user_fields.items()}


@router.get("/users", responses={403: {"description": "User not authorized"}})
async def list_dashboard_users(current_user: User = Depends(authenticate_current_user)):
    """List the dashboard users (staff only; 403 for anyone else)."""
    if current_user.is_staff:
        user_docs = UserDocument.objects()
    else:
        raise HTTPException(status_code=403, detail="User not authorized")

    return JSONResponse(content=[_serialize_user_record(user_doc) for user_doc in user_docs])


# Password policy: at least 8 characters, 1 uppercase letter, 1 special character (the frontend mirrors it).
PASSWORD_POLICY_PATTERN = r'^(?=.*[A-Z])(?=.*[!@#$%^&*(),.?":{}|<>])[A-Za-z\d!@#$%^&*(),.?":{}|<>]{8,}$'


def verify_password_meets_policy(candidate_password: Optional[str]) -> None:
    """400 when the password is missing or fails the policy."""
    if not candidate_password:
        raise HTTPException(status_code=400, detail="Password is required")
    if not re.match(PASSWORD_POLICY_PATTERN, candidate_password):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least 8 characters, 1 uppercase letter and 1 special character",
        )


def verify_email_unregistered(raw_email: str) -> str:
    """The normalized email, or 400 when a dashboard user already has it."""
    normalized_email = raw_email.strip().lower()
    if UserDocument.objects(email=normalized_email).first():
        raise HTTPException(status_code=400, detail="User already exists")
    return normalized_email


def enroll_member_in_tenant(member_email: str, candidate_password: str, display_name: str, tenant_doc: TenantDocument) -> UserDocument:
    """Create a plain member (`role=user`) of one tenant and add it to the tenant's `access_users`.

    Used by tenant creation ("open a business with its owner's login"); the register route below handles the
    general case (roles, several tenants).
    """
    normalized_email = verify_email_unregistered(member_email)
    verify_password_meets_policy(candidate_password)
    user_doc = UserDocument(name=display_name or normalized_email, email=normalized_email, role="user", tenants_name=[tenant_doc.name])
    user_doc.password = generate_password_hash(candidate_password)
    user_doc.password_last_changed_at = datetime.utcnow()
    user_doc.save()
    if normalized_email not in (tenant_doc.access_users or []):
        tenant_doc.access_users.append(normalized_email)
        tenant_doc.save()
        tenant_doc.reload()
    return user_doc


def _verify_registration_payload(registration_request: UserRequest) -> None:
    """Reject an incomplete registration payload or a password that fails the policy."""
    if not registration_request.name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not registration_request.email:
        raise HTTPException(status_code=400, detail="Email is required")
    verify_password_meets_policy(registration_request.password)


def _extract_user_document_fields(registration_request: UserRequest) -> dict:
    """Keep only real ``UserDocument`` fields from the request payload."""
    request_fields = registration_request.model_dump(mode="python")
    if request_fields.get("role") is None:
        request_fields.pop("role", None)
    # Keep only real ``UserDocument`` fields.
    return {key: value for key, value in request_fields.items() if key in UserDocument._fields}


def _load_requested_tenants(tenant_names: Optional[list[str]]):
    """The tenants named in the registration request (a queryset); 404 when names were given but none exists,
    an empty list when no name was given.
    """
    if tenant_names:
        tenant_docs = TenantDocument.objects(name__in=tenant_names)
        if not tenant_docs:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return tenant_docs
    return []


def _authorize_registration_and_cap_role(current_user: UserDocument, registration_request: UserRequest) -> None:
    """403 when the actor may not create this user; a non-staff actor's request is capped in place to a plain
    member (is_staff=False, role='user'), which the saved user and the response then carry.
    """
    # A member with grant_access may only create plain members of their own tenants.
    requested_role = registration_request.role or ("staff" if registration_request.is_staff else None)
    verify_can_administer_user(current_user, list(registration_request.tenants_name or []) + list((registration_request.permissions or {}).keys()), requested_role)
    if not current_user.is_staff:
        registration_request.is_staff = False
        registration_request.role = "user"


def _persist_new_user(registration_request: UserRequest) -> UserDocument:
    """Save the new user from the request (password hashed, email stripped and lowercased); 400 when the email is
    taken.
    """
    password_hash = generate_password_hash(registration_request.password)
    user_fields = _extract_user_document_fields(registration_request)
    user_fields["email"] = user_fields["email"].strip().lower()
    existing_user_doc = UserDocument.objects(email=user_fields["email"]).first()
    if existing_user_doc:
        raise HTTPException(status_code=400, detail="User already exists")
    new_user_doc = UserDocument(**user_fields)
    new_user_doc.password = password_hash
    new_user_doc.password_last_changed_at = datetime.utcnow()
    new_user_doc.save()
    return new_user_doc


def _grant_tenant_access(tenant_docs, member_email: str) -> None:
    """Append the email to the access_users of each tenant (no duplicate check), saving and reloading the tenant."""
    for tenant_doc in tenant_docs:
        tenant_doc.access_users.append(member_email)
        tenant_doc.save()
        tenant_doc.reload()


@router.post("/register")
async def register_dashboard_user(
    registration_request: UserRequest, current_user: UserDocument = Security(authorize_current_user, scopes=["staff", "grant_access"])
):
    """Register a dashboard user (by staff or a member with grant_access): hash the password, link the user
    to the requested tenants and return a JWT.
    """
    _verify_registration_payload(registration_request)

    tenant_docs = _load_requested_tenants(registration_request.tenants_name)
    _authorize_registration_and_cap_role(current_user, registration_request)
    new_user_doc = _persist_new_user(registration_request)
    _grant_tenant_access(tenant_docs, new_user_doc.email)
    # user_model = User(password=password_hash, email=registration_request.email, is_staff=False)
    # await odmantic_engine.save(user_model)
    access_token = encode_access_token(claims=_build_jwt_claims(new_user_doc))
    return {"token": access_token, "user": registration_request}


class LoginRequest(BaseModel):
    """Body of POST /login."""
    email: str
    password: str


class UserProfileUpdateRequest(BaseModel):
    """Body of PUT /profile: the fields a user may change on his own account."""
    name: Optional[str] = None
    phone_number: Optional[str] = None
    color: Optional[str] = None
    password: Optional[str] = None


def _apply_profile_changes(user_doc: UserDocument, changed_fields: dict) -> None:
    """Apply the submitted fields: the password through rotate_user_password (which saves the user), name /
    phone_number / color by setattr; any other field is ignored.
    """
    for field_name, field_value in changed_fields.items():
        # Password change handled by password service
        if field_name == "password" and field_value:
            rotate_user_password(user_doc, field_value)
        elif field_name in ["name", "phone_number", "color"]:
            setattr(user_doc, field_name, field_value)


@router.put("/profile")
async def update_own_profile(profile_update: UserProfileUpdateRequest, current_user: User = Depends(authenticate_current_user)):
    """Allow users to update their own profile information"""
    user_doc = UserDocument.objects(email=current_user.email).first()
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Update only allowed fields
    changed_fields = profile_update.dict(exclude_unset=True)

    _apply_profile_changes(user_doc, changed_fields)

    user_doc.save()

    # Generate a new token with updated user data
    access_token = encode_access_token(claims=_build_jwt_claims(user_doc))

    # Return updated user data (excluding sensitive information)
    user_fields = _extract_user_fields_without_password_and_id(user_doc)

    return {"token": access_token, "user": user_fields}


def _authenticate_credentials(email: str, plain_password: str, failure_detail: str) -> UserDocument:
    """The dashboard user with this email (lowercased) whose password hash matches; 401 with failure_detail and the
    Bearer challenge otherwise.
    """
    user_doc = UserDocument.objects(email=email.lower()).first()
    if not user_doc or not check_password_hash(user_doc.password, plain_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=failure_detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_doc


@router.post("/login")
async def login_with_password(credentials: LoginRequest):
    """Email + password login; returns the JWT the dashboard sends as Bearer."""
    user_doc = _authenticate_credentials(credentials.email, credentials.password, "Incorrect email or password")
    user_doc.last_login = str(datetime.now(timezone.utc))
    user_doc.save()
    access_token = encode_access_token(claims=_build_jwt_claims(user_doc))
    return {"token": access_token, "user": user_doc.to_json()}


# Utility function to create JWT tokens
def encode_access_token(claims: dict, lifetime: Optional[timedelta] = None):
    """Sign a JWT with the given payload; it expires after ACCESS_TOKEN_LIFETIME_MINUTES unless lifetime is given.
    """
    token_payload = claims.copy()
    if lifetime:
        expires_at = datetime.utcnow() + lifetime
    else:
        expires_at = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_LIFETIME_MINUTES)
    token_payload.update({"exp": expires_at})
    signed_token = jwt.encode(token_payload, JWT_SIGNING_SECRET, algorithm=JWT_SIGNING_ALGORITHM)
    return signed_token


def _build_jwt_claims(subject_user_doc) -> dict:
    """Build JWT payload with identity, role, and tenant permissions.

    Keeping `tenants` and `permissions` in the token lets any service that
    verifies the same JWT authorize without a DB roundtrip.
    """
    return {
        "sub": subject_user_doc.email,
        "is_staff": subject_user_doc.is_staff,
        # "staff" | "user".
        "role": getattr(subject_user_doc, "role", None) or ("staff" if subject_user_doc.is_staff else "user"),
        "tenants": list(subject_user_doc.tenants_name or []),
        "permissions": dict(subject_user_doc.permissions or {}),
    }


@router.post("/token", include_in_schema=False)
async def issue_oauth2_password_token(oauth2_form: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 password flow token endpoint — used by the Swagger UI at /docs."""
    user_doc = _authenticate_credentials(oauth2_form.username, oauth2_form.password, "Incorrect username or password")
    access_token = encode_access_token(claims=_build_jwt_claims(user_doc))
    return {"access_token": access_token, "token_type": "bearer"}

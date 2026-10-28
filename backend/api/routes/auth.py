from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from backend.auth.dependencies import get_current_user
from backend.auth.rate_limit import rate_limit
from backend.auth.security import (
    create_access_token,
    generate_reset_token,
    hash_password,
    hash_reset_token,
    needs_rehash,
    verify_password,
)
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.api_models import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from backend.models.response_models import (
    ApiResponse,
    AuthResponse,
    ErrorResponse,
    ForgotPasswordResponse,
    UserResponse,
)

logger = get_logger(__name__)
router = APIRouter()

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Invalid credentials or missing/expired token"},
    409: {"model": ErrorResponse, "description": "Email already registered"},
    422: {"description": "Validation error"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


def _to_user_response(user: dict) -> UserResponse:
    return UserResponse(
        user_id=user["user_id"],
        email=user["email"],
        name=user["name"],
        created_at=user["created_at"],
    )


@router.post(
    "/register",
    response_model=ApiResponse[AuthResponse],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(rate_limit("register", limit=5, window_seconds=60))],
)
async def register(payload: RegisterRequest) -> ApiResponse[AuthResponse]:
    repository = ResearchRepository()

    existing = repository.users.get_by_email(payload.email)
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    password_hash, salt = hash_password(payload.password)
    user = repository.users.create(
        email=payload.email,
        name=payload.name,
        password_hash=password_hash,
        password_salt=salt,
    )
    token = create_access_token(user_id=user["user_id"], email=user["email"])

    logger.info("User registered", extra={"user_id": user["user_id"]})

    return ApiResponse(
        success=True,
        message="Account created.",
        data=AuthResponse(access_token=token, user=_to_user_response(user)),
    )


@router.post(
    "/login",
    response_model=ApiResponse[AuthResponse],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(rate_limit("login", limit=10, window_seconds=60))],
)
async def login(payload: LoginRequest) -> ApiResponse[AuthResponse]:
    repository = ResearchRepository()
    user = repository.users.get_by_email(payload.email)

    if not user or not verify_password(payload.password, user["password_hash"], user["password_salt"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    # Transparent upgrade path: a successful login against a legacy PBKDF2
    # hash (or a future weaker-than-target Argon2id hash) is re-hashed with
    # the current Argon2id parameters right here, using the plaintext
    # password we already have in hand for this one request.
    if needs_rehash(user["password_hash"]):
        new_hash, new_salt = hash_password(payload.password)
        repository.users.update_password_hash(user["user_id"], new_hash, new_salt)
        logger.info("Upgraded password hash to Argon2id", extra={"user_id": user["user_id"]})

    token = create_access_token(user_id=user["user_id"], email=user["email"])

    logger.info("User logged in", extra={"user_id": user["user_id"]})

    return ApiResponse(
        success=True,
        message="Logged in.",
        data=AuthResponse(access_token=token, user=_to_user_response(user)),
    )


@router.get("/me", response_model=ApiResponse[UserResponse], responses=ERROR_RESPONSES)
async def me(current_user: dict = Depends(get_current_user)) -> ApiResponse[UserResponse]:
    return ApiResponse(success=True, message="OK", data=_to_user_response(current_user))


_GENERIC_FORGOT_MESSAGE = "If an account exists for that email, a reset link is on its way."


@router.post(
    "/forgot-password",
    response_model=ApiResponse[ForgotPasswordResponse],
    dependencies=[Depends(rate_limit("forgot_password", limit=5, window_seconds=300))],
)
async def forgot_password(payload: ForgotPasswordRequest) -> ApiResponse[ForgotPasswordResponse]:
    """Always returns the same generic success response whether or not the
    email belongs to an account — the frontend already renders one message
    regardless (see script.js), and this is what makes that safe: an
    attacker probing emails gets no signal either way."""
    repository = ResearchRepository()
    settings = get_settings()
    user = repository.users.get_by_email(payload.email)
    dev_reset_token: str | None = None

    if user:
        raw_token = generate_reset_token()
        repository.password_resets.create(
            user_id=user["user_id"],
            token_hash=hash_reset_token(raw_token),
            expires_minutes=settings.password_reset_expire_minutes,
        )
        # No email provider is configured for this deployment (out of scope
        # for this phase — see the security report). The raw token is never
        # written to logs: log files are routinely shipped to aggregators
        # or retained far longer than a single response, so that would be
        # a worse leak surface than this. Instead, in development only, the
        # token is returned directly in the response so local/manual
        # testing works without an email provider. Staging/production get
        # neither a logged token nor a returned one — this endpoint can't
        # actually deliver a reset until real email delivery is wired up.
        if settings.app_env == "development":
            dev_reset_token = raw_token
        logger.info("Password reset requested", extra={"user_id": user["user_id"]})

    return ApiResponse(
        success=True,
        message=_GENERIC_FORGOT_MESSAGE,
        data=ForgotPasswordResponse(dev_reset_token=dev_reset_token),
    )


@router.post(
    "/reset-password",
    response_model=ApiResponse[None],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or already-used reset token"},
        410: {"model": ErrorResponse, "description": "Reset token has expired"},
    },
    dependencies=[Depends(rate_limit("reset_password", limit=10, window_seconds=300))],
)
async def reset_password(payload: ResetPasswordRequest) -> ApiResponse[None]:
    repository = ResearchRepository()
    record = repository.password_resets.get_by_hash(hash_reset_token(payload.token))

    if not record or record["used_at"] is not None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")

    if record["expires_at"] < datetime.utcnow().isoformat():
        raise HTTPException(status_code=410, detail="Reset token has expired.")

    password_hash, password_salt = hash_password(payload.new_password)
    repository.users.update_password_hash(record["user_id"], password_hash, password_salt)
    # Marked used only after the update succeeds, and looked up by hash
    # above — a token can't be replayed even if the request is retried.
    repository.password_resets.mark_used(record["token_id"])

    logger.info("Password reset completed", extra={"user_id": record["user_id"]})

    return ApiResponse(success=True, message="Password updated.", data=None)

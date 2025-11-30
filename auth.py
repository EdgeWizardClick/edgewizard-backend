import requests
import os
import json
import uuid
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import JWTError, jwt
from dotenv import load_dotenv
from credits_manager import clear_all_credits

# Optional: redis for production
try:
    from redis import Redis
except ImportError:
    Redis = None  # type: ignore


# ============================================================================
# Configuration
# ============================================================================

load_dotenv()

JWT_SECRET_KEY = os.getenv("JWT_SECRET")
if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET is not set. Please define it in .env / Railway ENV.")

JWT_ALGORITHM = "HS256"

try:
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))  # 7 days default
except ValueError:
    ACCESS_TOKEN_EXPIRE_MINUTES = 10080

PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = 30

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# For local development we default to a fake in-memory store.
# In production (Railway) you can set USE_FAKE_REDIS=0 to use real Redis.
USE_FAKE_REDIS = os.getenv("USE_FAKE_REDIS", "1") == "1"

_redis_client: Optional["Redis"] = None

# In-memory stores for local mode
_user_store: Dict[str, Dict[str, Any]] = {}             # user_id -> user data
_email_to_user_id: Dict[str, str] = {}                  # email -> user_id
_password_reset_tokens: Dict[str, Dict[str, Any]] = {}  # token -> {user_id, expires_at_iso}


def _get_redis_client() -> Optional["Redis"]:
    global _redis_client

    if USE_FAKE_REDIS:
        return None

    if Redis is None:
        return None

    if _redis_client is None:
        _redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


# Password hashing context (no bcrypt problems on Windows)
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

router = APIRouter()


# ============================================================================
# Models
# ============================================================================

class UserInStore(BaseModel):
    user_id: str
    email: EmailStr
    password_hash: str
    created_at: datetime
    last_login: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    stripe_customer_id: Optional[str] = None


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    user_id: str
    email: EmailStr
    created_at: datetime
    last_login: Optional[datetime] = None
    stripe_customer_id: Optional[str] = None


class RequestPasswordResetPayload(BaseModel):
    email: EmailStr


class ResetPasswordPayload(BaseModel):
    token: str
    new_password: str


class GenericMessageResponse(BaseModel):
    message: str


# ============================================================================
# Key helpers (for Redis mode)
# ============================================================================

def _key_user(user_id: str) -> str:
    return f"ew:user:{user_id}"


def _key_user_email(email: str) -> str:
    email_normalized = email.strip().lower()
    return f"ew:user_email:{email_normalized}"


def _key_password_reset_token(token: str) -> str:
    return f"ew:password_reset:{token}"


# ============================================================================
# Password & token helpers
# ============================================================================

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta is not None:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt


# ============================================================================
# User repository (Redis or in-memory)
# ============================================================================

def get_user_by_id(user_id: str) -> Optional[UserInStore]:
    client = _get_redis_client()

    if client is not None:
        key = _key_user(user_id)
        raw = client.get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
    else:
        data = _user_store.get(user_id)
        if not data:
            return None

    try:
        deleted_raw = data.get("deleted_at")
        return UserInStore(
            user_id=data["user_id"],
            email=data["email"],
            password_hash=data["password_hash"],
            created_at=datetime.fromisoformat(data["created_at"]),
            last_login=datetime.fromisoformat(data["last_login"]) if data.get("last_login") else None,
            deleted_at=datetime.fromisoformat(deleted_raw) if deleted_raw else None,
            stripe_customer_id=data.get("stripe_customer_id"),
        )
    except Exception:
        return None


def get_user_by_email(email: str) -> Optional[UserInStore]:
    client = _get_redis_client()
    email_normalized = email.strip().lower()

    if client is not None:
        email_key = _key_user_email(email_normalized)
        user_id = client.get(email_key)
        if not user_id:
            return None
        return get_user_by_id(user_id)
    else:
        user_id = _email_to_user_id.get(email_normalized)
        if not user_id:
            return None
        return get_user_by_id(user_id)


def save_user(user: UserInStore) -> None:
    client = _get_redis_client()

    data = {
        "user_id": user.user_id,
        "email": str(user.email),
        "password_hash": user.password_hash,
        "created_at": user.created_at.isoformat(),
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "deleted_at": user.deleted_at.isoformat() if user.deleted_at else None,
        "stripe_customer_id": user.stripe_customer_id,
    }

    if client is not None:
        key = _key_user(user.user_id)
        client.set(key, json.dumps(data))

        email_key = _key_user_email(str(user.email))
        client.set(email_key, user.user_id)
    else:
        _user_store[user.user_id] = data
        _email_to_user_id[str(user.email).strip().lower()] = user.user_id


def create_user(email: str, password: str) -> UserInStore:
    existing = get_user_by_email(email)
    if existing:
        raise ValueError("User with this email already exists.")

    now = datetime.utcnow()
    user = UserInStore(
        user_id=str(uuid.uuid4()),
        email=email.strip().lower(),
        password_hash=hash_password(password),
        created_at=now,
        last_login=now,
        stripe_customer_id=None,
    )
    save_user(user)
    return user


def update_user_last_login(user: UserInStore) -> None:
    user.last_login = datetime.utcnow()
    save_user(user)


# ============================================================================
# Password reset token handling
# ============================================================================

def create_password_reset_token_for_user(user: UserInStore) -> str:
    token = secrets.token_urlsafe(32)
    client = _get_redis_client()
    expires_seconds = PASSWORD_RESET_TOKEN_EXPIRE_MINUTES * 60

    if client is not None:
        key = _key_password_reset_token(token)
        client.set(key, user.user_id, ex=expires_seconds)
    else:
        expires_at = datetime.utcnow() + timedelta(seconds=expires_seconds)
        _password_reset_tokens[token] = {
            "user_id": user.user_id,
            "expires_at": expires_at.isoformat(),
        }

    return token


def get_user_id_by_password_reset_token(token: str) -> Optional[str]:
    client = _get_redis_client()

    if client is not None:
        key = _key_password_reset_token(token)
        return client.get(key)

    # in-memory mode
    entry = _password_reset_tokens.get(token)
    if not entry:
        return None
    expires_at_str = entry.get("expires_at")
    if not expires_at_str:
        return None
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except Exception:
        return None
    if datetime.utcnow() > expires_at:
        _password_reset_tokens.pop(token, None)
        return None
    return entry.get("user_id")


def delete_password_reset_token(token: str) -> None:
    client = _get_redis_client()
    if client is not None:
        key = _key_password_reset_token(token)
        client.delete(key)
    else:
        _password_reset_tokens.pop(token, None)


# ============================================================================
# Email sending placeholder
# ============================================================================

def send_password_reset_email(email: str, reset_token: str) -> None:
    """
    Send a real password reset email via MailerSend.

    - Builds a reset link based on APP_BASE_URL.
    - Uses MAILERSEND_API_KEY and MAILERSEND_FROM_EMAIL from the environment.
    """
    api_key = os.getenv("MAILERSEND_API_KEY")
    from_email = os.getenv("MAILERSEND_FROM_EMAIL", "no-reply@edgewizard.click")
    app_base_url = os.getenv("APP_BASE_URL", "https://wizardedge.preview.emergentagent.com")

    if not api_key:
        # For MVP we just log and return, endpoint bleibt trotzdem generisch.
        print("MAILERSEND_API_KEY is not set - cannot send password reset email.")
        return

    reset_link = f"{app_base_url.rstrip('/')}/password-reset?token={reset_token}"

    url = "https://api.mailersend.com/v1/email"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "from": {
            "email": from_email,
            "name": "EdgeWizard",
        },
        "to": [
            {
                "email": email,
            }
        ],
        "subject": "Reset your EdgeWizard password",
        "text": (
            "You requested a password reset for your EdgeWizard account.\n\n"
            f"Click the following link to set a new password:\n{reset_link}\n\n"
            "If you did not request this, you can ignore this email."
        ),
        "html": (
            "<p>You requested a password reset for your EdgeWizard account.</p>"
            f"<p><a href=\"{reset_link}\">Click here to reset your password</a></p>"
            "<p>If you did not request this, you can ignore this email.</p>"
        ),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code >= 400:
            print(f"MailerSend error: status={resp.status_code}, body={resp.text}")
    except Exception as e:
        print(f"Error while sending password reset email via MailerSend: {e}")


# ============================================================================
# Security dependency
# ============================================================================

async def get_current_user(token: str = Depends(oauth2_scheme)) -> UserInStore:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: Optional[str] = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_id(user_id)
    if user is None:
        raise credentials_exception

    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account has been deleted.",
        )

    return user


# ============================================================================
# Auth routes
# ============================================================================

@router.post("/signup", response_model=TokenResponse, summary="Register a new account")
async def signup(payload: SignupRequest):
    email = payload.email.strip().lower()
    password = payload.password

    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    try:
        user = create_user(email=email, password=password)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists.",
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        data={"sub": user.user_id, "email": str(user.email)},
        expires_delta=access_token_expires,
    )
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse, summary="Log in with email and password")
async def login(payload: LoginRequest):
    email = payload.email.strip().lower()
    password = payload.password

    user = get_user_by_email(email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is invalid.",
        )

    if getattr(user, "deleted_at", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account has been deleted.",
        )

    if not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is invalid.",
        )

    update_user_last_login(user)

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        data={"sub": user.user_id, "email": str(user.email)},
        expires_delta=access_token_expires,
    )
    return TokenResponse(access_token=token)


@router.get("/me", response_model=MeResponse, summary="Get current account information")
async def read_me(current_user: UserInStore = Depends(get_current_user)):
    return MeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        created_at=current_user.created_at,
        last_login=current_user.last_login,
        stripe_customer_id=current_user.stripe_customer_id,
    )


# ============================================================================
# Password reset routes
# ============================================================================

@router.post(
    "/request-password-reset",
    response_model=GenericMessageResponse,
    summary="Request password reset",
)
async def request_password_reset(payload: RequestPasswordResetPayload):
    email = payload.email.strip().lower()
    user = get_user_by_email(email)

    message = "If an account with this email exists, a password reset email has been sent."

    if not user:
        return GenericMessageResponse(message=message)

    reset_token = create_password_reset_token_for_user(user)
    send_password_reset_email(email=email, reset_token=reset_token)

    return GenericMessageResponse(message=message)


@router.post(
    "/reset-password",
    response_model=GenericMessageResponse,
    summary="Reset password using a valid token",
)
async def reset_password(payload: ResetPasswordPayload):
    token = payload.token.strip()
    new_password = payload.new_password

    if len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    user_id = get_user_id_by_password_reset_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is invalid or has expired.",
        )

    user = get_user_by_id(user_id)
    if not user:
        delete_password_reset_token(token)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is invalid or has expired.",
        )

    user.password_hash = hash_password(new_password)
    save_user(user)
    delete_password_reset_token(token)

    return GenericMessageResponse(message="Password has been updated successfully.")

def admin_reset_password(email: str, new_password: str):
    # Load existing user if present
    user = _find_user_by_email(email)
    if not user:
        raise Exception("User does not exist")

    user_id = user["user_id"]
    hashed = hash_password(new_password)

    user["password_hash"] = hashed
    user["last_password_reset"] = datetime.utcnow().isoformat()

    _save_user(user_id, user)
    return user_id


class DeleteAccountRequest(BaseModel):
    confirmation_text: str


@router.post("/me/delete-account")
def delete_own_account(
    payload: DeleteAccountRequest,
    current_user: UserInStore = Depends(get_current_user),
):
    """
    Soft-delete the current user account.
    - Requires the confirmation text to be exactly "Delete".
    - Marks the user as deleted and clears all credits.
    """
    if payload.confirmation_text != "Delete":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Confirmation text must exactly be "Delete".',
        )

    user = get_user_by_id(current_user.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    # Soft delete: mark as deleted
    user.deleted_at = datetime.utcnow()
    save_user(user)

    # Best-effort: clear all credits
    try:
        clear_all_credits(current_user.user_id)
    except Exception:
        # We do not fail the deletion if credits clearing has an issue
        pass

    return {"message": "Account has been deleted."}



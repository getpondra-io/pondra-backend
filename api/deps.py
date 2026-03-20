"""
api/deps.py
────────────
FastAPI dependency injection:
- get_current_user  — verify JWT, return User
- get_current_farm  — verify farm belongs to user
- pagination params
"""

from typing import Optional, Annotated
from fastapi import Depends, HTTPException, status, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError
import uuid

from db.database import get_db, User, Farm
from core.security import decode_token

# ── Bearer token scheme ───────────────────────────────────────────────────────

bearer_scheme = HTTPBearer(auto_error=True)

# ── Current user ──────────────────────────────────────────────────────────────

async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """
    Decode JWT from Authorization header.
    Returns the authenticated User or raises 401.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(credentials.credentials)
        user_id: str = payload.get("sub")
        token_type: str = payload.get("type")
        if user_id is None or token_type != "access":
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(
        select(User).where(User.id == uuid.UUID(user_id), User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user


async def get_current_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user


# ── Farm ownership ────────────────────────────────────────────────────────────

async def get_user_farm(
    farm_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Farm:
    """
    Verify the requested farm belongs to the current user.
    farm_id here is the human-readable farm_id string (e.g. 'POND-04'),
    not the UUID primary key.
    """
    result = await db.execute(
        select(Farm).where(
            Farm.farm_id == farm_id,
            Farm.owner_id == current_user.id,
            Farm.is_active == True,
        )
    )
    farm = result.scalar_one_or_none()
    if farm is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Farm '{farm_id}' not found",
        )
    return farm


# ── Pagination ────────────────────────────────────────────────────────────────

class PaginationParams:
    def __init__(
        self,
        page: int = Query(default=1, ge=1, description="Page number"),
        limit: int = Query(default=50, ge=1, le=500, description="Items per page"),
    ):
        self.page = page
        self.limit = limit
        self.offset = (page - 1) * limit


# ── Type aliases for clean signatures ────────────────────────────────────────

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]
Pagination = Annotated[PaginationParams, Depends()]

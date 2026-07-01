"""Role management endpoints.

GET  /api/roles           — list all roles with their full config
GET  /api/roles/{name}    — single role by name
PATCH /api/roles/{name}   — update editable fields (model, prompts, limits)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from flowtrack.api.deps import db_session
from flowtrack.api.schemas import RoleCard, RoleUpdate
from flowtrack.models import Role

router = APIRouter(prefix="/api/roles", tags=["roles"])


@router.get("", response_model=list[RoleCard])
def list_roles(db: Session = Depends(db_session)) -> list[Role]:
    return list(db.scalars(select(Role).order_by(Role.name)))


@router.get("/{name}", response_model=RoleCard)
def get_role(name: str, db: Session = Depends(db_session)) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"role '{name}' not found")
    return role


@router.patch("/{name}", response_model=RoleCard)
def update_role(name: str, body: RoleUpdate, db: Session = Depends(db_session)) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"role '{name}' not found")

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(role, field, value)

    db.flush()
    return role

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Group, GroupMember, User


def require_member(db: Session, group_id: str, user_id: str) -> Group:
    group = db.get(Group, group_id)
    if not group or not db.scalar(select(GroupMember.id).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)):
        raise HTTPException(status_code=404, detail="Group not found")
    return group


def require_admin(db: Session, group_id: str, user_id: str) -> Group:
    group = require_member(db, group_id, user_id)
    membership = db.scalar(select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Group administrator access required")
    return group


def audit(db: Session, actor_user_id: str | None, action: str, entity_type: str, entity_id: str, metadata: dict | None = None) -> None:
    from app.models import AuditLog
    db.add(AuditLog(actor_user_id=actor_user_id, action=action, entity_type=entity_type, entity_id=entity_id, metadata_json=metadata or {}))

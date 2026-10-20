"""
Roles and the View/Add/Edit/Delete permission matrix (RRAP-01-008, 01-009,
01-013). A role held by active users cannot be deleted until they are
reassigned; inactive holders are simply unassigned. The system
Administrator role is immutable apart from its description/MFA flag.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, require
from app.api.openapi import err, errors
from app.core.permissions import MODULE_BY_KEY, MODULES
from app.core.db.session import get_db
from app.modules.m01_security.models.user import Role, RolePermission, User, UserRole, UserStatus
from app.modules.m01_security.schemas import ModuleOut, PermissionRow, RoleCreateIn, RoleOut, RoleUpdateIn
from app.core import audit

router = APIRouter()

_E_404 = err(404, "not_found", "Role not found.")
_E_NAME = err(409, "role_name_taken", "A role with this name already exists.")
_E_MATRIX = (err(422, "unknown_module", "Unknown module 'reportz'."),
             err(422, "duplicate_module", "Module 'detections' listed twice."))


def _matrix(role: Role) -> list[PermissionRow]:
    if role.is_system:
        return [PermissionRow(module=m.key, **{f"can_{a}": a in m.actions for a in ("view", "add", "edit", "delete")})
                for m in MODULES]
    by_module = {p.module: p for p in role.permissions}
    rows = []
    for m in MODULES:
        p = by_module.get(m.key)
        rows.append(PermissionRow(
            module=m.key,
            can_view=bool(p and p.can_view), can_add=bool(p and p.can_add),
            can_edit=bool(p and p.can_edit), can_delete=bool(p and p.can_delete),
        ))
    return rows


def _flat(rows: list[PermissionRow]) -> dict[str, list[str]]:
    return {r.module: [a for a in ("view", "add", "edit", "delete") if getattr(r, f"can_{a}")]
            for r in rows if any((r.can_view, r.can_add, r.can_edit, r.can_delete))}


async def _counts(db: AsyncSession, role_ids: list[str]) -> dict[str, tuple[int, int]]:
    if not role_ids:
        return {}
    rows = (await db.execute(
        select(UserRole.role_id, User.status, func.count())
        .join(User, User.id == UserRole.user_id)
        .where(UserRole.role_id.in_(role_ids))
        .group_by(UserRole.role_id, User.status)
    )).all()
    out: dict[str, list[int]] = {}
    for role_id, status, n in rows:
        total_active = out.setdefault(role_id, [0, 0])
        total_active[0] += n
        if status == UserStatus.active:
            total_active[1] += n
    return {k: (v[0], v[1]) for k, v in out.items()}


def _out(role: Role, counts: dict[str, tuple[int, int]]) -> RoleOut:
    total, active = counts.get(role.id, (0, 0))
    return RoleOut(
        id=role.id, name=role.name, description=role.description, is_active=role.is_active,
        is_system=role.is_system, mfa_required=role.mfa_required,
        user_count=total, active_user_count=active, permissions=_matrix(role),
        created_at=role.created_at, updated_at=role.updated_at,
    )


def _apply_permissions(role: Role, rows: list[PermissionRow]) -> None:
    seen = set()
    for row in rows:
        module = MODULE_BY_KEY.get(row.module)
        if module is None:
            raise api_error(422, "unknown_module", f"Unknown module '{row.module}'.")
        if row.module in seen:
            raise api_error(422, "duplicate_module", f"Module '{row.module}' listed twice.")
        seen.add(row.module)
    by_module = {p.module: p for p in role.permissions}
    for row in rows:
        allowed = MODULE_BY_KEY[row.module].actions
        values = {f"can_{a}": bool(getattr(row, f"can_{a}")) and a in allowed for a in ("view", "add", "edit", "delete")}
        # Any write permission implies being able to see the module.
        if values["can_add"] or values["can_edit"] or values["can_delete"]:
            values["can_view"] = True
        existing = by_module.get(row.module)
        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
        else:
            role.permissions.append(RolePermission(module=row.module, **values))


async def _get_role(db: AsyncSession, role_id: str) -> Role:
    role = await db.get(Role, role_id)
    if role is None:
        raise api_error(404, "not_found", "Role not found.")
    return role


async def _check_name(db: AsyncSession, name: str, exclude: str | None = None) -> None:
    q = select(Role.id).where(func.lower(Role.name) == name.strip().lower())
    if exclude:
        q = q.where(Role.id != exclude)
    if (await db.execute(q)).first():
        raise api_error(409, "role_name_taken", "A role with this name already exists.")


@router.get("/modules", response_model=list[ModuleOut], summary="List permission modules")
async def list_modules(_: Principal = Depends(require("roles", "view"))):
    """The catalogue of permission modules (key, label, menu group, actions) that roles grant rights
    on. These keys appear in every `**Permission:** module:action` line of this documentation."""
    return [ModuleOut(key=m.key, label=m.label, group=m.group, actions=list(m.actions)) for m in MODULES]


@router.get("/", response_model=list[RoleOut], summary="List roles")
async def list_roles(db: AsyncSession = Depends(get_db),
                     _: Principal = Depends(require("roles", "view"))):
    """All roles (system role first) with their permission matrix and holder counts."""
    roles = (await db.execute(select(Role).order_by(Role.is_system.desc(), Role.name))).scalars().all()
    counts = await _counts(db, [r.id for r in roles])
    return [_out(r, counts) for r in roles]


@router.get("/{role_id}", response_model=RoleOut, summary="Get a role", responses=errors(_E_404))
async def get_role(role_id: str, db: AsyncSession = Depends(get_db),
                   _: Principal = Depends(require("roles", "view"))):
    role = await _get_role(db, role_id)
    return _out(role, await _counts(db, [role.id]))


@router.post("/", response_model=RoleOut, status_code=201, summary="Create a role",
             responses=errors(_E_NAME, *_E_MATRIX))
async def create_role(body: RoleCreateIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("roles", "add"))):
    """Creates a role with its View/Add/Edit/Delete matrix (any write right implies View). Audited as
    `role.create`."""
    await _check_name(db, body.name)
    role = Role(name=body.name.strip(), description=body.description, is_active=body.is_active,
                mfa_required=body.mfa_required)
    _apply_permissions(role, body.permissions)
    db.add(role)
    await db.flush()
    audit.record(db, request, "role.create", principal=principal, object_type="role", object_id=role.id,
                 details={"name": role.name, "is_active": role.is_active, "mfa_required": role.mfa_required,
                          "permissions": _flat(_matrix(role))})
    await db.commit()
    return _out(role, {})


@router.patch("/{role_id}", response_model=RoleOut, summary="Update a role",
              responses=errors(_E_404, _E_NAME,
                               err(409, "system_role",
                                   "The Administrator role always holds every permission and stays active."),
                               *_E_MATRIX))
async def update_role(role_id: str, body: RoleUpdateIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("roles", "edit"))):
    """Partial update; `permissions` rows are merged per module. The system Administrator role only
    accepts description / `mfa_required` changes. Takes effect on the holders' next request. Audited as
    `role.update` with a diff."""
    role = await _get_role(db, role_id)
    changes = body.model_dump(exclude_unset=True)
    if role.is_system and ({"name", "is_active", "permissions"} & {k for k, v in changes.items() if v is not None}):
        if changes.get("name") not in (None, role.name) or changes.get("is_active") is False or changes.get("permissions"):
            raise api_error(409, "system_role", "The Administrator role always holds every permission and stays active.")

    before = {"name": role.name, "description": role.description, "is_active": role.is_active,
              "mfa_required": role.mfa_required, "permissions": _flat(_matrix(role))}
    if changes.get("name"):
        await _check_name(db, changes["name"], exclude=role.id)
        role.name = changes["name"].strip()
    if "description" in changes:
        role.description = changes["description"]
    if changes.get("is_active") is not None:
        role.is_active = changes["is_active"]
    if changes.get("mfa_required") is not None:
        role.mfa_required = changes["mfa_required"]
    if changes.get("permissions") is not None and not role.is_system:
        _apply_permissions(role, body.permissions or [])
    after = {"name": role.name, "description": role.description, "is_active": role.is_active,
             "mfa_required": role.mfa_required, "permissions": _flat(_matrix(role))}
    audit.record(db, request, "role.update", principal=principal, object_type="role", object_id=role.id,
                 details=audit.diff(before, after))
    await db.commit()
    return _out(role, await _counts(db, [role.id]))


@router.delete("/{role_id}", status_code=204, summary="Delete a role",
               responses=errors(_E_404, err(409, "system_role", "The Administrator role cannot be deleted."),
                                err(409, "role_in_use",
                                    "2 active user(s) still have this role. Reassign them before deleting it.",
                                    active_user_count=2)))
async def delete_role(role_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("roles", "delete"))):
    """Refused while active users hold the role; inactive holders are simply unassigned. Audited as
    `role.delete`."""
    role = await _get_role(db, role_id)
    if role.is_system:
        raise api_error(409, "system_role", "The Administrator role cannot be deleted.")
    total, active = (await _counts(db, [role.id])).get(role.id, (0, 0))
    if active:
        raise api_error(409, "role_in_use",
                        f"{active} active user(s) still have this role. Reassign them before deleting it.",
                        active_user_count=active)
    await db.execute(delete(UserRole).where(UserRole.role_id == role.id))
    snapshot = {"name": role.name, "unassigned_inactive_users": total, "permissions": _flat(_matrix(role))}
    await db.delete(role)
    audit.record(db, request, "role.delete", principal=principal, object_type="role", object_id=role_id,
                 details=snapshot)
    await db.commit()

"""Internal team cases. Legacy customer support endpoints are unchanged."""
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .audit_log import AuditAction, write_audit
from .moderator_access import ModeratorIdentity, require_permission
from .moderator_customers import CustomerSummary, customer_read_connection, customer_row, customer_summary
from .support_sync import sync_support_cases

router = APIRouter(prefix="/api/mod/support", tags=["Internal support"])
CaseStatus = Literal["NEW", "OPEN", "WAITING", "RESOLVED", "CLOSED"]
Priority = Literal["LOW", "NORMAL", "HIGH"]
ID = r"^[A-Za-z0-9_-]+$"


def mask_support_text(text: str) -> str:
    text = re.sub(r"\b(?:sk|pk)_[A-Za-z0-9_-]+", "[GİZLİ]", text, flags=re.I)
    text = re.sub(r"\bBearer\s+[^\s<>\"']+", "Bearer [GİZLİ]", text, flags=re.I)
    text = re.sub(r"(?i)\b(?:api[_ -]?key|secret|token|password)\s*[:=]\s*[\"']?[^\s<>\"']+",
                  "[GİZLİ]", text)
    text = re.sub(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", "[GİZLİ]", text)
    return re.sub(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_+/=-]{24,})(?![A-Za-z0-9])", "[GİZLİ]", text)


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseItem(ReadModel):
    id: str = Field(pattern=ID, max_length=160)
    user_id: str = Field(pattern=ID, max_length=160)
    subject: str
    priority: Priority
    case_status: CaseStatus
    assignee_user_id: str | None = Field(default=None, pattern=ID, max_length=160)
    created_at: datetime
    version: int = Field(ge=1)
    assigned_to_me: bool


class CasesPage(ReadModel):
    items: list[CaseItem]
    total: int
    limit: int
    offset: int


class NoteItem(ReadModel):
    id: str = Field(pattern=ID, max_length=160)
    author_user_id: str = Field(pattern=ID, max_length=160)
    body: str
    created_at: datetime


class CaseDetail(CaseItem):
    message: str
    legacy_status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]
    legacy_response_note: str
    notes: list[NoteItem]
    customer: CustomerSummary


class Summary(ReadModel):
    open_cases: int | None = Field(default=None, ge=0)
    unassigned: int | None = Field(default=None, ge=0)
    assigned_to_me: int | None = Field(default=None, ge=0)


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: CaseStatus
    expected_version: int = Field(ge=1, strict=True)


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Note must not be blank")
        return value


VISIBLE = """FROM support_cases c JOIN commercial_auth_users u ON u.user_id = c.user_id
    WHERE u.security->>'role' = 'CUSTOMER' AND c.user_id <> $1
      AND NOT EXISTS (SELECT 1 FROM commercial_erased_users e
        WHERE e.user_hash = encode(sha256(convert_to(c.user_id, 'UTF8')), 'hex'))"""
FILTERS = """AND ($2::text IS NULL OR c.case_status = $2
                 OR ($2 = 'OPEN' AND c.case_status = 'NEW'))
    AND ($3::text IS NULL OR ($3 = 'mine' AND c.assignee_user_id = $1)
         OR ($3 = 'unassigned' AND c.assignee_user_id IS NULL))
    AND ($4::text IS NULL OR c.priority = $4)"""


@asynccontextmanager
async def support_connection(request: Request, identity: ModeratorIdentity, permission: str):
    # Same durable 30/60s bucket as the stage 4 customer endpoints.
    async with customer_read_connection(request, identity, permission) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(61010004)")
        yield conn


def case_item(row, identity: ModeratorIdentity, *, full_subject=False) -> CaseItem:
    subject = mask_support_text(row["subject"])
    return CaseItem(
        id=row["id"], user_id=row["user_id"], subject=subject if full_subject else subject[:100],
        priority=row["priority"], case_status=row["case_status"],
        assignee_user_id=row["assignee_user_id"], created_at=row["created_at_legacy"],
        version=row["version"], assigned_to_me=row["assignee_user_id"] == identity.user_id,
    )


async def visible_case(conn, identity: ModeratorIdentity, case_id: str):
    row = await conn.fetchrow(
        f"SELECT c.* {VISIBLE} AND c.id = $2 FOR UPDATE OF c", identity.user_id, case_id,
    )
    if row is None:
        raise HTTPException(404, "Support case not found")
    # Canonical target/customer DTO uses the same hidden-target and erasure rules.
    customer = customer_summary(await customer_row(conn, identity, row["user_id"]))
    return row, customer


async def audit_case(conn, request: Request, case_id: str, action: AuditAction):
    await write_audit(conn, request.state.mod_read_actor, action, "SUPPORT_CASE", case_id, {}, {})


@router.get("/cases", response_model=CasesPage)
async def list_cases(
    request: Request,
    identity: ModeratorIdentity = Depends(require_permission("support.view")),
    limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0),
    status: CaseStatus | None = None,
    assignment: Literal["mine", "unassigned"] | None = None, priority: Priority | None = None,
):
    async with support_connection(request, identity, "support.view") as conn:
        await sync_support_cases(conn)
        args = (identity.user_id, status, assignment, priority)
        total = await conn.fetchval(f"SELECT COUNT(*) {VISIBLE} {FILTERS}", *args)
        rows = await conn.fetch(
            f"SELECT c.* {VISIBLE} {FILTERS} ORDER BY c.created_at_legacy DESC, c.id LIMIT $5 OFFSET $6",
            *args, limit, offset,
        )
        return CasesPage(items=[case_item(row, identity) for row in rows], total=total, limit=limit, offset=offset)


@router.get("/summary", response_model=Summary)
async def support_summary(
    request: Request, identity: ModeratorIdentity = Depends(require_permission("support.view")),
):
    try:
        async with support_connection(request, identity, "support.view") as conn:
            await sync_support_cases(conn)
            row = await conn.fetchrow(
                f"""SELECT COUNT(*) FILTER (WHERE c.case_status IN ('NEW','OPEN','WAITING')) AS open_cases,
                    COUNT(*) FILTER (WHERE c.assignee_user_id IS NULL AND c.case_status NOT IN ('RESOLVED','CLOSED')) AS unassigned,
                    COUNT(*) FILTER (WHERE c.assignee_user_id = $1 AND c.case_status NOT IN ('RESOLVED','CLOSED')) AS assigned_to_me
                    {VISIBLE}""", identity.user_id,
            )
            if row is None:
                raise RuntimeError("Support counts unavailable")
            return Summary(**dict(row))
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        # Explicit requirement: unavailable data is null, never a fabricated zero.
        return Summary()


@router.get("/cases/{case_id}", response_model=CaseDetail)
async def case_detail(
    request: Request, case_id: str = Path(pattern=ID, min_length=1, max_length=160),
    identity: ModeratorIdentity = Depends(require_permission("support.view")),
):
    async with support_connection(request, identity, "support.view") as conn:
        row, customer = await visible_case(conn, identity, case_id)
        notes = await conn.fetch(
            "SELECT id, author_user_id, body, created_at FROM support_notes WHERE case_id = $1 ORDER BY created_at, id", case_id,
        )
        result = CaseDetail(
            **case_item(row, identity, full_subject=True).model_dump(), customer=customer,
            message=mask_support_text(row["message"]), legacy_status=row["legacy_status"],
            legacy_response_note=mask_support_text(row["legacy_response_note"]),
            notes=[NoteItem(**{**dict(note), "body": mask_support_text(note["body"])}) for note in notes],
        )
        await audit_case(conn, request, case_id, "support.case.viewed")
        return result


async def mutate_case(request: Request, identity: ModeratorIdentity, case_id: str, operation: str,
                      payload: StatusChange | NoteCreate | None = None):
    async with support_connection(request, identity, "support.manage") as conn:
        row, _ = await visible_case(conn, identity, case_id)
        if operation == "take":
            changed = await conn.fetchrow(
                """UPDATE support_cases SET assignee_user_id = $2, version = version + 1, updated_at = clock_timestamp()
                   WHERE id = $1 AND assignee_user_id IS NULL RETURNING *""", case_id, identity.user_id,
            )
            action: AuditAction = "support.case.taken"
        else:
            if row["assignee_user_id"] != identity.user_id:
                raise HTTPException(409, "Support case assignment changed")
            if operation == "release":
                changed = await conn.fetchrow(
                    """UPDATE support_cases SET assignee_user_id = NULL, version = version + 1, updated_at = clock_timestamp()
                       WHERE id = $1 AND assignee_user_id = $2 RETURNING *""", case_id, identity.user_id,
                )
                action = "support.case.released"
            elif isinstance(payload, StatusChange):
                changed = await conn.fetchrow(
                    """UPDATE support_cases SET case_status = $3, version = version + 1, updated_at = clock_timestamp()
                       WHERE id = $1 AND assignee_user_id = $2 AND version = $4 RETURNING *""",
                    case_id, identity.user_id, payload.status, payload.expected_version,
                )
                action = "support.case.status_changed"
            elif isinstance(payload, NoteCreate):
                await conn.execute(
                    "INSERT INTO support_notes (id, case_id, author_user_id, body) VALUES ($1, $2, $3, $4)",
                    uuid.uuid4().hex, case_id, identity.user_id, payload.body,
                )
                changed = await conn.fetchrow(
                    """UPDATE support_cases SET version = version + 1, updated_at = clock_timestamp()
                       WHERE id = $1 AND assignee_user_id = $2 RETURNING *""", case_id, identity.user_id,
                )
                action = "support.note.added"
            else:
                raise RuntimeError("Unsupported support operation")
        if changed is None:
            raise HTTPException(409, "Support case updated by another operator")
        result = case_item(changed, identity)
        await audit_case(conn, request, case_id, action)
        return result


@router.post("/cases/{case_id}/take", response_model=CaseItem)
async def take_case(request: Request, case_id: str = Path(pattern=ID, min_length=1, max_length=160),
                    identity: ModeratorIdentity = Depends(require_permission("support.manage"))):
    return await mutate_case(request, identity, case_id, "take")


@router.post("/cases/{case_id}/release", response_model=CaseItem)
async def release_case(request: Request, case_id: str = Path(pattern=ID, min_length=1, max_length=160),
                       identity: ModeratorIdentity = Depends(require_permission("support.manage"))):
    return await mutate_case(request, identity, case_id, "release")


@router.post("/cases/{case_id}/status", response_model=CaseItem)
async def status_case(payload: StatusChange, request: Request,
                      case_id: str = Path(pattern=ID, min_length=1, max_length=160),
                      identity: ModeratorIdentity = Depends(require_permission("support.manage"))):
    return await mutate_case(request, identity, case_id, "status", payload)


@router.post("/cases/{case_id}/notes", response_model=CaseItem)
async def note_case(payload: NoteCreate, request: Request,
                    case_id: str = Path(pattern=ID, min_length=1, max_length=160),
                    identity: ModeratorIdentity = Depends(require_permission("support.manage"))):
    return await mutate_case(request, identity, case_id, "notes", payload)

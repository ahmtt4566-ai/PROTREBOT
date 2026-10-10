"""V24 sales, onboarding and support launch lab.

The router shares the existing authenticated commercial state.  It stores only
Demo commercial records; it cannot collect card data or switch billing live.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .commerce_core import calculate_demo_invoice, launch_checklist, sanitize_business_settings
from .email_service import masked_recipient
from .v22_commercial import (
    active_license,
    add_audit,
    authenticated_user,
    now_iso,
    public_user,
    runtime,
    save_state,
)


router = APIRouter(prefix="/api/v22/commerce", tags=["V24 Commercial Launch Lab"])


class BusinessSettingsRequest(BaseModel):
    brand_name: str = Field(min_length=2, max_length=80)
    legal_name: str = Field(default="", max_length=120)
    support_email: str = Field(default="", max_length=180)
    website_url: str = Field(default="", max_length=240)
    currency: Literal["USD", "EUR", "TRY", "USDT"] = "USD"
    trial_days: int = Field(default=14, ge=1, le=90)
    terms_version: str = Field(default="V24-DEMO-1", min_length=3, max_length=40)
    country: str = Field(default="Türkiye", min_length=2, max_length=80)


class LeadRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=180)
    company: str = Field(default="", max_length=120)
    interested_plan: Literal["TRIAL", "STARTER", "PRO", "ELITE"] = "TRIAL"
    note: str = Field(default="", max_length=500)


class LeadStatusRequest(BaseModel):
    status: Literal["NEW", "CONTACTED", "TRIAL", "WON", "LOST"]
    note: str = Field(default="", max_length=500)


class InvoicePreviewRequest(BaseModel):
    plan: Literal["TRIAL", "STARTER", "PRO", "ELITE"]
    months: int = Field(default=1, ge=1, le=24)
    discount_pct: float = Field(default=0, ge=0, le=100)
    tax_pct: float = Field(default=0, ge=0, le=100)
    customer_name: str = Field(default="Demo Müşteri", max_length=120)


class AcceptanceRequest(BaseModel):
    terms_version: str = Field(min_length=3, max_length=40)
    risk_acknowledged: bool
    demo_only_acknowledged: bool


class SupportRequest(BaseModel):
    subject: str = Field(min_length=3, max_length=160)
    message: str = Field(min_length=5, max_length=2000)
    priority: Literal["LOW", "NORMAL", "HIGH"] = "NORMAL"


class SupportStatusRequest(BaseModel):
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]
    response_note: str = Field(default="", max_length=1000)


def commerce_overview(state: dict) -> dict:
    checklist = launch_checklist(state["business"], state.get("release_evidence", {}))
    passed = sum(1 for item in checklist if item["passed"])
    funnel = {key: 0 for key in ("NEW", "CONTACTED", "TRIAL", "WON", "LOST")}
    for lead in state.get("leads", []):
        status = str(lead.get("status", "NEW"))
        funnel[status] = funnel.get(status, 0) + 1
    open_tickets = sum(1 for item in state.get("support_tickets", []) if item.get("status") in {"OPEN", "IN_PROGRESS"})
    return {
        "business": state["business"],
        "plans": state["plans"],
        "leads": state.get("leads", [])[:100],
        "invoices": state.get("demo_invoices", [])[:50],
        "support_tickets": state.get("support_tickets", [])[:100],
        "funnel": funnel,
        "open_tickets": open_tickets,
        "launch_score": round(passed / len(checklist) * 100) if checklist else 0,
        "launch_checklist": checklist,
        "checkout_live": False,
        "payment_provider": "NOT_CONFIGURED",
        "collects_card_data": False,
        "demo_only": True,
    }


@router.get("/overview")
async def v24_commerce_overview(request: Request):
    authenticated_user(request, owner=True)
    return commerce_overview(runtime(request)["state"])


@router.put("/settings")
async def v24_business_settings(payload: BusinessSettingsRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    if payload.support_email and "@" not in payload.support_email:
        raise HTTPException(422, "Geçerli bir destek e-postası yazın")
    if payload.website_url and not payload.website_url.startswith(("https://", "http://")):
        raise HTTPException(422, "Web adresi http:// veya https:// ile başlamalıdır")
    rt = runtime(request)
    async with rt["lock"]:
        settings = sanitize_business_settings(payload.model_dump())
        rt["state"]["business"] = settings
        add_audit(rt["state"], "BUSINESS_SETTINGS", "V24 marka ve müşteri kurulum ayarları güncellendi.", actor=owner["id"], subject="business")
        save_state(rt["state"])
    return {"business": settings, "checkout_live": False, "demo_only": True}


@router.post("/leads")
async def v24_create_lead(payload: LeadRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    if "@" not in payload.email:
        raise HTTPException(422, "Geçerli bir e-posta yazın")
    rt = runtime(request)
    async with rt["lock"]:
        row = {
            "id": uuid.uuid4().hex,
            **payload.model_dump(),
            "email": payload.email.strip().casefold(),
            "status": "NEW",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "demo_only": True,
        }
        rt["state"]["leads"].insert(0, row)
        del rt["state"]["leads"][500:]
        add_audit(rt["state"], "LEAD_CREATED", f"{masked_recipient(row['email'])} için Demo satış adayı eklendi.", actor=owner["id"], subject=row["id"])
        save_state(rt["state"])
    return row


@router.put("/leads/{lead_id}/status")
async def v24_lead_status(lead_id: str, payload: LeadStatusRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        row = next((item for item in rt["state"]["leads"] if item.get("id") == lead_id), None)
        if not row:
            raise HTTPException(404, "Satış adayı bulunamadı")
        row.update({"status": payload.status, "status_note": payload.note.strip(), "updated_at": now_iso()})
        add_audit(rt["state"], "LEAD_STATUS", f"Demo satış adayı {payload.status} aşamasına taşındı.", actor=owner["id"], subject=lead_id)
        save_state(rt["state"])
    return row


@router.post("/invoice-preview")
async def v24_invoice_preview(payload: InvoicePreviewRequest, request: Request):
    user = authenticated_user(request)
    rt = runtime(request)
    plan = rt["state"]["plans"].get(payload.plan)
    if not plan:
        raise HTTPException(404, "Paket bulunamadı")
    result = calculate_demo_invoice(
        plan_code=payload.plan,
        plan=plan,
        months=payload.months,
        discount_pct=payload.discount_pct,
        tax_pct=payload.tax_pct,
        currency=rt["state"]["business"].get("currency", "USD"),
    )
    async with rt["lock"]:
        row = {"id": uuid.uuid4().hex, "customer_name": payload.customer_name.strip(), "created_by": user["id"], "created_at": now_iso(), **result}
        rt["state"]["demo_invoices"].insert(0, row)
        del rt["state"]["demo_invoices"][200:]
        add_audit(rt["state"], "DEMO_INVOICE", f"{payload.plan} için tahsilatsız fiyat teklifi oluşturuldu.", actor=user["id"], subject=row["id"])
        save_state(rt["state"])
    return row


@router.post("/acceptance")
async def v24_acceptance(payload: AcceptanceRequest, request: Request):
    user = authenticated_user(request)
    if not payload.risk_acknowledged or not payload.demo_only_acknowledged:
        raise HTTPException(422, "Risk ve Demo/Paper kapsamı ayrı ayrı kabul edilmelidir")
    rt = runtime(request)
    async with rt["lock"]:
        row = {"id": uuid.uuid4().hex, "user_id": user["id"], **payload.model_dump(), "accepted_at": now_iso(), "demo_only": True}
        rt["state"]["acceptances"].insert(0, row)
        add_audit(rt["state"], "TERMS_ACCEPTED", f"{payload.terms_version} Demo koşulları kabul edildi.", actor=user["id"], subject=user["id"])
        save_state(rt["state"])
    return row


@router.post("/support")
async def v24_support_create(payload: SupportRequest, request: Request):
    user = authenticated_user(request)
    rt = runtime(request)
    async with rt["lock"]:
        row = {"id": uuid.uuid4().hex, "user_id": user["id"], "customer": public_user(user), **payload.model_dump(), "status": "OPEN", "created_at": now_iso(), "updated_at": now_iso(), "demo_only": True}
        rt["state"]["support_tickets"].insert(0, row)
        del rt["state"]["support_tickets"][500:]
        add_audit(rt["state"], "SUPPORT_OPENED", payload.subject, actor=user["id"], subject=row["id"])
        save_state(rt["state"])
    return row


@router.put("/support/{ticket_id}")
async def v24_support_status(ticket_id: str, payload: SupportStatusRequest, request: Request):
    owner = authenticated_user(request, owner=True)
    rt = runtime(request)
    async with rt["lock"]:
        row = next((item for item in rt["state"]["support_tickets"] if item.get("id") == ticket_id), None)
        if not row:
            raise HTTPException(404, "Destek kaydı bulunamadı")
        row.update({"status": payload.status, "response_note": payload.response_note.strip(), "updated_at": now_iso(), "updated_by": owner["id"]})
        add_audit(rt["state"], "SUPPORT_STATUS", f"Destek kaydı {payload.status} olarak güncellendi.", actor=owner["id"], subject=ticket_id)
        save_state(rt["state"])
    return row


@router.get("/customer-home")
async def v24_customer_home(request: Request):
    user = authenticated_user(request)
    state = runtime(request)["state"]
    license_row = active_license(state, user["id"])
    plan = state["plans"].get((license_row or {}).get("plan", ""), {})
    accepted = next((item for item in state["acceptances"] if item.get("user_id") == user["id"]), None)
    tickets = [item for item in state["support_tickets"] if item.get("user_id") == user["id"]][:25]
    return {
        "user": public_user(user),
        "license": license_row,
        "entitlements": {"agents": plan.get("agents", 0), "bots": plan.get("bots", 0), "features": plan.get("features", [])},
        "latest_acceptance": accepted,
        "support_tickets": tickets,
        "business": state["business"],
        "checkout_live": False,
        "demo_only": True,
    }

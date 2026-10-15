"""Tenant admin endpoints for partner change webhooks (#103).

Mounted at /api/v1/admin/webhooks with the tenant API key (resolve_tenant),
so the tenant, not the partner, decides where its registry data is pushed.
The signing secret is returned once, on creation and on rotation.

  POST   /subscriptions                        create (returns the secret)
  GET    /subscriptions                        list
  GET    /subscriptions/{id}                   read
  DELETE /subscriptions/{id}                   delete with its deliveries
  POST   /subscriptions/{id}/rotate-secret     new secret (returned once);
                                               {"grace_period_seconds": n}
                                               keeps the old one signing too
  GET    /deliveries?status=dead               dead-letter (and other) view
  POST   /deliveries/{id}/redeliver            queue a delivery again

See collector/webhooks.py for delivery, signing and destination checks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from db.models import PartnerCredential, WebhookDelivery, WebhookSubscription
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from collector import webhooks
from collector.auth import resolve_tenant

router = APIRouter(prefix="/api/v1/admin/webhooks", tags=["Admin"])

SECRET_WARNING = "Save the signing secret now. It will not be shown again."


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _not_configured() -> HTTPException:
    return _problem(503, "Service Unavailable", "Webhooks are not configured on this collector")


class CreateSubscriptionBody(BaseModel):
    partner_id: UUID
    url: str = Field(..., min_length=1, max_length=webhooks.MAX_URL_LENGTH)
    event_types: list[webhooks.EventType] = Field(
        default_factory=lambda: list(webhooks.EVENT_TYPES), min_length=1
    )
    description: str | None = Field(default=None, max_length=200)


def _subscription_dict(s: WebhookSubscription) -> dict:
    previous_until = (
        s.previous_secret_expires_at
        if webhooks.previous_secret_valid(s, datetime.now(UTC))
        else None
    )
    return {
        "id": str(s.id),
        "partner_id": str(s.partner_id),
        "url": s.url,
        "event_types": list(s.event_types),
        "description": s.description,
        "is_active": s.is_active,
        # Set while a rotated-out secret still signs deliveries.
        "rotation_grace_ends_at": previous_until.isoformat() if previous_until else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _delivery_dict(d: WebhookDelivery) -> dict:
    def iso(v):
        return v.isoformat() if v else None

    return {
        "id": str(d.id),
        "subscription_id": str(d.subscription_id),
        "event_id": str(d.event_id),
        "event_type": d.event_type,
        "asset_id": str(d.asset_id) if d.asset_id else None,
        "status": d.status,
        "attempts": d.attempts,
        "last_status_code": d.last_status_code,
        "last_error": d.last_error,
        "next_attempt_at": iso(d.next_attempt_at) if d.status == "pending" else None,
        "last_attempt_at": iso(d.last_attempt_at),
        "delivered_at": iso(d.delivered_at),
        "created_at": iso(d.created_at),
    }


async def _get_subscription(db, tenant_id: UUID, subscription_id: UUID) -> WebhookSubscription:
    row = (
        await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "Webhook subscription not found")
    return row


@router.post("/subscriptions", status_code=201, summary="Subscribe a partner to asset change events")
async def create_subscription(body: CreateSubscriptionBody, request: Request) -> dict:
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    partner = (
        await db.execute(
            select(PartnerCredential).where(
                PartnerCredential.id == body.partner_id,
                PartnerCredential.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if partner is None:
        raise _problem(404, "Not Found", "Partner credential not found")
    reason = webhooks.partner_ineligible_reason(
        partner, datetime.now(UTC), body.event_types
    )
    if reason is not None:
        raise _problem(400, "Bad Request", f"Partner cannot receive webhooks: {reason}")

    count = (
        await db.execute(
            select(func.count(WebhookSubscription.id)).where(
                WebhookSubscription.partner_id == partner.id
            )
        )
    ).scalar() or 0
    if count >= webhooks.MAX_SUBSCRIPTIONS_PER_PARTNER:
        raise _problem(
            400,
            "Bad Request",
            f"A partner can have at most {webhooks.MAX_SUBSCRIPTIONS_PER_PARTNER} subscriptions",
        )

    try:
        await webhooks.resolve_destination(body.url)
    except webhooks.DestinationNotAllowed as exc:
        raise _problem(400, "Bad Request", f"Invalid webhook URL: {exc}") from exc

    subscription_id = uuid4()
    secret = webhooks.generate_signing_secret()
    try:
        ciphertext = webhooks.encrypt_secret(secret, subscription_id)
    except webhooks.WebhooksNotConfigured as exc:
        raise _not_configured() from exc

    sub = WebhookSubscription(
        id=subscription_id,
        tenant_id=tenant_id,
        partner_id=partner.id,
        url=body.url,
        event_types=sorted(set(body.event_types)),
        description=body.description,
        secret_ciphertext=ciphertext,
        is_active=True,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {**_subscription_dict(sub), "secret": secret, "warning": SECRET_WARNING}


@router.get("/subscriptions", summary="List webhook subscriptions")
async def list_subscriptions(request: Request, partner_id: UUID | None = None) -> dict:
    tenant_id = await resolve_tenant(request)
    conditions = [WebhookSubscription.tenant_id == tenant_id]
    if partner_id is not None:
        conditions.append(WebhookSubscription.partner_id == partner_id)
    rows = (
        await request.state.db.execute(
            select(WebhookSubscription)
            .where(*conditions)
            .order_by(WebhookSubscription.created_at)
        )
    ).scalars().all()
    return {"data": [_subscription_dict(r) for r in rows]}


@router.get("/subscriptions/{subscription_id}", summary="Get a webhook subscription")
async def get_subscription(subscription_id: UUID, request: Request) -> dict:
    tenant_id = await resolve_tenant(request)
    return _subscription_dict(
        await _get_subscription(request.state.db, tenant_id, subscription_id)
    )


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=204,
    summary="Delete a webhook subscription and its queued and past deliveries",
)
async def delete_subscription(subscription_id: UUID, request: Request) -> None:
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    sub = await _get_subscription(db, tenant_id, subscription_id)
    await db.execute(
        delete(WebhookDelivery).where(WebhookDelivery.subscription_id == sub.id)
    )
    await db.delete(sub)
    await db.commit()


class RotateSecretBody(BaseModel):
    grace_period_seconds: int = Field(
        default=0,
        ge=0,
        le=int(webhooks.MAX_SECRET_GRACE.total_seconds()),
        description=(
            "Keep signing with the old secret too for this long, so the "
            "receiver can switch without dropping deliveries. 0 (the default) "
            "invalidates the old secret at once."
        ),
    )


@router.post(
    "/subscriptions/{subscription_id}/rotate-secret",
    summary="Replace the signing secret, optionally keeping the old one valid for a grace period",
)
async def rotate_secret(
    subscription_id: UUID, request: Request, body: RotateSecretBody | None = None
) -> dict:
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    sub = await _get_subscription(db, tenant_id, subscription_id)
    grace = body.grace_period_seconds if body else 0
    now = datetime.now(UTC)
    secret = webhooks.generate_signing_secret()
    try:
        previous = None
        if grace > 0:
            # Re-encrypt rather than copy, so the kept secret is under the
            # active key too. Only the secret being replaced is kept; one
            # still in an earlier grace period ends now.
            try:
                old = webhooks.decrypt_secret(sub.secret_ciphertext, sub.id)
            except ValueError as exc:
                raise _problem(
                    409,
                    "Conflict",
                    "The current secret cannot be read, so it cannot be kept; "
                    "rotate without a grace period",
                ) from exc
            previous = webhooks.encrypt_secret(old, sub.id)
        sub.secret_ciphertext = webhooks.encrypt_secret(secret, sub.id)
    except webhooks.WebhooksNotConfigured as exc:
        raise _not_configured() from exc
    sub.previous_secret_ciphertext = previous
    sub.previous_secret_expires_at = now + timedelta(seconds=grace) if previous else None
    await db.commit()
    await db.refresh(sub)
    return {**_subscription_dict(sub), "secret": secret, "warning": SECRET_WARNING}


@router.get("/deliveries", summary="List webhook deliveries (dead letters by default)")
async def list_deliveries(
    request: Request,
    status: Literal["pending", "succeeded", "dead"] = "dead",
    subscription_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    tenant_id = await resolve_tenant(request)
    conditions = [WebhookDelivery.tenant_id == tenant_id, WebhookDelivery.status == status]
    if subscription_id is not None:
        conditions.append(WebhookDelivery.subscription_id == subscription_id)
    rows = (
        await request.state.db.execute(
            select(WebhookDelivery)
            .where(*conditions)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"data": [_delivery_dict(d) for d in rows]}


@router.post("/deliveries/{delivery_id}/redeliver", summary="Queue a delivery again")
async def redeliver(delivery_id: UUID, request: Request) -> dict:
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    d = (
        await db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if d is None:
        raise _problem(404, "Not Found", "Webhook delivery not found")
    if d.status == "pending":
        raise _problem(409, "Conflict", "Delivery is already queued")
    d.status = "pending"
    d.attempts = 0
    d.next_attempt_at = datetime.now(UTC)
    d.last_error = None
    d.last_status_code = None
    d.delivered_at = None
    await db.commit()
    await db.refresh(d)
    return _delivery_dict(d)

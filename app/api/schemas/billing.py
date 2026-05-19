from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class InitializePaymentRequest(BaseModel):
    plan: Literal["pro", "growth"] = "pro"
    callback_url: str


class InitializePaymentResponse(BaseModel):
    authorization_url: str
    access_code: str
    reference: str


class SubscriptionResponse(BaseModel):
    plan: str
    effective_plan: str
    status: str
    paystack_subscription_code: str | None
    next_payment_date: datetime | None
    payment_failed_at: datetime | None = None
    grace_ends_at: datetime | None = None
    payment_attention: bool = False
    manage_link_available: bool = False
    updated_at: datetime | None


class ManageLinkResponse(BaseModel):
    link: str

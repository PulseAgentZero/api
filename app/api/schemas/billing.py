from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class InitializePaymentRequest(BaseModel):
    plan: str  # "starter" | "growth" | "enterprise"
    callback_url: str


class InitializePaymentResponse(BaseModel):
    authorization_url: str
    access_code: str
    reference: str


class SubscriptionResponse(BaseModel):
    plan: str
    status: str
    paystack_subscription_code: str | None
    next_payment_date: datetime | None
    updated_at: datetime | None

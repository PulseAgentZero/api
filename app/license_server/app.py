"""
Entivia License Server — validates and issues license keys for self-hosted customers.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.license_server.deps import get_db, require_license_api_key
from app.license_server.service import purchase_license, validate_license

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Entivia License Server",
    description="Internal license validation and issuance for self-hosted Entivia instances",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)


class PurchaseRequest(BaseModel):
    payment_reference: str = Field(min_length=1)
    email: EmailStr
    org_id: str | None = None
    product: str = "self_hosted"


class ValidateRequest(BaseModel):
    license_key: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    version: str = "1.0.0"


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "service": "pulse-license"}


@app.post("/api/v1/keys/purchase")
async def keys_purchase(
    body: PurchaseRequest,
    _: None = Depends(require_license_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        data = await purchase_license(
            db,
            payment_reference=body.payment_reference,
            email=str(body.email),
            purchaser_org_id=body.org_id,
            product=body.product,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("purchase_license failed")
        raise HTTPException(status_code=500, detail="License issuance failed") from exc
    return {"data": data}


@app.post("/validate")
async def validate_license_route(
    body: ValidateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await validate_license(db, license_key=body.license_key, org_id=body.org_id)
    except Exception as exc:
        logger.exception("validate_license failed")
        raise HTTPException(status_code=500, detail="Validation failed") from exc

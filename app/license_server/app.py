"""
Pulse License Server — validates license keys for self-hosted customers.
Internal service only, not exposed publicly.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Pulse License Server",
    description="Internal license validation for self-hosted Pulse instances",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "service": "pulse-license"}


@app.post("/validate")
async def validate_license(body: dict) -> dict:
    """
    Validate a self-hosted license key.
    Called by self-hosted Pulse instances on activation and daily revalidation.
    Full implementation lives in the license management system.
    """
    return {
        "valid": False,
        "reason": "License server not yet fully implemented",
    }

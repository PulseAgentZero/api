def envelope(data: dict | list, org_id: str) -> dict:
    """Wraps public API responses in a consistent envelope."""
    return {
        "data": data,
        "meta": {
            "org_id": org_id,
            "api_version": "1",
        },
    }

def envelope(data: dict | list, org_id: str) -> dict:
    """Wrap payload in the public API `{ data, meta }` envelope (see ReDoc overview)."""
    return {
        "data": data,
        "meta": {
            "org_id": org_id,
            "api_version": "1",
        },
    }

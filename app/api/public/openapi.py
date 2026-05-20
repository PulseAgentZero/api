"""Custom OpenAPI schema for the mounted Public API sub-application."""

from __future__ import annotations

import copy

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

PUBLIC_API_MOUNT_PATH = "/api/public"

# Paths that do not require X-API-Key (visitor / embed access).
_UNAUTHENTICATED_PATH_PREFIXES = (
    "/v1/studio/dashboards/",
    "/v1/studio/embed/",
)

_PUBLIC_API_DESCRIPTION = """
# Entivia Public API

Programmatic access to **profiled entities**, **AI recommendations**, **pipeline runs**, and **org analytics** for integrations, data warehouses, and custom apps.

Interactive docs: **`/api/public/redoc`** · OpenAPI JSON: **`/api/public/openapi.json`**

---

## Base URL

The **Servers** box below is filled in automatically from the host you use to open this page
(for example, `http://localhost:8000/api/public` when viewing ReDoc locally).

All paths in this spec are relative to that base. Example: list entities → `GET /v1/entities`
(full URL: `{server}/v1/entities`).

---

## Authentication

Most endpoints require an **API key** in the `X-API-Key` header.

1. Sign in to Entivia → **Settings → API Keys**
2. Create a key with scope **`read`** (GET only) or **`write`** (GET + POST)
3. Send the key on every request:

```bash
# Copy the base URL from the Servers box below, then:
curl -sS -H "X-API-Key: YOUR_KEY" \\
  "<your-public-api-base>/v1/entities?page=1&limit=10"
```

**JWT bearer tokens are not accepted** on this API — use API keys only.

### Key scopes

| Scope | Allowed methods |
|-------|-----------------|
| `read` | `GET` |
| `write` | `GET`, `POST`, `PATCH` |

Write-scoped keys are required for `POST /v1/recommendations/{id}/action`, `POST .../dismiss`, and `POST /v1/pipeline/trigger`.

---

## Response envelope

Authenticated endpoints wrap payloads as:

```json
{
  "data": { },
  "meta": {
    "org_id": "uuid-of-your-org",
    "api_version": "1"
  }
}
```

The `meta.org_id` always matches the organization bound to your API key.

---

## Errors

Failed requests return:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Entity profile not found"
  }
}
```

| HTTP | Code | When |
|------|------|------|
| 400 | `BAD_REQUEST` | Invalid ID or parameter |
| 401 | `INVALID_API_KEY` | Missing, invalid, or revoked key |
| 401 | `API_KEY_EXPIRED` | Key past its expiry |
| 403 | `INSUFFICIENT_SCOPE` | Write action with a read-only key |
| 404 | `NOT_FOUND` | Resource does not exist in your org |
| 409 | `PIPELINE_ALREADY_RUNNING` | Active pipeline run in progress |
| 422 | `VALIDATION_ERROR` | Request body or query validation failed |
| 429 | `RATE_LIMITED` | Per-key or per-IP limit exceeded |

---

## Rate limits

When **`REDIS_URL`** is configured:

| Key scope | Limit |
|-----------|--------|
| `read` | 30 requests / minute / API key |
| `write` | 10 requests / minute / API key |

**Studio** public dashboards (slug and embed URLs) are limited to **60 requests / minute / IP** and do not use API keys.

Without Redis, API-key rate limits are not enforced (not recommended for production).

---

## Entivia Studio (public dashboards)

The **Studio** tag covers **unauthenticated** endpoints for shared dashboards:

- **`GET /v1/studio/dashboards/{slug}`** — Public dashboard by shareable slug (`is_public=true` in Studio).
- **`GET /v1/studio/embed/{token}`** — Private dashboard via short-lived embed token from the authenticated Studio API.

Pass dashboard filter parameters as query strings (e.g. `?region=US&start_date=2025-01-01`). Responses are **not** wrapped in the `data` / `meta` envelope.

---

## Typical integration flow

1. **Poll** `GET /v1/entities` or `GET /v1/recommendations?status=open` on a schedule.
2. **Action** high-urgency items with `POST /v1/recommendations/{id}/action` (write key).
3. **Refresh** intelligence with `POST /v1/pipeline/trigger`, then **`GET /v1/pipeline/runs`** until `status` is `completed`.
4. **Report** with `GET /v1/analytics/overview?period=30d`.
"""


_PUBLIC_TAG_DESCRIPTIONS: dict[str, str] = {
    "Entities": (
        "Profiled entities from your connected database with risk scores, tiers, and narratives. "
        "Data reflects the latest completed pipeline run."
    ),
    "Recommendations": (
        "AI-generated next-best-action recommendations. "
        "List and fetch with a read key; mark actioned or dismissed with a write key."
    ),
    "Pipeline": (
        "Trigger on-demand pipeline runs and inspect run history. "
        "Only one run may be queued or running per organization at a time."
    ),
    "Analytics": (
        "Org-level aggregates: entity counts, risk distribution, open recommendations, and pipeline activity."
    ),
    "Studio": (
        "**No API key required.** View shareable Entivia Studio dashboards by slug or embed token. "
        "Rate-limited per IP when Redis is enabled."
    ),
}

_ERROR_SCHEMA_REF = {"$ref": "#/components/schemas/PublicErrorResponse"}

_COMMON_ERROR_RESPONSES = {
    "401": {
        "description": "Invalid, revoked, or expired API key",
        "content": {"application/json": {"schema": _ERROR_SCHEMA_REF}},
    },
    "403": {
        "description": "API key lacks required scope (e.g. write action with read key)",
        "content": {"application/json": {"schema": _ERROR_SCHEMA_REF}},
    },
    "429": {
        "description": "Rate limit exceeded",
        "content": {"application/json": {"schema": _ERROR_SCHEMA_REF}},
    },
}


def _hoist_defs_to_components(openapi: dict) -> None:
    """Move JSON Schema ``$defs`` into ``components/schemas`` (ReDoc 2.x compat).

    ReDoc cannot resolve ``#/`` references whose path starts with ``$defs``.
    Pydantic / OpenAPI 3.1 may nest ``$defs`` inside component schemas or
    inline response bodies; this hoists them and rewrites refs.
    """
    components = openapi.setdefault("components", {})
    schemas: dict = components.setdefault("schemas", {})
    renamed: dict[str, str] = {}

    while True:
        found = False
        for schema_name, body in list(schemas.items()):
            if not isinstance(body, dict):
                continue
            defs = body.get("$defs")
            if not defs:
                continue
            found = True
            body = {k: v for k, v in body.items() if k != "$defs"}
            schemas[schema_name] = body
            for def_name, def_schema in defs.items():
                comp_name = def_name
                if comp_name in schemas and schemas[comp_name] is not def_schema:
                    comp_name = f"{schema_name}_{def_name}"
                renamed[def_name] = comp_name
                schemas[comp_name] = def_schema
        if not found:
            break

    def _rewrite_refs(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                def_name = ref.removeprefix("#/$defs/")
                comp_name = renamed.get(def_name, def_name)
                node["$ref"] = f"#/components/schemas/{comp_name}"
            # Inline schemas accidentally embedded with a top-level $defs sibling
            if "$defs" in node and "$ref" not in node:
                inline_defs = node.pop("$defs")
                for def_name, def_schema in inline_defs.items():
                    comp_name = def_name
                    if comp_name in schemas and schemas[comp_name] is not def_schema:
                        comp_name = f"Inline_{def_name}"
                    renamed[def_name] = comp_name
                    schemas[comp_name] = def_schema
                _rewrite_refs(node)
            else:
                for value in node.values():
                    _rewrite_refs(value)
        elif isinstance(node, list):
            for item in node:
                _rewrite_refs(item)

    _rewrite_refs(openapi)


def public_api_base_url(request: Request, mount_path: str = PUBLIC_API_MOUNT_PATH) -> str:
    """Build the public API base URL from the incoming request (honors reverse-proxy headers)."""
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    scheme = (
        forwarded_proto.split(",")[0].strip()
        if forwarded_proto
        else request.url.scheme
    )
    host = (
        forwarded_host.split(",")[0].strip()
        if forwarded_host
        else request.headers.get("host", request.url.netloc)
    )
    mount = mount_path if mount_path.startswith("/") else f"/{mount_path}"
    return f"{scheme}://{host}{mount.rstrip('/')}"


def _strip_default_openapi_route(public_app: FastAPI, openapi_path: str) -> None:
    """Remove FastAPI's built-in /openapi.json handler so we can serve a host-aware one."""
    public_app.router.routes = [
        route
        for route in public_app.router.routes
        if not (
            getattr(route, "path", None) == openapi_path
            and "GET" in (getattr(route, "methods", None) or ())
        )
    ]


def configure_public_openapi(
    public_app: FastAPI,
    *,
    mount_path: str = PUBLIC_API_MOUNT_PATH,
) -> None:
    """Attach a customized OpenAPI generator to the public sub-app."""

    def custom_openapi() -> dict:
        if public_app.openapi_schema:
            return public_app.openapi_schema

        schema = get_openapi(
            title=public_app.title,
            version=public_app.version,
            description=_PUBLIC_API_DESCRIPTION,
            routes=public_app.routes,
            tags=public_app.openapi_tags,
            # ReDoc 2.5.x does not fully support OpenAPI 3.1 / JSON Schema $defs.
            openapi_version="3.0.2",
        )

        schema["info"]["contact"] = public_app.contact
        schema["info"]["license"] = {
            "name": "Proprietary",
            "url": "https://entivia.online/terms",
        }

        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": (
                    "Organization API key from **Settings → API Keys**. "
                    "Use a `read` key for GET endpoints and a `write` key for POST actions."
                ),
            }
        }
        schema["security"] = [{"ApiKeyAuth": []}]

        for path, path_item in schema.get("paths", {}).items():
            if any(path.startswith(prefix) for prefix in _UNAUTHENTICATED_PATH_PREFIXES):
                for method in path_item.values():
                    if isinstance(method, dict):
                        method["security"] = []
                continue
            for method in path_item.values():
                if not isinstance(method, dict):
                    continue
                responses = method.setdefault("responses", {})
                for code, spec in _COMMON_ERROR_RESPONSES.items():
                    responses.setdefault(code, spec)

        if schema.get("tags"):
            desc_by_name = _PUBLIC_TAG_DESCRIPTIONS
            for tag in schema["tags"]:
                if tag.get("name") in desc_by_name:
                    tag["description"] = desc_by_name[tag["name"]]

        _hoist_defs_to_components(schema)

        # Fallback when openapi.json is read without a Request (e.g. codegen tools).
        schema["servers"] = [
            {
                "url": mount_path,
                "description": "Relative to the host that serves /api/public/openapi.json",
            }
        ]

        public_app.openapi_schema = schema
        return schema

    public_app.openapi = custom_openapi  # type: ignore[method-assign]

    openapi_path = public_app.openapi_url or "/openapi.json"
    _strip_default_openapi_route(public_app, openapi_path)

    @public_app.get(openapi_path, include_in_schema=False)
    async def public_openapi_json(request: Request) -> JSONResponse:
        schema = copy.deepcopy(public_app.openapi())
        schema["servers"] = [
            {
                "url": public_api_base_url(request, mount_path),
                "description": "Detected from the host serving this documentation",
            }
        ]
        return JSONResponse(schema)

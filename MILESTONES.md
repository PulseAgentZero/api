# Pulse — Engineering Milestones, Feature Spec & Schema

## Product in One Sentence
Pulse lets any organization connect their data sources (databases, spreadsheets, S3, cloud warehouses), then runs autonomous AI agents to surface behavioral intelligence, risk scores, and actionable recommendations — all without ever copying their data.

---

## Deployment Modes

### Cloud (Managed)
- Hosted by Pulse team. User signs up, verifies email, connects their data.
- No API key or license key entry in the UI.
- Billing via recurring **Paystack** subscriptions (`free`, `growth`, `pro`). Card details collected on Paystack checkout; renewals via webhooks. Feature access tied to effective plan (includes payment-failure grace period).
- Backend knows it's cloud via `DEPLOYMENT_MODE=cloud` env var.

### Self-Hosted
- User runs the Docker image from Docker Hub (`pulseai/pulse:latest`).
- Their `docker-compose.yml` points to their own Postgres instance via env var (`DATABASE_URL`).
- First screen after install is **Login** (no landing page).
- In **Settings → License**, user enters:
  - Their own LLM API keys (Anthropic, Groq, etc.)
  - Their Pulse **License Key** (purchased from pulseai.io) to unlock Pro features.
- Free tier: near-full access since the user is running everything on their own infrastructure. Only enterprise-grade operational features (audit log, SSO/SAML) are behind Pro.
- Pro tier: adds audit log, SSO, white-labelling, and priority support. Unlocked with a valid license key (validated against Pulse license server; works offline for up to 7 days after last contact).

---

## Current State (Both Repos)

### Backend (`/api`)
| Area | Status |
|---|---|
| FastAPI app, routing, middleware | Done |
| JWT auth (login, signup, refresh) | Done |
| Role-based access (4 roles) | Done |
| PostgreSQL / MySQL / SQLite / MSSQL connections | Done |
| 4-stage agent pipeline (schema → profile → risk → recommend) | Done (needs polish) |
| Conversational agent with tool calling | Done |
| Background pipeline scheduler (APScheduler) | Done |
| Email verification | **Not built** |
| Password reset | **Not built** |
| Email sending (SMTP/Resend) | **Not built** |
| User invitation flow | **Not built** |
| Google Sheets / S3 / Snowflake / ClickHouse / BigQuery connectors | **Not built** |
| License key system | **Not built** |
| Feature flags / plan gating | **Not built** |
| API key management (external access) | **Not built** |
| Streaming LLM responses (SSE) | **Not built** |
| Webhook delivery | **Not built** |
| Audit logging | **Not built** |
| Usage tracking | **Not built** |

### Frontend (`/dashboard`)
| Area | Status |
|---|---|
| All 14 pages built with Tailwind (UI only) | Done |
| TypeScript strict mode, Next.js App Router | Done |
| Demo data (hardcoded) | Done |
| **Zero backend API integration** | **Not built** |
| Auth state, token storage, protected routes | **Not built** |
| Email verification page | **Not built** |
| Password reset pages | **Not built** |
| OAuth callback handler | **Not built** |
| Loading states, error handling, toast notifications | **Not built** |
| Streaming chat responses | **Not built** |
| Self-hosted mode (license key entry, LLM key settings) | **Not built** |

---

## Milestone Map

```
M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9 → M10 → M11
Foundation  Auth  Connectors  Onboarding  Pipeline  Agent  Analytics  Alerts  Team  Self-Host  API  Launch
```

---

## M0 — Foundation & DX Setup

**Goal:** Clean, wired development environment before any feature work.

### Backend
- Populate the `Makefile` with standard commands:
  ```makefile
  dev:       uvicorn app.api.app:app --reload
  migrate:   alembic upgrade head
  revision:  alembic revision --autogenerate -m "$(name)"
  seed:      python scripts/db/seed_telecom_db.py
  reset-db:  python scripts/db/reset_db.py
  ```
- Add `pytest` + `httpx` async test client. Seed a test DB fixture.
- Add `ruff` for linting + `mypy` for type checking.
- Create `.env.example` covering every env var with descriptions.

### Frontend
- Install `axios` (or native `fetch` wrapper) + `@tanstack/react-query` for data fetching and caching.
- Create `src/lib/api-client.ts` — Axios instance with base URL from `NEXT_PUBLIC_API_URL`, auto-attaches `Authorization: Bearer <token>` from storage.
- Create `src/lib/auth-store.ts` — Zustand store for `{ user, accessToken, refreshToken, isAuthenticated }`.
- Add `next-auth` or custom middleware for protected route enforcement (redirect to `/auth/login` if unauthenticated).
- Install `sonner` (toast notifications) and `react-hook-form` + `zod` for form handling.
- Add `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_DEPLOYMENT_MODE` env vars.

---

## M1 — Authentication & User Management

**Goal:** Complete, production-grade auth flow for both deployment modes.

### Backend

#### Email Verification
- On signup, generate a short-lived token (UUID, stored in `email_verification_tokens` table), send via email with link.
- `GET /api/v1/auth/verify-email?token=<token>` — marks user as verified.
- Unverified users: can log in but see a banner; cannot run pipeline or connect data sources.

#### Password Reset
- `POST /api/v1/auth/forgot-password` — generates reset token, emails link.
- `POST /api/v1/auth/reset-password` — validates token, hashes new password, invalidates token.

#### JWT Rotation
- Issue short-lived access tokens (15m) and longer refresh tokens (7d).
- `POST /api/v1/auth/refresh` — swaps refresh token for new access + refresh pair.
- Store refresh tokens in DB (for revocation on logout / suspicious activity).

#### OAuth (Google)
- `GET /api/v1/auth/oauth/google` — redirect to Google consent.
- `GET /api/v1/auth/oauth/google/callback` — exchange code, upsert user, issue tokens.

#### Email Service
- Wrap Resend (or SMTP via `fastapi-mail`) into `app/infrastructure/email/sender.py`.
- Templates: verify email, reset password, invitation, pipeline complete digest.

#### Self-Hosted: LLM Key Injection
- `PUT /api/v1/settings/llm-keys` — stores encrypted Anthropic/Groq API keys per org.
- Backend reads these instead of global env vars when `DEPLOYMENT_MODE=self-hosted`.

### Frontend

#### New Pages
- `/auth/verify-email` — shows success/error state from `?token=` param.
- `/auth/forgot-password` — email input form, POST to backend.
- `/auth/reset-password?token=` — new password form.
- `/auth/callback` — OAuth callback, receives tokens, stores in Zustand, redirects to dashboard.

#### Auth Wiring
- Wire login form → `POST /api/v1/auth/login` → store tokens → redirect to `/dashboard`.
- Wire signup form → `POST /api/v1/auth/signup` → redirect to `/auth/verify-email` notice page.
- Implement auto-refresh: intercept 401 responses, call `/auth/refresh`, retry original request.
- `middleware.ts` — Next.js edge middleware checking for valid token cookie; redirect unauthenticated requests.
- Add "verify your email" banner in dashboard layout when `user.is_verified === false`.

---

## M2 — Data Source Connectors

**Goal:** Support every meaningful data source, not just SQL databases.

### Connector Types

| Connector | Auth Method | Read Approach |
|---|---|---|
| PostgreSQL | DSN / credentials | asyncpg |
| MySQL | DSN / credentials | aiomysql |
| MSSQL | DSN / credentials | aioodbc |
| SQLite | File path | aiosqlite |
| Redshift | DSN + IAM | asyncpg (Redshift speaks Postgres wire) |
| Snowflake | Account + user + private key / OAuth | snowflake-connector-python (async wrapper) |
| ClickHouse | HTTP DSN | clickhouse-driver / httpx to HTTP interface |
| BigQuery | Service account JSON / OAuth | google-cloud-bigquery (async) |
| Google Sheets | OAuth2 / Service account | googleapis (Sheets API v4) |
| S3 / GCS | Access key + secret / IAM | boto3 / google-cloud-storage — list objects, download Parquet/CSV/JSON |
| CSV / Excel Upload | File upload | Store to temp storage, parse with polars |
| Airtable | API key | Airtable REST API |
| MongoDB | Connection string | motor (async) — read-only via `find`, `aggregate` |

### Backend Changes

#### Generalized Connection Model
Replace the single `encrypted_dsn` field approach with a structured `credentials` JSONB field that captures connector-specific auth config. The `connector_type` enum expands to cover all types above.

```
connections.connector_type: enum (postgres, mysql, mssql, sqlite, redshift, snowflake, clickhouse, bigquery, google_sheets, s3, gcs, csv, airtable, mongodb)
connections.credentials: JSONB  # encrypted blob with connector-specific fields
connections.metadata: JSONB     # display info: table count, row count estimate, last synced
connections.status: enum (pending, active, error, disabled)
connections.last_tested_at: timestamp
```

#### Connector Abstraction Layer
- `app/connectors/base.py` — `BaseConnector` abstract class: `test()`, `list_tables()`, `describe_table()`, `execute_query()`, `sample_rows()`.
- One subclass per connector type in `app/connectors/`.
- `ConnectorFactory.create(connection_record)` returns the right subclass.
- All SQL-based connectors share `app/connectors/sql_base.py` (SQLAlchemy dialect dispatch).
- File-based connectors (CSV, S3, GCS) normalize data to a virtual table abstraction so agents treat them identically to SQL tables.

#### API Endpoints
- `POST /api/v1/connections` — create connection (stores encrypted credentials).
- `GET /api/v1/connections` — list all org connections.
- `GET /api/v1/connections/:id` — single connection details.
- `PUT /api/v1/connections/:id` — update credentials.
- `DELETE /api/v1/connections/:id` — soft-delete.
- `POST /api/v1/connections/:id/test` — run connector.test(), return latency + table count.
- `GET /api/v1/connections/:id/tables` — list available tables/sheets/objects.
- `GET /api/v1/connections/:id/tables/:table/preview` — first 50 rows of a table.
- `POST /api/v1/connections/upload` — CSV/Excel file upload (returns a transient connection_id).

### Frontend

#### Connections Page (wire up)
- Replace hardcoded 4 cards with fetched list from `/api/v1/connections`.
- "Add Connection" modal with connector type picker (icons grid), then dynamic form per connector type.
- Test connection button → calls `/connections/:id/test` → shows latency badge + green/red status.
- Show table count and last-synced time on connection cards.
- File upload zone for CSV/Excel with progress bar.
- Google OAuth flow for Google Sheets: popup → callback → store connection.

---

## M3 — Onboarding Wizard

**Goal:** Guide a new org through setup in under 5 minutes. Wire the existing 4-step UI to real backend calls.

### Steps

**Step 1 — Business Context**
- Form: org name, industry (dropdown), describe what you're trying to optimize (free text).
- `PUT /api/v1/organization` — saves context.
- Used by agents to tailor prompts and recommendation language.

**Step 2 — Connect Data**
- Embed the connection creation flow from M2.
- User adds at least one connection and tests it successfully before proceeding.

**Step 3 — Schema Mapping**
- After selecting a connection, fetch its table list.
- User picks: entity table (who/what is being tracked), entity ID column, up to 10 behavioral signal columns (with their types auto-detected), and the primary goal.
- `POST /api/v1/schema-mappings` — saves mapping.
- Preview: backend runs `sample_rows()` and renders a mini-table so the user can verify they picked the right columns.

**Step 4 — Run First Intelligence Pass**
- Button: "Run First Analysis" → `POST /api/v1/pipeline/trigger`.
- SSE stream endpoint `GET /api/v1/pipeline/:run_id/stream` pushes `{ stage, status, message }` events.
- Frontend shows a live progress indicator (4 stages, each completing in order).
- On completion, redirect to `/dashboard`.
- Mark `organizations.onboarding_done = true`.

---

## M4 — Intelligence Pipeline (Harden & Expand)

**Goal:** Make the existing 4-stage pipeline reliable, observable, and configurable.

### Current gaps to fix
- No streaming progress to frontend (only DB polling).
- Schema Intelligence Agent doesn't handle non-SQL sources (Sheets, S3).
- Risk scoring thresholds are static; need per-org customization via schema_mappings.
- No retry/backoff on LLM calls that return malformed JSON.
- Pipeline fails silently when client DB is unreachable; should set run status to `error` with a human-readable reason.

### Enhancements

#### SSE Progress Streaming
- `GET /api/v1/pipeline/:run_id/stream` returns `text/event-stream`.
- Orchestrator emits events: `{ event: "stage_start", data: { stage: "profiling", entity_count: 1200 } }`.
- Frontend subscribes via `EventSource` and animates the onboarding wizard or a pipeline status widget in the dashboard header.

#### Multi-Source Pipeline
- Schema Intelligence Agent must call `connector.list_tables()` and `connector.describe_table()` generically, not assume SQL.
- Profiling Agent queries through the connector abstraction rather than raw SQL when source is Sheets, S3, etc.

#### Configurable Risk Thresholds
- `schema_mappings.risk_config` already exists as JSONB. Expose it in Settings UI.
- Allow per-signal threshold configuration: `{ signal: "churn_score", high_risk: "> 0.8", low_risk: "< 0.3" }`.

#### Pipeline Scheduling
- `POST /api/v1/pipeline/schedule` — set cron expression per org (e.g., `0 */6 * * *` = every 6h).
- `GET /api/v1/pipeline/runs` — paginated run history with status, duration, entity count, errors.
- `GET /api/v1/pipeline/runs/:id` — detailed run log.

#### Entity Profile Cache
- After profiling stage, store profile JSON in `entity_profiles` table (keyed by `org_id + entity_id`).
- Dashboard entity detail page reads from cache; agent can also read from cache for faster responses.
- Cache invalidated on next pipeline run.

---

## M5 — Conversational Agent (Streaming + Polish)

**Goal:** Full streaming chat, multi-turn memory, tool transparency, and conversation history.

### Backend

#### Streaming Endpoint
- Replace `POST /api/v1/agent/chat` (returns full response) with SSE:
  - `POST /api/v1/agent/conversations` — creates conversation, returns `conversation_id`.
  - `POST /api/v1/agent/conversations/:id/messages` — sends a message, returns `text/event-stream`.
  - Stream tokens as `{ event: "token", data: "..." }`, tool calls as `{ event: "tool_call", data: { name, args } }`, tool results as `{ event: "tool_result", data: { name, result } }`, done as `{ event: "done" }`.

#### Tool Transparency
- Each tool call and its result is stored in `agent_conversations.messages` JSONB alongside the assistant turns.
- Frontend renders these as collapsible "tool call" blocks in the chat.

#### Conversation History
- `GET /api/v1/agent/conversations` — list all conversations for the org (paginated).
- `GET /api/v1/agent/conversations/:id` — full message history.
- `DELETE /api/v1/agent/conversations/:id` — delete conversation.

#### Memory
- `agent_memory` table already exists. Extend to support:
  - Short-term: scoped to conversation (TTL = conversation duration).
  - Long-term: cross-conversation org-level facts the agent has learned ("This org is a B2B SaaS company with 90-day sales cycles").
  - Users can view and delete stored memories from Settings.

### Frontend

#### Chat Wiring
- Use `EventSource` or `fetch` with `ReadableStream` to consume SSE.
- Stream tokens into message bubbles character-by-character.
- Show tool call blocks with a spinner while running, then collapse to a summary when done.
- Input: Enter to send, Shift+Enter for newline. Disabled while streaming.
- Conversation list in a left panel inside `/dashboard/agent`.
- New conversation button.

---

## M6 — Analytics & Reporting

**Goal:** Real time-series analytics pulled from pipeline run history and entity risk history.

### Backend

#### New: `entity_risk_history` table
- Stores `{ org_id, entity_id, risk_score, risk_tier, signals_snapshot, recorded_at }` after each pipeline run.
- Enables trend charts (30/60/90 day views).

#### Analytics Endpoints
- `GET /api/v1/analytics/overview?period=30d` — aggregate stats: avg risk, entity count by tier, recommendation acceptance rate.
- `GET /api/v1/analytics/risk-trend?period=90d&granularity=week` — time-series risk score distribution.
- `GET /api/v1/analytics/segments` — risk breakdown by org-defined segments.
- `GET /api/v1/analytics/cohorts` — behavioral cohort movement (entities shifting between tiers over time).
- `GET /api/v1/analytics/pipeline-performance` — avg pipeline duration, entity throughput, LLM cost estimate per run.
- `POST /api/v1/analytics/export` — trigger async CSV/Excel export job; `GET /api/v1/analytics/exports/:id` to download.

### Frontend
- Wire analytics page to real endpoints.
- Use `recharts` or `tremor` for chart components (time series, bar, funnel, scatter).
- Period picker (7d / 30d / 90d / custom).
- Export button → polls export job → triggers browser download.

---

## M7 — Alerts & Notifications

**Goal:** Notify stakeholders when risk thresholds are crossed or pipeline completes.

### Alert Rule Engine (Backend)
- After each pipeline run, evaluate all `alert_rules` for the org.
- Rule conditions: `{ metric: "risk_score", operator: ">", threshold: 0.8, entity_filter: { segment: "enterprise" } }`.
- On match, create `alert_events` records and trigger configured `notification_channels`.

#### Notification Channels
| Channel | Implementation |
|---|---|
| In-app | Insert to `notifications` table; frontend polls `GET /api/v1/notifications` |
| Email | Send via Resend/SMTP with entity list + action link |
| Webhook | POST to configured URL with HMAC-signed payload |
| Slack (Pro) | Slack Incoming Webhook or Bot token |

#### Endpoints
- `GET/POST/PUT/DELETE /api/v1/alerts/rules` — CRUD for alert rules.
- `GET/POST/PUT/DELETE /api/v1/alerts/channels` — CRUD for notification channels.
- `GET /api/v1/alerts/events` — paginated alert history.
- `GET /api/v1/notifications` — in-app notification inbox.
- `POST /api/v1/notifications/:id/read` — mark read.
- `POST /api/v1/alerts/channels/:id/test` — send a test notification.

### Frontend
- Wire alerts page to real CRUD endpoints.
- Notification bell in top nav with unread count badge.
- Notification dropdown with recent 10 items.
- Alert rule builder: metric picker, operator, threshold, entity filter, delivery channel multi-select.

---

## M8 — Team & RBAC

**Goal:** Invite teammates, manage roles, track activity.

### Backend

#### Invitation Flow
- `POST /api/v1/users/invite` — Admin creates invitation (email + role), generates token, sends email.
- `GET /api/v1/auth/accept-invite?token=` — validates token, creates user account with pre-set role, redirects to set-password.
- Invitations expire after 72h.

#### User Management
- `GET /api/v1/users` — list all users in org (Admin only).
- `PUT /api/v1/users/:id/role` — change role (Admin only).
- `DELETE /api/v1/users/:id` — deactivate user (Admin only).
- `GET /api/v1/users/me` — current user profile.
- `PUT /api/v1/users/me` — update name, avatar URL.

#### Audit Log
- Every sensitive action (login, invite, role change, connection create/delete, pipeline trigger, recommendation action) writes to `audit_logs`.
- `GET /api/v1/audit-logs` — paginated (Admin only).

### Frontend
- Wire team page: fetch real user list, send invites via form, change role via dropdown.
- Accept-invite page: `/auth/accept-invite?token=` — set password form.
- User profile menu in nav: avatar, name, role, "My Profile", "Sign Out".
- Audit log tab in Settings → Security section.

---

## M9 — Self-Hosted Mode

**Goal:** Make self-hosted a first-class experience with proper feature gating.

### License Key System

#### Backend
- `POST /api/v1/license/activate` — org submits license key.
  - Backend calls Entivia license server: `POST https://license.entivia.online/validate` with `{ license_key, org_id, version, machine_fingerprint }`.
  - On success: stores `{ plan, features, expires_at, seat_limit }` in `license_keys` table.
  - Caches validation result for 7 days (so offline operation works up to 7 days after last contact).
- `GET /api/v1/license` — returns current license status, plan, features, expiry.
- `POST /api/v1/license/refresh` — re-validates against license server.

#### License Server (Separate Microservice)
- Manages license key issuance, plan lookup, revocation.
- Returns signed JWT that self-hosted instance can verify with embedded public key (no phone-home needed for day-to-day feature checks).

#### Feature Flag Middleware
- `app/api/dependencies/feature_flags.py` — FastAPI dependency `require_feature("advanced_analytics")`.
- Checks `license_keys.features` JSONB array (or `DEPLOYMENT_MODE=cloud` which bypasses all checks).
- Returns `HTTP 402 Payment Required` with `{ feature: "...", upgrade_url: "https://pulseai.io/pricing" }` if feature is locked.

#### Self-Hosted UI Differences
- Login page is the entry point (no landing page).
- Settings → License tab: show current plan, expiry, seat usage, "Activate License" input.
- Settings → LLM Keys tab: enter Anthropic, Groq API keys (encrypted, stored in DB). Show token usage counter.
- Locked features show a "Pro Feature" badge with an upgrade CTA instead of the UI.

### Docker Hub Setup
- `Dockerfile.backend` and `Dockerfile.worker` already exist in `deploy/docker/`.
- Add `docker-compose.self-hosted.yml` in repo root:
  ```yaml
  services:
    api:    image: pulseai/pulse-api:latest
    worker: image: pulseai/pulse-worker:latest
    db:     image: postgres:16-alpine
  ```
- CI pipeline (GitHub Actions) builds and pushes images to Docker Hub on each release tag.
- Add `DEPLOYMENT_MODE`, `LICENSE_SERVER_URL`, `LICENSE_SERVER_PUBLIC_KEY` env vars.

---

## M10 — External API & Integrations

**Goal:** Let power users and enterprises automate Pulse via API.

### API Keys
- `POST /api/v1/api-keys` — generate API key (scoped: read-only, full-access).
- `GET /api/v1/api-keys` — list keys (show prefix only, not full key).
- `DELETE /api/v1/api-keys/:id` — revoke.
- API keys authenticate via `X-API-Key` header as an alternative to JWT Bearer.

### Webhooks (Outbound)
- Already in M7. Extend: org can subscribe to specific event types:
  - `pipeline.completed`, `entity.risk_changed`, `recommendation.created`, `alert.triggered`.
- Webhook delivery with retry (3 attempts with exponential backoff), stored in `webhook_deliveries` table.
- `GET /api/v1/webhooks/deliveries` — delivery log.

### Embeds (Cloud only, future)
- Shareable read-only dashboard link with HMAC-signed token.
- Embedded chart widget (`<iframe>`) for a single entity or metric.

---

## M11 — Production Hardening & Launch

### Performance
- Add `Redis` caching for frequently read data (org context, entity list, schema mappings) with 5-minute TTL.
- `GET /api/v1/entities` — add cursor-based pagination (default page size 50).
- Database: add indexes on `org_id`, `entity_id`, `created_at`, `status` columns across all tables.
- Rate limiting: `slowapi` middleware — 60 req/min for regular endpoints, 10 req/min for pipeline trigger.

### Observability
- Structured JSON logging already partially in place. Ensure every request logs `org_id`, `user_id`, `duration_ms`, `status`.
- Add `POST /api/v1/pipeline/runs/:id/logs` endpoint for streaming structured run logs to frontend.
- Integrate Sentry (both backend and frontend) for error tracking.
- Health check `/health` already exists. Add `/readiness` and `/liveness` for Kubernetes probes.

### Security
- Content Security Policy headers.
- Rate limit auth endpoints more aggressively (5 req/min for login/signup).
- Rotate `ENCRYPTION_KEY` workflow (re-encrypt all connections on key rotation).
- OWASP top-10 pass before launch.

### Frontend
- SEO: `metadata` in `layout.tsx` — title, description, Open Graph tags.
- Loading skeletons for all data-fetching components.
- Empty states for all list views (no entities yet, no recommendations, etc.).
- Error boundaries per page section.
- Keyboard navigation for chat (↑/↓ through message history).

---

## Feature Matrix — Cloud vs Self-Hosted

**Cloud Free** — any connector type, but only 1 active connection at a time. Upgrade to Pro for multiple simultaneous connections and higher limits.

**Self-Hosted Free** — near-full access by default since the user owns the infrastructure. Only audit log and SSO are gated behind a Pro license key.

| Feature | Cloud Free | Cloud Pro | Self-Hosted Free | Self-Hosted Pro |
|---|:---:|:---:|:---:|:---:|
| **Connections** | | | | |
| Active connections at once | **1** | Unlimited | Unlimited | Unlimited |
| Connector types (SQL, warehouses, Sheets, S3, CSV…) | **All types** | All types | All types | All types |
| **Intelligence** | | | | |
| Entities tracked | 1,000 | Unlimited | Unlimited | Unlimited |
| Pipeline runs per day | 2 | Unlimited | Unlimited | Unlimited |
| Recommendations | ✅ | ✅ | ✅ | ✅ |
| Entity risk history (time-series) | ✅ | ✅ | ✅ | ✅ |
| **Agent** | | | | |
| Agent chat messages | 50 / mo | Unlimited | Unlimited | Unlimited |
| Conversation history | ✅ | ✅ | ✅ | ✅ |
| Long-term agent memory | ❌ | ✅ | ✅ | ✅ |
| **Analytics** | | | | |
| Analytics & risk trends | ❌ | ✅ | ✅ | ✅ |
| Behavioral cohorts | ❌ | ✅ | ✅ | ✅ |
| Data export (CSV/Excel) | ❌ | ✅ | ✅ | ✅ |
| **Alerts & Notifications** | | | | |
| Alert rules | 3 | Unlimited | Unlimited | Unlimited |
| Email notifications | ✅ | ✅ | ✅ (own SMTP) | ✅ |
| Slack notifications | ❌ | ✅ | ✅ | ✅ |
| Webhook delivery | ❌ | ✅ | ✅ | ✅ |
| **Team** | | | | |
| Team members | 3 | Unlimited | Unlimited | Unlimited |
| Role-based access (4 roles) | ✅ | ✅ | ✅ | ✅ |
| User invitations | ✅ | ✅ | ✅ | ✅ |
| **Developer** | | | | |
| API key access | ❌ | ✅ | ✅ | ✅ |
| Custom LLM API keys | N/A | N/A | Required | Required |
| **Enterprise** | | | | |
| Audit log | ❌ | ✅ | ❌ | ✅ |
| SSO (SAML / OIDC) | ❌ | ❌ | ❌ | ✅ |
| White-label / custom domain | ❌ | ❌ | ❌ | ✅ |
| Priority support | ❌ | ✅ | ❌ | ✅ |

---

## Complete Database Schema

### Design Principles
- Every table has `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`.
- Every table has `created_at TIMESTAMPTZ DEFAULT NOW()` and `updated_at TIMESTAMPTZ`.
- Multi-tenant: every org-scoped table has `org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE`.
- Soft deletes via `deleted_at TIMESTAMPTZ` where data must be preserved for audit.
- Sensitive fields (credentials, tokens) are always encrypted at the application layer before storage.

---

### Core Platform Tables

```sql
-- ─────────────────────────────────────────────
-- ORGANIZATIONS
-- ─────────────────────────────────────────────
CREATE TABLE organizations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    slug             TEXT UNIQUE NOT NULL,           -- URL-safe identifier
    industry         TEXT,
    business_context TEXT,                           -- free-text description for agents
    goal             TEXT,                           -- primary optimization goal
    logo_url         TEXT,
    timezone         TEXT DEFAULT 'UTC',
    onboarding_done  BOOLEAN DEFAULT FALSE,
    plan             TEXT DEFAULT 'free',            -- free | pro | enterprise
    deployment_mode  TEXT DEFAULT 'cloud',           -- cloud | self_hosted
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- USERS
-- ─────────────────────────────────────────────
CREATE TYPE user_role AS ENUM ('admin', 'manager', 'analyst', 'viewer');

CREATE TABLE users (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email             TEXT NOT NULL UNIQUE,
    hashed_password   TEXT,                          -- NULL if OAuth-only
    full_name         TEXT NOT NULL,
    avatar_url        TEXT,
    role              user_role NOT NULL DEFAULT 'analyst',
    is_active         BOOLEAN DEFAULT TRUE,
    is_verified       BOOLEAN DEFAULT FALSE,         -- email verified
    auth_provider     TEXT DEFAULT 'email',          -- email | google | github
    auth_provider_id  TEXT,                          -- external OAuth subject
    last_login_at     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_org_id ON users(org_id);
CREATE INDEX idx_users_email ON users(email);

-- ─────────────────────────────────────────────
-- EMAIL VERIFICATION TOKENS
-- ─────────────────────────────────────────────
CREATE TABLE email_verification_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- PASSWORD RESET TOKENS
-- ─────────────────────────────────────────────
CREATE TABLE password_reset_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- REFRESH TOKENS (JWT rotation)
-- ─────────────────────────────────────────────
CREATE TABLE refresh_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,               -- store SHA-256 hash, not raw
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- USER INVITATIONS
-- ─────────────────────────────────────────────
CREATE TABLE invitations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    invited_by   UUID NOT NULL REFERENCES users(id),
    email        TEXT NOT NULL,
    role         user_role NOT NULL DEFAULT 'analyst',
    token        TEXT NOT NULL UNIQUE,
    expires_at   TIMESTAMPTZ NOT NULL,
    accepted_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_invitations_org_id ON invitations(org_id);
CREATE INDEX idx_invitations_token ON invitations(token);

-- ─────────────────────────────────────────────
-- API KEYS
-- ─────────────────────────────────────────────
CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by  UUID NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,                     -- first 8 chars shown in UI
    key_hash    TEXT NOT NULL UNIQUE,              -- SHA-256 of full key
    scope       TEXT DEFAULT 'read',               -- read | write | admin
    last_used_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_api_keys_org_id ON api_keys(org_id);
```

---

### Data Source Tables

```sql
-- ─────────────────────────────────────────────
-- CONNECTIONS (data sources)
-- ─────────────────────────────────────────────
CREATE TYPE connector_type AS ENUM (
    'postgres', 'mysql', 'mssql', 'sqlite',
    'redshift', 'snowflake', 'clickhouse', 'bigquery',
    'google_sheets', 's3', 'gcs',
    'csv', 'excel',
    'airtable', 'mongodb'
);

CREATE TYPE connection_status AS ENUM ('pending', 'active', 'error', 'disabled');

CREATE TABLE connections (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    connector_type   connector_type NOT NULL,
    credentials      TEXT NOT NULL,                -- Fernet-encrypted JSON blob
    -- connector-specific config (not sensitive — not encrypted)
    config           JSONB DEFAULT '{}',           -- e.g. { "database": "...", "schema": "public" }
    status           connection_status DEFAULT 'pending',
    last_tested_at   TIMESTAMPTZ,
    last_test_error  TEXT,
    metadata         JSONB DEFAULT '{}',           -- { table_count, row_estimate, last_synced_at }
    deleted_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_connections_org_id ON connections(org_id);

-- ─────────────────────────────────────────────
-- SCHEMA MAPPINGS
-- ─────────────────────────────────────────────
CREATE TABLE schema_mappings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    connection_id       UUID NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    name                TEXT NOT NULL DEFAULT 'Default',
    entity_table        TEXT NOT NULL,
    entity_id_column    TEXT NOT NULL,
    entity_name_column  TEXT,
    entity_type         TEXT DEFAULT 'customer',   -- customer | user | product | asset | route | etc.
    signal_columns      JSONB NOT NULL DEFAULT '[]',
    -- e.g. [{ "column": "churn_score", "label": "Churn Risk", "type": "float", "direction": "higher_is_worse" }]
    risk_config         JSONB NOT NULL DEFAULT '{}',
    -- e.g. { "high": "> 0.8", "medium": "> 0.5", "signals": { "churn_score": { "high": 0.8 } } }
    segment_column      TEXT,                      -- optional column for segmenting entities
    time_column         TEXT,                      -- optional timestamp column for trend analysis
    goal                TEXT,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_schema_mappings_org_id ON schema_mappings(org_id);
CREATE INDEX idx_schema_mappings_connection_id ON schema_mappings(connection_id);
```

---

### Intelligence Pipeline Tables

```sql
-- ─────────────────────────────────────────────
-- PIPELINE RUNS
-- ─────────────────────────────────────────────
CREATE TYPE pipeline_status AS ENUM ('queued', 'running', 'completed', 'failed', 'cancelled');

CREATE TABLE pipeline_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    mapping_id      UUID REFERENCES schema_mappings(id),
    triggered_by    UUID REFERENCES users(id),     -- NULL if scheduler-triggered
    trigger_type    TEXT DEFAULT 'scheduler',      -- scheduler | manual | api
    status          pipeline_status DEFAULT 'queued',
    current_stage   TEXT,                          -- schema_intelligence | profiling | risk_scoring | recommendation
    stages_log      JSONB DEFAULT '[]',
    -- [{ "stage": "profiling", "status": "completed", "started_at": "...", "duration_ms": 4200, "entity_count": 1200 }]
    entity_count    INTEGER,
    metrics         JSONB DEFAULT '{}',
    -- { "high_risk": 42, "new_recommendations": 18, "llm_tokens_used": 120000 }
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_pipeline_runs_org_id ON pipeline_runs(org_id);
CREATE INDEX idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX idx_pipeline_runs_created_at ON pipeline_runs(created_at DESC);

-- ─────────────────────────────────────────────
-- PIPELINE SCHEDULE
-- ─────────────────────────────────────────────
CREATE TABLE pipeline_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    mapping_id      UUID REFERENCES schema_mappings(id),
    cron_expression TEXT NOT NULL DEFAULT '0 */6 * * *',
    timezone        TEXT DEFAULT 'UTC',
    is_active       BOOLEAN DEFAULT TRUE,
    next_run_at     TIMESTAMPTZ,
    last_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- ENTITY PROFILES (cached per pipeline run)
-- ─────────────────────────────────────────────
CREATE TABLE entity_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    entity_id       TEXT NOT NULL,                 -- the value of entity_id_column
    entity_name     TEXT,
    segment         TEXT,
    profile_data    JSONB NOT NULL DEFAULT '{}',   -- raw behavioral signals + aggregations
    risk_score      NUMERIC(4,3),                  -- 0.000 to 1.000
    risk_tier       TEXT,                          -- High | Medium | Low | Healthy
    risk_narrative  TEXT,                          -- LLM-generated explanation
    is_latest       BOOLEAN DEFAULT TRUE,          -- only latest per entity_id
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_entity_profiles_org_latest ON entity_profiles(org_id, entity_id, is_latest);
CREATE INDEX idx_entity_profiles_risk_tier ON entity_profiles(org_id, risk_tier);

-- ─────────────────────────────────────────────
-- ENTITY RISK HISTORY (time series)
-- ─────────────────────────────────────────────
CREATE TABLE entity_risk_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_id       TEXT NOT NULL,
    risk_score      NUMERIC(4,3) NOT NULL,
    risk_tier       TEXT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL,
    pipeline_run_id UUID REFERENCES pipeline_runs(id)
);

CREATE INDEX idx_risk_history_entity ON entity_risk_history(org_id, entity_id, recorded_at DESC);
CREATE INDEX idx_risk_history_recorded_at ON entity_risk_history(org_id, recorded_at DESC);

-- ─────────────────────────────────────────────
-- RECOMMENDATIONS
-- ─────────────────────────────────────────────
CREATE TYPE recommendation_status AS ENUM ('open', 'actioned', 'dismissed', 'escalated', 'expired');
CREATE TYPE recommendation_urgency AS ENUM ('critical', 'high', 'medium', 'low');

CREATE TABLE recommendations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    pipeline_run_id     UUID REFERENCES pipeline_runs(id),
    entity_id           TEXT NOT NULL,
    entity_name         TEXT,
    recommendation_type TEXT NOT NULL,             -- churn_risk | upsell | support | operational | etc.
    title               TEXT NOT NULL,
    urgency             recommendation_urgency NOT NULL DEFAULT 'medium',
    confidence_score    NUMERIC(4,3),
    reasoning           TEXT NOT NULL,
    suggested_action    TEXT,
    expected_impact     TEXT,
    status              recommendation_status DEFAULT 'open',
    actioned_by         UUID REFERENCES users(id),
    actioned_at         TIMESTAMPTZ,
    outcome_notes       TEXT,
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_recommendations_org_id ON recommendations(org_id);
CREATE INDEX idx_recommendations_entity_id ON recommendations(org_id, entity_id);
CREATE INDEX idx_recommendations_status ON recommendations(org_id, status);
CREATE INDEX idx_recommendations_urgency ON recommendations(org_id, urgency, status);
```

---

### Agent / Conversation Tables

```sql
-- ─────────────────────────────────────────────
-- AGENT CONVERSATIONS
-- ─────────────────────────────────────────────
CREATE TABLE agent_conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id),
    title       TEXT,                              -- auto-generated from first message
    messages    JSONB NOT NULL DEFAULT '[]',
    -- [{ "role": "user"|"assistant"|"tool", "content": "...", "tool_calls": [...], "created_at": "..." }]
    message_count INTEGER DEFAULT 0,
    last_message_at TIMESTAMPTZ,
    deleted_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_conversations_org_user ON agent_conversations(org_id, user_id);
CREATE INDEX idx_conversations_last_message ON agent_conversations(org_id, last_message_at DESC);

-- ─────────────────────────────────────────────
-- AGENT MEMORY
-- ─────────────────────────────────────────────
CREATE TYPE memory_scope AS ENUM ('conversation', 'org', 'user');

CREATE TABLE agent_memory (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    scope           memory_scope NOT NULL DEFAULT 'org',
    scope_id        UUID,                          -- conversation_id or user_id if scoped
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(org_id, scope, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'::UUID), key)
);

CREATE INDEX idx_agent_memory_org ON agent_memory(org_id, scope);
```

---

### Alerts & Notifications Tables

```sql
-- ─────────────────────────────────────────────
-- ALERT RULES
-- ─────────────────────────────────────────────
CREATE TABLE alert_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by      UUID REFERENCES users(id),
    name            TEXT NOT NULL,
    description     TEXT,
    metric          TEXT NOT NULL,                 -- risk_score | entity_count | recommendation_count | custom_signal
    operator        TEXT NOT NULL,                 -- > | < | >= | <= | = | !=
    threshold       NUMERIC NOT NULL,
    entity_filter   JSONB DEFAULT '{}',            -- { "segment": "enterprise", "risk_tier": "High" }
    channel_ids     UUID[] DEFAULT '{}',           -- references to notification_channels
    is_active       BOOLEAN DEFAULT TRUE,
    cooldown_minutes INTEGER DEFAULT 60,           -- min time between same-rule alerts
    last_triggered_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_alert_rules_org_id ON alert_rules(org_id);

-- ─────────────────────────────────────────────
-- NOTIFICATION CHANNELS
-- ─────────────────────────────────────────────
CREATE TYPE channel_type AS ENUM ('in_app', 'email', 'webhook', 'slack');

CREATE TABLE notification_channels (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        channel_type NOT NULL,
    config      TEXT NOT NULL,                     -- Fernet-encrypted JSON (webhook URL, Slack token, etc.)
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- ALERT EVENTS (triggered alerts)
-- ─────────────────────────────────────────────
CREATE TABLE alert_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    rule_id         UUID NOT NULL REFERENCES alert_rules(id),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    metric          TEXT NOT NULL,
    metric_value    NUMERIC NOT NULL,
    threshold       NUMERIC NOT NULL,
    entity_ids      TEXT[],
    entity_count    INTEGER,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_alert_events_org_id ON alert_events(org_id, created_at DESC);

-- ─────────────────────────────────────────────
-- IN-APP NOTIFICATIONS
-- ─────────────────────────────────────────────
CREATE TABLE notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id),         -- NULL = all org users
    title       TEXT NOT NULL,
    body        TEXT,
    type        TEXT DEFAULT 'info',               -- info | warning | critical | success
    action_url  TEXT,
    source      TEXT,                              -- alert_event | pipeline | system
    source_id   UUID,
    read_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_notifications_user ON notifications(user_id, read_at, created_at DESC);

-- ─────────────────────────────────────────────
-- WEBHOOK DELIVERIES
-- ─────────────────────────────────────────────
CREATE TABLE webhook_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    channel_id      UUID NOT NULL REFERENCES notification_channels(id),
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT DEFAULT 'pending',        -- pending | delivered | failed
    attempts        INTEGER DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    response_status INTEGER,
    response_body   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

### Self-Hosted / Licensing Tables

```sql
-- ─────────────────────────────────────────────
-- LICENSE KEYS (self-hosted only)
-- ─────────────────────────────────────────────
CREATE TABLE license_keys (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE UNIQUE,
    license_key         TEXT NOT NULL UNIQUE,
    plan                TEXT NOT NULL DEFAULT 'free',  -- free | pro | enterprise
    features            TEXT[] DEFAULT '{}',
    -- ['advanced_analytics', 'unlimited_connections', 'webhooks', 'slack', 'api_keys', 'audit_log', 'sso']
    seat_limit          INTEGER,                    -- NULL = unlimited
    expires_at          TIMESTAMPTZ,
    last_validated_at   TIMESTAMPTZ,
    validation_cached_until TIMESTAMPTZ,           -- offline grace period
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- LLM KEY STORE (self-hosted: per-org API keys)
-- ─────────────────────────────────────────────
CREATE TABLE llm_key_store (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE UNIQUE,
    keys        TEXT NOT NULL,                     -- Fernet-encrypted JSON: { anthropic: "sk-...", groq: "gsk_..." }
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

### Audit & Usage Tables

```sql
-- ─────────────────────────────────────────────
-- AUDIT LOG
-- ─────────────────────────────────────────────
CREATE TABLE audit_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id),
    action      TEXT NOT NULL,
    -- e.g. user.login | connection.created | pipeline.triggered | recommendation.actioned
    resource    TEXT,                              -- organizations | connections | recommendations | etc.
    resource_id UUID,
    metadata    JSONB DEFAULT '{}',               -- old/new values, IP address, etc.
    ip_address  INET,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_logs_org_id ON audit_logs(org_id, created_at DESC);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id, created_at DESC);

-- ─────────────────────────────────────────────
-- USAGE EVENTS (for metering / billing)
-- ─────────────────────────────────────────────
CREATE TABLE usage_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    -- pipeline_run | agent_message | entity_profiled | recommendation_generated
    quantity    INTEGER DEFAULT 1,
    metadata    JSONB DEFAULT '{}',
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_usage_events_org ON usage_events(org_id, event_type, recorded_at DESC);
```

---

## API Contract Summary

All endpoints are prefixed `/api/v1`. All require `Authorization: Bearer <token>` or `X-API-Key: <key>` except auth endpoints.

```
Auth
  POST   /auth/signup
  POST   /auth/login
  POST   /auth/refresh
  POST   /auth/logout
  POST   /auth/forgot-password
  POST   /auth/reset-password
  GET    /auth/verify-email?token=
  GET    /auth/oauth/google
  GET    /auth/oauth/google/callback
  GET    /auth/accept-invite?token=

Organizations
  GET    /organization
  PUT    /organization
  DELETE /organization

Users
  GET    /users
  GET    /users/me
  PUT    /users/me
  PUT    /users/:id/role
  DELETE /users/:id
  POST   /users/invite

Connections
  GET    /connections
  POST   /connections
  GET    /connections/:id
  PUT    /connections/:id
  DELETE /connections/:id
  POST   /connections/:id/test
  GET    /connections/:id/tables
  GET    /connections/:id/tables/:table/preview
  POST   /connections/upload              (CSV/Excel multipart)

Schema Mappings
  GET    /schema-mappings
  POST   /schema-mappings
  GET    /schema-mappings/:id
  PUT    /schema-mappings/:id
  DELETE /schema-mappings/:id

Pipeline
  POST   /pipeline/trigger
  GET    /pipeline/runs
  GET    /pipeline/runs/:id
  GET    /pipeline/runs/:id/stream        (SSE)
  GET    /pipeline/schedule
  PUT    /pipeline/schedule

Entities
  GET    /entities                        (?page&limit&risk_tier&segment&search)
  GET    /entities/:id
  GET    /entities/:id/risk-history

Recommendations
  GET    /recommendations                 (?status&urgency&entity_id&page&limit)
  GET    /recommendations/:id
  POST   /recommendations/:id/action
  POST   /recommendations/:id/dismiss
  POST   /recommendations/:id/escalate

Agent
  GET    /agent/conversations
  POST   /agent/conversations
  GET    /agent/conversations/:id
  DELETE /agent/conversations/:id
  POST   /agent/conversations/:id/messages   (SSE response)
  GET    /agent/memory
  DELETE /agent/memory/:id

Analytics
  GET    /analytics/overview?period=
  GET    /analytics/risk-trend?period=&granularity=
  GET    /analytics/segments
  GET    /analytics/cohorts
  GET    /analytics/pipeline-performance
  POST   /analytics/export
  GET    /analytics/exports/:id

Alerts
  GET    /alerts/rules
  POST   /alerts/rules
  PUT    /alerts/rules/:id
  DELETE /alerts/rules/:id
  GET    /alerts/channels
  POST   /alerts/channels
  PUT    /alerts/channels/:id
  DELETE /alerts/channels/:id
  POST   /alerts/channels/:id/test
  GET    /alerts/events

Notifications
  GET    /notifications
  POST   /notifications/:id/read
  POST   /notifications/read-all

Webhooks
  GET    /webhooks/deliveries

API Keys
  GET    /api-keys
  POST   /api-keys
  DELETE /api-keys/:id

License (self-hosted)
  GET    /license
  POST   /license/activate
  POST   /license/refresh

Settings
  GET    /settings/llm-keys
  PUT    /settings/llm-keys

Audit
  GET    /audit-logs

System
  GET    /health
  GET    /readiness
```

---

## Implementation Priority Order

1. **M0** — DX setup (Makefile, API client, Zustand, `react-query`, toast library)
2. **M1** — Full auth flow (email verify, password reset, JWT rotation, OAuth wiring in frontend)
3. **M2** — Expanded connectors (cloud warehouses + Sheets + S3 + file upload) + wire connections UI
4. **M3** — Onboarding wizard fully wired with live preview
5. **M4** — Pipeline SSE streaming + entity profile cache + hardening
6. **M5** — Streaming agent chat + conversation history
7. **M7** — Alerts & in-app notifications (this unblocks user retention before analytics is ready)
8. **M8** — Team & RBAC (invitations, audit log)
9. **M6** — Full analytics with real data
10. **M9** — Self-hosted mode (license key, LLM key store, feature flags, Docker Hub release)
11. **M10** — External API keys + webhook delivery
12. **M11** — Production hardening, rate limiting, Sentry, performance pass

# Pulse — Complete Database Schema

Every table the application will ever need. Tables marked **[ALTER]** exist in migrations but need columns added. Tables marked **[NEW]** need a fresh migration. Ephemeral short-lived tokens (email verify, password reset, refresh tokens) live in **Redis** — not the database.

---

## Summary

### PostgreSQL tables (22)

| # | Table | Status | Notes |
|---|---|---|---|
| 1 | organizations | **[ALTER]** | Missing slug, plan, deployment_mode, timezone, logo_url, updated_at |
| 2 | users | **[ALTER]** | Missing full_name, is_active, is_verified, auth_provider, auth_provider_id, last_login_at, updated_at |
| 3 | connections | **[ALTER]** | Needs name, connector_type, credentials JSONB, config JSONB, metadata JSONB, deleted_at, updated_at. Individual host/port/username columns get folded into credentials |
| 4 | schema_mappings | **[ALTER]** | Missing name, entity_type, segment_column, is_active, goal, updated_at |
| 5 | recommendations | **[ALTER]** | Missing pipeline_run_id, confidence_score, expected_impact, expires_at, outcome_notes, updated_at |
| 6 | agent_conversations | **[ALTER]** | Missing title, message_count, last_message_at, deleted_at |
| 7 | pipeline_runs | **[ALTER]** | Add mapping_id, triggered_by FKs |
| 8 | agent_memory | **[ALTER]** | Extend with scope system |
| 9 | invitations | **[NEW]** | |
| 10 | api_keys | **[NEW]** | |
| 11 | pipeline_schedules | **[NEW]** | |
| 12 | entity_profiles | **[NEW]** | Agent output cache — written by pipeline, read by API |
| 13 | entity_risk_history | **[NEW]** | Time-series risk scores, powers trend charts |
| 14 | alert_rules | **[NEW]** | |
| 15 | notification_channels | **[NEW]** | |
| 16 | alert_events | **[NEW]** | |
| 17 | notifications | **[NEW]** | In-app inbox |
| 18 | webhook_deliveries | **[NEW]** | |
| 19 | license_keys | **[NEW]** | Self-hosted only |
| 20 | llm_key_store | **[NEW]** | Self-hosted only |
| 21 | audit_logs | **[NEW]** | Pro feature, immutable |
| 22 | usage_events | **[NEW]** | Metering |

### Redis keys (not in Postgres)

| Key pattern | TTL | Purpose |
|---|---|---|
| `email_verify:{token}` | 24 h | Email verification token → `user_id` |
| `pw_reset:{token}` | 30 min | Password reset token → `user_id` |
| `refresh:{token_hash}` | 7 days | Refresh token → `{ user_id, org_id, role }` |
| `session:{user_id}` | 24 h | Optional: cached user session data |
| `pipeline:lock:{org_id}` | 10 min | Distributed lock — prevents concurrent pipeline runs |
| `pipeline:progress:{run_id}` | 1 h | SSE progress events for a run |
| `auth_rl:ip:{ip}:{action}` | 60s (varies) | Auth endpoint per-IP counters (login, signup, etc.) |
| `auth_rl:email:{email}:{action}` | 60s–1h | Auth per-email counters (signup, forgot-password) |
| `invite_rl:org:{org_id}` | 1h | Invitation emails per org |
| `invite_rl:inv:{invitation_id}` | 60s | Resend-invitation cooldown |
| `rate_limit:{ip}:{endpoint}` | 1 min | Legacy/generic rate limit key (reserved) |
| `org:context:{org_id}` | 5 min | Cached org + schema mapping for agents |

---

## Migration Strategy

```
Migration 4: alter_existing_tables    → adds missing columns to tables 1–8
Migration 5: create_new_tables        → creates tables 9–22
Migration 6: drop_deprecated_connection_columns  → after running data migration script
```

Do not drop the old connection columns (host, port, username, encrypted_dsn) in migration 4. Run `scripts/db/migrate_connection_credentials.py` first to fold them into `credentials` JSONB, then drop in migration 6.

---

## 1. organizations [ALTER]

```sql
CREATE TABLE organizations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing
    name             TEXT NOT NULL,
    industry         TEXT,
    business_context TEXT,
    entity_label     TEXT,
    goal_label       TEXT,
    onboarding_done  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    slug             TEXT UNIQUE,                    -- e.g. "acme-corp", generated on create
    plan             TEXT NOT NULL DEFAULT 'free',   -- free | pro | enterprise
    deployment_mode  TEXT NOT NULL DEFAULT 'cloud',  -- cloud | self_hosted
    timezone         TEXT NOT NULL DEFAULT 'UTC',
    logo_url         TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_organizations_slug ON organizations(slug) WHERE slug IS NOT NULL;
```

---

## 2. users [ALTER]

```sql
CREATE TABLE users (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing
    org_id           UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email            TEXT NOT NULL UNIQUE,
    password_hash    TEXT,                           -- NULL for OAuth-only accounts
    role             TEXT NOT NULL DEFAULT 'analyst',
    -- admin | manager | analyst | viewer
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    full_name        TEXT NOT NULL DEFAULT '',
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    is_verified      BOOLEAN NOT NULL DEFAULT FALSE,
    auth_provider    TEXT NOT NULL DEFAULT 'email',  -- email | google | github
    auth_provider_id TEXT,                           -- OAuth subject ID
    last_login_at    TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_org_id ON users(org_id);
CREATE INDEX idx_users_email  ON users(email);
CREATE INDEX idx_users_oauth  ON users(auth_provider, auth_provider_id)
    WHERE auth_provider_id IS NOT NULL;
```

---

## 3. connections [ALTER]

The current table has individual columns (host, port, database_name, username, encrypted_dsn). New design folds all credentials into one encrypted JSONB blob so every connector type fits the same row.

```sql
CREATE TABLE connections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing (kept during transition — drop in migration 6)
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    db_type         TEXT,
    host            TEXT,
    port            INTEGER,
    database_name   TEXT,
    username        TEXT,
    encrypted_dsn   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    last_tested_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    name            TEXT NOT NULL DEFAULT 'My Connection',
    connector_type  TEXT NOT NULL DEFAULT 'postgres',
    -- postgres | mysql | mssql | sqlite
    -- redshift | snowflake | clickhouse | bigquery
    -- google_sheets | s3 | gcs | csv | excel | airtable | mongodb

    credentials     TEXT,
    -- Fernet-encrypted JSON, shape varies by connector:
    -- SQL dbs:      { host, port, database, username, password, ssl }
    -- Snowflake:    { account, user, password, warehouse, database, schema, role }
    -- BigQuery:     { project_id, service_account_json }
    -- Google Sheets:{ oauth_token, refresh_token, spreadsheet_id }
    -- S3:           { aws_access_key_id, aws_secret_access_key, region, bucket, prefix }
    -- GCS:          { service_account_json, bucket, prefix }
    -- CSV/Excel:    { file_path, storage_key }

    config          JSONB NOT NULL DEFAULT '{}',
    -- Non-sensitive display config: { schema, warehouse, ssl_mode }

    metadata        JSONB NOT NULL DEFAULT '{}',
    -- Populated after successful test: { table_count, row_estimate, last_synced_at }

    last_test_error TEXT,
    deleted_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_connections_org_id ON connections(org_id) WHERE deleted_at IS NULL;
```

---

## 4. schema_mappings [ALTER]

```sql
CREATE TABLE schema_mappings (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing
    org_id           UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    connection_id    UUID NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    entity_table     TEXT,
    entity_id_col    TEXT,
    entity_name_col  TEXT,
    signal_columns   JSONB,
    -- [{ column, label, type, direction: "higher_is_worse"|"lower_is_worse" }]
    timestamp_col    TEXT,
    risk_config      JSONB,
    -- { high: 0.8, medium: 0.5, signals: { churn_score: { high: 0.8 } } }
    raw_schema       JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    name             TEXT NOT NULL DEFAULT 'Default',
    entity_type      TEXT NOT NULL DEFAULT 'customer',
    -- customer | user | product | asset | route | employee | transaction
    segment_column   TEXT,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    goal             TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_schema_mappings_org_id        ON schema_mappings(org_id);
CREATE INDEX idx_schema_mappings_connection_id ON schema_mappings(connection_id);
```

---

## 5. recommendations [ALTER]

```sql
CREATE TABLE recommendations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing
    org_id           UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_id        TEXT,
    entity_label     TEXT,
    type             TEXT,
    urgency          TEXT,           -- critical | high | medium | low
    title            TEXT,
    reasoning        TEXT,
    suggested_action TEXT,
    status           TEXT NOT NULL DEFAULT 'open',
    -- open | actioned | dismissed | escalated | expired
    actioned_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    actioned_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    pipeline_run_id  UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    confidence_score NUMERIC(4,3),   -- 0.000 to 1.000
    expected_impact  TEXT,
    expires_at       TIMESTAMPTZ,
    outcome_notes    TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_recommendations_org_id  ON recommendations(org_id);
CREATE INDEX idx_recommendations_entity  ON recommendations(org_id, entity_id);
CREATE INDEX idx_recommendations_status  ON recommendations(org_id, status);
CREATE INDEX idx_recommendations_urgency ON recommendations(org_id, urgency, status)
    WHERE status = 'open';
CREATE INDEX idx_recommendations_run     ON recommendations(pipeline_run_id);
```

---

## 6. agent_conversations [ALTER]

```sql
CREATE TABLE agent_conversations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing
    org_id           UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          UUID REFERENCES users(id) ON DELETE SET NULL,
    messages         JSONB NOT NULL DEFAULT '[]',
    -- [{ role: "user"|"assistant"|"tool", content, tool_calls, ts }]
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    title            TEXT,           -- auto-set from first user message (truncated to 80 chars)
    message_count    INTEGER NOT NULL DEFAULT 0,
    last_message_at  TIMESTAMPTZ,
    deleted_at       TIMESTAMPTZ
);

CREATE INDEX idx_conversations_org_user ON agent_conversations(org_id, user_id)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_conversations_updated  ON agent_conversations(org_id, updated_at DESC)
    WHERE deleted_at IS NULL;
```

---

## 7. pipeline_runs [ALTER]

```sql
CREATE TABLE pipeline_runs (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing (all good)
    org_id                    UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    status                    TEXT NOT NULL DEFAULT 'queued',
    -- queued | running | succeeded | failed | skipped | cancelled
    trigger_source            TEXT NOT NULL DEFAULT 'manual',
    -- scheduled | manual | onboarding | api
    current_step              TEXT,
    error                     TEXT,
    started_at                TIMESTAMPTZ,
    completed_at              TIMESTAMPTZ,
    duration_ms               INTEGER,
    entities_scored           INTEGER DEFAULT 0,
    critical_count            INTEGER DEFAULT 0,
    high_count                INTEGER DEFAULT 0,
    recommendations_generated INTEGER DEFAULT 0,
    total_llm_calls           INTEGER DEFAULT 0,
    total_tool_calls          INTEGER DEFAULT 0,
    total_tokens              INTEGER DEFAULT 0,
    provider_fallbacks        INTEGER DEFAULT 0,
    step_metrics              JSONB,
    generation_caps           JSONB,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    mapping_id                UUID REFERENCES schema_mappings(id) ON DELETE SET NULL,
    triggered_by              UUID REFERENCES users(id) ON DELETE SET NULL
    -- NULL when trigger_source = 'scheduled'
);

CREATE INDEX idx_pipeline_runs_org_id     ON pipeline_runs(org_id);
CREATE INDEX idx_pipeline_runs_status     ON pipeline_runs(status);
CREATE INDEX idx_pipeline_runs_created_at ON pipeline_runs(org_id, created_at DESC);
```

---

## 8. agent_memory [ALTER]

Current structure is a per-agent cache keyed by `(org_id, agent_name)`. Extend with a scope system for conversation-level and user-level memory.

```sql
CREATE TABLE agent_memory (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- existing
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_name  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    data        JSONB,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ADD
    scope       TEXT NOT NULL DEFAULT 'org',
    -- org | conversation | user
    scope_id    UUID,
    -- conversation scope → agent_conversations.id
    -- user scope         → users.id
    -- org scope          → NULL
    key         TEXT,           -- named memory slot for org-level facts
    expires_at  TIMESTAMPTZ
    -- drop the old UNIQUE(org_id, agent_name) constraint and replace with index below
);

CREATE INDEX idx_agent_memory_org      ON agent_memory(org_id, scope);
CREATE INDEX idx_agent_memory_scope_id ON agent_memory(scope_id) WHERE scope_id IS NOT NULL;
CREATE INDEX idx_agent_memory_expires  ON agent_memory(expires_at) WHERE expires_at IS NOT NULL;
```

---

## 9. invitations [NEW]

```sql
CREATE TABLE invitations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    invited_by   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'analyst',
    token        TEXT NOT NULL UNIQUE,   -- random 64-char hex, stored in plain (not sensitive once accepted)
    expires_at   TIMESTAMPTZ NOT NULL,   -- NOW() + 72 hours
    accepted_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_invitations_org_id ON invitations(org_id);
CREATE INDEX idx_invitations_token  ON invitations(token);
CREATE INDEX idx_invitations_email  ON invitations(org_id, email)
    WHERE accepted_at IS NULL;
```

---

## 10. api_keys [NEW]

```sql
CREATE TABLE api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,          -- first 8 chars shown in UI e.g. "pk_live_a"
    key_hash     TEXT NOT NULL UNIQUE,   -- SHA-256 of the full key
    scope        TEXT NOT NULL DEFAULT 'read',  -- read | write
    last_used_at TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,            -- NULL = never expires
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_api_keys_org_id   ON api_keys(org_id) WHERE revoked_at IS NULL;
CREATE INDEX idx_api_keys_key_hash ON api_keys(key_hash);
```

---

## 11. pipeline_schedules [NEW]

```sql
CREATE TABLE pipeline_schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    mapping_id      UUID REFERENCES schema_mappings(id) ON DELETE CASCADE,
    cron_expression TEXT NOT NULL DEFAULT '0 */6 * * *',  -- every 6 hours
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at     TIMESTAMPTZ,
    last_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pipeline_schedules_org_id   ON pipeline_schedules(org_id);
CREATE INDEX idx_pipeline_schedules_next_run ON pipeline_schedules(next_run_at)
    WHERE is_active = TRUE;
```

---

## 12. entity_profiles [NEW]

Cached output of the Profiling + Risk Scoring agents. Written by the pipeline, read by all entity API endpoints — your routes never call AI directly.

```sql
CREATE TABLE entity_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    pipeline_run_id UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    mapping_id      UUID REFERENCES schema_mappings(id) ON DELETE CASCADE,

    entity_id       TEXT NOT NULL,   -- value of entity_id_col from the client DB
    entity_name     TEXT,            -- value of entity_name_col
    segment         TEXT,            -- value of segment_column

    profile_data    JSONB NOT NULL DEFAULT '{}',
    -- Raw aggregated signals: { avg_order_value: 142.5, last_active_days: 12, ... }

    risk_score      NUMERIC(4,3),    -- 0.000 to 1.000
    risk_tier       TEXT,            -- High | Medium | Low | Healthy
    risk_narrative  TEXT,            -- LLM-generated 1-2 sentence explanation

    is_latest       BOOLEAN NOT NULL DEFAULT TRUE,
    -- TRUE only for the most recent profile per entity_id.
    -- Set to FALSE when a new pipeline run writes a fresh profile for the same entity.

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entity_profiles_latest
    ON entity_profiles(org_id, entity_id)
    WHERE is_latest = TRUE;

CREATE INDEX idx_entity_profiles_risk
    ON entity_profiles(org_id, risk_tier)
    WHERE is_latest = TRUE;

CREATE INDEX idx_entity_profiles_segment
    ON entity_profiles(org_id, segment)
    WHERE is_latest = TRUE;
```

---

## 13. entity_risk_history [NEW]

One row per entity per pipeline run. Powers all trend charts — pure DB reads, no AI involved.

```sql
CREATE TABLE entity_risk_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    pipeline_run_id UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    entity_id       TEXT NOT NULL,
    risk_score      NUMERIC(4,3) NOT NULL,
    risk_tier       TEXT NOT NULL,           -- High | Medium | Low | Healthy
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_risk_history_entity
    ON entity_risk_history(org_id, entity_id, recorded_at DESC);

CREATE INDEX idx_risk_history_org_date
    ON entity_risk_history(org_id, recorded_at DESC);
```

---

## 14. alert_rules [NEW]

```sql
CREATE TABLE alert_rules (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by        UUID REFERENCES users(id) ON DELETE SET NULL,

    name              TEXT NOT NULL,
    description       TEXT,
    metric            TEXT NOT NULL,
    -- risk_score | entity_count_high | entity_count_critical
    -- recommendation_count | custom_signal:<column_name>
    operator          TEXT NOT NULL,           -- > | < | >= | <= | = | !=
    threshold         NUMERIC NOT NULL,
    entity_filter     JSONB NOT NULL DEFAULT '{}',
    -- { segment: "enterprise", risk_tier: "High" }
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    cooldown_minutes  INTEGER NOT NULL DEFAULT 60,
    last_triggered_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_alert_rules_org_id ON alert_rules(org_id) WHERE is_active = TRUE;
```

---

## 15. notification_channels [NEW]

```sql
CREATE TABLE notification_channels (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    -- in_app | email | webhook | slack

    config      TEXT,
    -- Fernet-encrypted JSON, shape by type:
    -- email:   { recipients: ["ops@acme.com"] }
    -- webhook: { url: "https://...", secret: "whsec_..." }
    -- slack:   { webhook_url: "https://hooks.slack.com/..." }
    -- in_app:  {} (no config needed)

    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notification_channels_org_id ON notification_channels(org_id);
```

---

## 16. alert_events [NEW]

One row per rule trigger instance.

```sql
CREATE TABLE alert_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    rule_id             UUID NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    pipeline_run_id     UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,

    metric              TEXT NOT NULL,
    metric_value        NUMERIC NOT NULL,
    threshold           NUMERIC NOT NULL,
    affected_entity_ids TEXT[],              -- entity_id values that crossed threshold
    affected_count      INTEGER NOT NULL DEFAULT 0,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_alert_events_org_id  ON alert_events(org_id, created_at DESC);
CREATE INDEX idx_alert_events_rule_id ON alert_events(rule_id);
```

---

## 17. notifications [NEW]

In-app notification inbox.

```sql
CREATE TABLE notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    -- NULL = visible to all org members

    title       TEXT NOT NULL,
    body        TEXT,
    type        TEXT NOT NULL DEFAULT 'info',  -- info | warning | critical | success
    action_url  TEXT,
    source      TEXT,                          -- alert_event | pipeline | system
    source_id   UUID,
    read_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notifications_user ON notifications(user_id, created_at DESC)
    WHERE read_at IS NULL;
CREATE INDEX idx_notifications_org  ON notifications(org_id, created_at DESC)
    WHERE user_id IS NULL;
```

---

## 18. webhook_deliveries [NEW]

```sql
CREATE TABLE webhook_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    channel_id      UUID NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
    alert_event_id  UUID REFERENCES alert_events(id) ON DELETE SET NULL,

    event_type      TEXT NOT NULL,
    -- pipeline.completed | entity.risk_changed | recommendation.created | alert.triggered

    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending | delivered | failed | skipped

    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    response_status INTEGER,
    response_body   TEXT,
    next_retry_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_webhook_deliveries_org     ON webhook_deliveries(org_id, created_at DESC);
CREATE INDEX idx_webhook_deliveries_retry   ON webhook_deliveries(next_retry_at)
    WHERE status = 'pending' AND next_retry_at IS NOT NULL;
```

---

## 19. license_keys [NEW]

Only meaningful when `DEPLOYMENT_MODE=self_hosted`.

```sql
CREATE TABLE license_keys (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                  UUID NOT NULL UNIQUE REFERENCES organizations(id) ON DELETE CASCADE,
    license_key             TEXT NOT NULL UNIQUE,
    plan                    TEXT NOT NULL DEFAULT 'free',  -- free | pro | enterprise
    features                TEXT[] NOT NULL DEFAULT '{}',
    -- audit_log | sso | white_label | priority_support
    seat_limit              INTEGER,           -- NULL = unlimited
    expires_at              TIMESTAMPTZ,       -- NULL = lifetime
    last_validated_at       TIMESTAMPTZ,
    validation_cached_until TIMESTAMPTZ,       -- offline grace window (7 days)
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## 20. llm_key_store [NEW]

Self-hosted orgs bring their own Anthropic/Groq keys. Stored encrypted per org.

```sql
CREATE TABLE llm_key_store (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL UNIQUE REFERENCES organizations(id) ON DELETE CASCADE,
    keys        TEXT NOT NULL,
    -- Fernet-encrypted JSON: { anthropic: "sk-ant-...", groq: "gsk_..." }
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## 21. audit_logs [NEW]

Pro-only. Written on every sensitive action, never updated or deleted.

```sql
CREATE TABLE audit_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,

    action      TEXT NOT NULL,
    -- Dot-namespaced actions:
    -- user.login | user.logout | user.invite | user.role_changed | user.deleted
    -- connection.created | connection.deleted | connection.tested
    -- pipeline.triggered | pipeline.cancelled
    -- recommendation.actioned | recommendation.dismissed | recommendation.escalated
    -- api_key.created | api_key.revoked
    -- license.activated | settings.updated

    resource    TEXT,            -- table name: users | connections | recommendations | ...
    resource_id UUID,
    metadata    JSONB NOT NULL DEFAULT '{}',
    -- { old_role: "analyst", new_role: "admin" }
    ip_address  INET,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- intentionally no updated_at — immutable
);

CREATE INDEX idx_audit_logs_org_id   ON audit_logs(org_id, created_at DESC);
CREATE INDEX idx_audit_logs_user_id  ON audit_logs(user_id, created_at DESC);
CREATE INDEX idx_audit_logs_action   ON audit_logs(org_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_resource ON audit_logs(org_id, resource, resource_id);
```

---

## 22. usage_events [NEW]

Lightweight metering — one row per billable or trackable action.

```sql
CREATE TABLE usage_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,
    -- pipeline_run | agent_message | entity_profiled
    -- recommendation_generated | connection_tested | export_generated
    quantity     INTEGER NOT NULL DEFAULT 1,
    metadata     JSONB NOT NULL DEFAULT '{}',
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_usage_events_org     ON usage_events(org_id, event_type, recorded_at DESC);
CREATE INDEX idx_usage_events_monthly ON usage_events(org_id, recorded_at DESC);
```

---

## Redis Key Reference

All ephemeral tokens and short-lived data go in Redis with native TTL — no cleanup jobs needed.

```
# Auth tokens (value = user_id as string)
email_verify:{token}          TTL: 86400s  (24h)
pw_reset:{token}              TTL: 1800s   (30 min)
refresh:{SHA256(raw_token)}   TTL: 604800s (7 days)  value: JSON { user_id, org_id, role }

# Session cache (optional, avoids DB hit on every request)
session:{user_id}             TTL: 86400s  value: JSON { org_id, role, is_verified, plan }

# Pipeline
pipeline:lock:{org_id}        TTL: 600s    (10 min) — distributed mutex, set with NX
pipeline:progress:{run_id}    TTL: 3600s   (1h)     — list of SSE event JSON strings

# Rate limiting (sliding window, using Redis INCR + EXPIRE)
rate_limit:{ip}:{endpoint}    TTL: 60s     value: request count

# Org context cache (avoid repeated DB reads in agent hot path)
org:context:{org_id}          TTL: 300s    (5 min)  value: JSON { org, schema_mapping, connection_config }
```

**Implementation note:** Use `app/infrastructure/redis/client.py` as the single place that initialises the Redis connection pool. Every service imports from there — no ad-hoc `redis.from_url()` calls scattered around.

---

## Alembic Migration Plan

### Migration 4 — `alter_existing_tables`
```
alembic revision --autogenerate -m "alter_existing_tables"
```
Adds all missing columns to tables 1–8. Does **not** drop old connection columns.

### Migration 5 — `create_new_tables`
```
alembic revision --autogenerate -m "create_new_tables"
```
Creates tables 9–22.

### Migration 6 — `drop_deprecated_connection_columns`
```
alembic revision -m "drop_deprecated_connection_columns"
```
Run `scripts/db/migrate_connection_credentials.py` first (folds host/port/username/encrypted_dsn into the `credentials` JSONB blob), then this migration drops the now-empty old columns.

---

## Model File Checklist

```
app/infrastructure/database/models/
  organization.py          [ALTER]
  user.py                  [ALTER]
  connection.py            [ALTER]
  schema_mapping.py        [ALTER]
  recommendation.py        [ALTER]
  agent_conversation.py    [ALTER]
  pipeline_run.py          [ALTER]
  agent_memory.py          [ALTER]
  invitation.py            [NEW]
  api_key.py               [NEW]
  pipeline_schedule.py     [NEW]
  entity_profile.py        [NEW]
  entity_risk_history.py   [NEW]
  alert_rule.py            [NEW]
  notification_channel.py  [NEW]
  alert_event.py           [NEW]
  notification.py          [NEW]
  webhook_delivery.py      [NEW]
  license_key.py           [NEW]
  llm_key_store.py         [NEW]
  audit_log.py             [NEW]
  usage_event.py           [NEW]
  __init__.py              re-export all (required for Alembic autogenerate)

app/infrastructure/redis/
  client.py                Redis connection pool singleton
  keys.py                  Key builder functions e.g. keys.email_verify(token)
  tokens.py                set/get/delete helpers for auth tokens
```

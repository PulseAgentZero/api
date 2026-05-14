# Pulse — Product Requirements Document (PRD)

**Tagline:** Real-Time Intelligence for Any Business  

| Field | Value |
|--------|--------|
| **Version** | 1.0 |
| **Context** | DSN × BCT LLM Agent Challenge Hackathon 3.0 |
| **Date** | May 2025 |
| **Presentation** | Grand Finale — 10 June 2025 |
| **Submitted for** | Data Science Nigeria × Bluechip Technologies |

---

## Table of contents

1. [Executive summary](#1-executive-summary)  
2. [Problem statement](#2-problem-statement)  
3. [Product overview](#3-product-overview)  
4. [How Pulse works](#4-how-pulse-works)  
5. [Product features](#5-product-features)  
6. [Technical architecture](#6-technical-architecture)  
7. [Monetization & pricing](#7-monetization--pricing-model)  
8. [User stories](#8-user-stories)  
9. [Team & execution plan](#9-team--execution-plan)  
10. [Go-to-market strategy](#10-go-to-market-strategy)  
11. [Success metrics](#11-success-metrics)  
12. [Risks & mitigations](#12-risks--mitigations)  
[Appendix: Glossary](#appendix-glossary)

---

## 1. Executive summary

**Pulse** is an open-source, self-hostable **user modeling and recommendation intelligence** platform for enterprise operations. It connects to an organization’s existing data (database or file upload), surfaces **behavioral profiles** for every entity, generates **AI-powered recommendations**, and delivers insights through a **conversational LLM agent** and a **real-time dashboard**.

**Core insight:** Many industries—telecom, healthcare, FMCG, retail, logistics, public sector—hold rich operational data but struggle to act on it. Tools are often too expensive, too rigid, require a data science team, or assume one industry.

**Pulse’s answer:** Be **industry-agnostic**. No hardcoded business logic. The org describes context in plain English, connects data, and Pulse builds a **living intelligence layer**—modeling behavior, generating recommendations, and answering operational questions in real time.

> **Hackathon context:** Built for the **DSN × Bluechip Technologies LLM Agent Challenge Hackathon 3.0** under the theme **User Modeling and Recommendation Systems**.

---

## 2. Problem statement

### 2.1 The core problem

Enterprises generate large volumes of operational data (transactions, usage, inventory, admissions, subscriber behavior). That data *should* support churn reduction, stockout prevention, better allocation, and personalization—but the gap between **having data** and **acting on it** is wide:

- **BI tools** (e.g. Power BI, Tableau) visualize but don’t reason, recommend, or answer follow-up questions.  
- **Custom ML** needs a data science team many orgs don’t have.  
- **Enterprise AI** is often costly, rigid, and industry-locked.  
- **Operational teams** can’t self-serve without SQL.  
- **Insights are late**—weekly reports and manual flows keep decisions reactive.

### 2.2 Who this affects

| Industry | Pain point | What Pulse delivers |
|----------|------------|---------------------|
| **Telecom** | Churn visible but hard to act on fast | Retention interventions, churn risk per subscriber |
| **Healthcare** | Bed/staff allocation without live facility intelligence | Patient flow signals, ward alerts, discharge-oriented insights |
| **FMCG / Retail** | Stockouts discovered late | SKU-level replenishment, demand patterns by location |
| **Logistics** | Weak view of routes, delays, drivers | Route/driver profiles, delay risk signals |
| **Public sector** | Little behavioral view of service usage | Demand modeling, regional resource hints |

### 2.3 Why existing solutions fail

The market tends to split into **generic BI** (shows, doesn’t reason) and **custom ML platforms** (powerful but slow and team-heavy). There is little that is **self-serve, deployable, industry-agnostic**, and **immediately** useful on *your* data without hiring a data scientist.

> **Positioning:** Pulse targets that gap.

---

## 3. Product overview

### 3.1 Vision

Be the **open-source intelligence layer** any organization can deploy on top of existing data—turning operational records into **profiles**, **recommendations**, and **conversational intelligence** without requiring a data science team.

### 3.2 Name & brand

| Attribute | Value |
|-----------|--------|
| **Product name** | Pulse |
| **Tagline** | Real-Time Intelligence for Any Business |
| **Category** | Open-source LLM-powered user modeling & recommendation platform |
| **License** | MIT (open core) + commercial cloud tier |
| **Target users** | Ops managers, retention teams, supply chain, hospital admins—teams that need data-backed decisions |
| **Primary markets** | Mid-to-large enterprises in Africa and emerging markets (e.g. Nigeria, Kenya, Ghana, South Africa) |

### 3.3 Deployment models

Inspired by tools like **n8n**, **Metabase**, and **Grafana**:

| Model | Description |
|--------|-------------|
| **Self-hosted (open source)** | Full stack on GitHub (MIT). Clone, deploy with Docker Compose or Kubernetes, connect to **internal** DBs. Data stays in the customer environment. Fits strict governance, NDPR/GDPR-style policies, integrators (e.g. Bluechip) offering managed installs. |
| **Cloud (managed SaaS)** | Sign up, guided onboarding, CSV/Excel or hosted DB. For SMEs, fast trials, teams avoiding self-managed infra. |

> **Demo strategy (hackathon):** Lead with **cloud** end-to-end (signup → live intelligence). Document self-hosted path in the repo and reference in the pitch.

### 3.4 Core principle: industry agnosticism

Pulse does **not** ship industry-specific business rules. At onboarding the org:

1. **Describes the business** in plain English (what they do, who “entities” are, what matters).  
2. **Connects a database** (PostgreSQL, MySQL, MSSQL, SQLite)—introspect schema; **data does not move** into Pulse for warehousing.  
3. **Maps entities**—entity table, ID column, display name, behavioral signal columns.  
4. **States goals**—e.g. reduce churn, prevent stockouts, optimize allocation.

That context drives dashboards and the agent. Responses are backed by **live queries** to the org’s database—not static copies. The same product can model subscribers, patients, or SKUs depending on configuration.

---

## 4. How Pulse works

### 4.1 Three intelligence layers

#### Layer 1 — Live connection & schema intelligence

- Read-only connection to the customer’s DB.  
- Introspect schema (tables, columns, types, relationships); store **metadata and mapping**, not row-level business data.  
- Queries at runtime hit the **live** database (Metabase/Retool-style).  
- Self-hosted: often same network as the DB; no requirement to copy data out.

#### Layer 2 — Query engine & recommendations

- Dashboard and agent requests become **parameterized SQL** using stored mapping.  
- Knows entity table, signal columns, and how risk-style scores are derived from config.  
- Recommendations are produced from **live analytics**; Pulse may store **recommendation records** in its app DB while underlying facts stay in the customer DB.

#### Layer 3 — Conversational agent

- Embedded in the dashboard; plain-English questions.  
- **Tool calling:** data answers come from tools that run SQL via the connection layer—not from invented facts.

### 4.2 Agent tool architecture (conceptual)

Tools map to live SQL (and safe patterns) through the connection manager. Representative tools:

| Tool | Role |
|------|------|
| **get_entity_list** | Filtered/ranked entity list (risk tier, search, limit). |
| **get_entity_profile** | Full mapped row + behavioral metrics for one entity. |
| **get_risk_summary** | Aggregates: tier counts, top at-risk, key averages. |
| **get_recommendations** | Current recommendation queue (from Pulse DB), ranked by urgency. |
| **run_custom_query** | NL → validated, parameterized SQL against allowed schema. |
| **get_trend_data** | Time series for a signal (needs mapped timestamp column). |
| **generate_action_draft** | Draft comms/plan from profile + goals (uses fetched context). |
| **get_aggregate_summary** | High-level operational snapshot from live aggregates. |

*(Additional tools may be extended per org mapping and context.)*

---

## 5. Product features

### 5.1 Onboarding wizard

Guided, low-friction setup (credential-style UX similar to n8n):

| Step | Content |
|------|---------|
| Organization | Name, industry, team size, primary use case |
| Business context | Free text → foundation for agent system prompt |
| Database connection | Type, host, port, DB name, credentials; test connection; **encrypt at rest** |
| Schema introspection | Pick entity table, ID, display name, signal columns, optional timestamp |
| Review | Confirm mapping and connection |
| First dashboard load | First live queries and populated dashboard |

**Supported DB types (conceptual):** PostgreSQL, MySQL, SQLite, Microsoft SQL Server; plus CSV/Excel paths where productized.

### 5.2 Intelligence dashboard

- **Overview:** Entity counts by risk tier, recommendation counts/urgency, KPI cards, anomaly banner.  
- **Entity explorer:** Search/filter; row shows risk, top recommendation, activity signal, profile completeness; drill-down to full profile, history, charts, entity-scoped recommendations.  
- **Recommendations:** Live queue; types, urgency, reasoning, suggested actions; states like actioned / dismissed / escalated; track outcomes after actioned.  
- **Trends & analytics:** Time series, segment comparison, cohort views; export CSV/PNG.

### 5.3 Conversational agent

- Persistent side panel; context-aware (screen, selected entity, alerts).  
- Example intents: top risk drivers, draft retention message, segment decline, weekly exec summary, capacity triage, location comparison.

### 5.4 Role-based access control (RBAC)

| Role | Permissions (summary) |
|------|-------------------------|
| **Organization admin** | Full access: config, users, billing (cloud), settings; all entities. |
| **Operations manager** | Full dashboard + agent + recommendations + export; no system config. |
| **Team member / analyst** | View + agent + mark recommendations; no admin config. |
| **Read-only viewer** | View dashboards/reports; no agent actions or recommendation state changes. |

### 5.5 Alerts & notifications

- In-app alerts for new high-risk entities.  
- Email for critical thresholds (configurable).  
- Daily digest of recommendations and risk shifts.  
- Webhooks (self-hosted) to Slack, Teams, or internal systems.

---

## 6. Technical architecture

### 6.1 Stack (reference)

| Layer | Technology |
|--------|------------|
| **Frontend** | Next.js 14, Tailwind, shadcn/ui, Recharts / ApexCharts |
| **Backend API** | FastAPI, async SQLAlchemy |
| **Pulse app DB** | PostgreSQL — org config, metadata, encrypted credentials, recommendations, users, audit logs (**not** a copy of client row data) |
| **AI** | Anthropic Claude (e.g. Sonnet-class) with tool calling |
| **Client DB** | asyncpg / aiomysql / aiosqlite (+ MSSQL where supported) |
| **Secrets** | Fernet for connection material at rest |
| **Auth** | JWT + refresh; RBAC at API |
| **Deploy** | Docker & Compose for self-hosted |

### 6.2 Multi-tenancy

- Shared application tables with **organization scope** on Pulse’s Postgres (e.g. `org_id` per row).  
- Requests carry tenant context from the authenticated session; queries are always scoped.  
- **Client operational data** remains in the customer’s database; Pulse stores configuration and derived artifacts (e.g. recommendations), not a warehouse of their raw tables.

### 6.3 Connection manager (summary)

1. Collect connection details in onboarding.  
2. Validate with a simple test query (e.g. `SELECT 1`).  
3. Store encrypted credentials; key in environment, not in DB.  
4. On each use: decrypt → connect → run query → return results → close (pooling per org as needed).  
5. Prefer **read-only** DB users (`SELECT` only).  
6. TLS for cloud paths to customer DBs; optional SSH/VPN for extra control.  
7. Avoid persisting raw query result sets as system-of-record copies.

**Illustrative `connections` shape (conceptual):**

```text
id, org_id, db_type, encrypted_dsn, host, port, database_name,
status, last_tested_at, created_at
```

### 6.4 Query engine (patterns)

Examples of generated patterns (always parameterized):

- Entity list: ordered by risk signal, limited rows.  
- Entity profile: keyed lookup on entity ID.  
- Risk distribution: aggregates by tier.  
- Trends: `date_trunc`-style grouping on configured date column.  
- Search: ILIKE / text match on allowed columns.

Validation against stored schema metadata reduces injection and “wrong column” risk.

### 6.5 LLM agent

- Dynamic system prompt: business context + schema mapping summary + lightweight live state snippet.  
- Instructions for tone, format, and escalation.  
- All quantitative answers grounded in tool results.

### 6.6 Self-hosted quick start (illustrative)

```bash
git clone https://github.com/pulse-platform/pulse
cp .env.example .env   # add ANTHROPIC_API_KEY, ENCRYPTION_KEY, DATABASE_URL, etc.
docker compose up -d
```

Outbound LLM calls send **prompt + metadata**, not bulk raw exports; air-gapped orgs may use OpenAI-compatible local endpoints.

---

## 7. Monetization & pricing model

### 7.1 Free (open source) — always

- Full source (MIT), Docker self-host, unlimited orgs/users (self-hosted), dashboard + agent + connectors + API, community support (Issues/Discussions).

> **Why free?** Distribution and trust; enterprises evaluate on-prem for free. Revenue from **cloud** and **services**.

### 7.2 Cloud tiers (illustrative)

| Tier | Price (indicative) | Highlights |
|------|-------------------|------------|
| **Starter** | Free | 1 org, ≤3 users, ≤10k entities, upload-only, capped agent queries, branding |
| **Growth** | ~$49/mo | More users/entities, DB connect, more queries, email support, remove branding |
| **Business** | ~$149/mo | Multi-org, webhooks, priority support, custom domain |
| **Enterprise** | Custom | SLA, dedicated infra, white-label, integrations |

### 7.3 Other revenue

- **Managed self-hosted** — customer infra, Pulse/partner ops ($500–$2k/mo illustrative).  
- **Professional services** — connectors, onboarding, custom dashboards, ERP/CRM integration (strong SI channel, e.g. Bluechip).  
- **Partner / reseller** — margin + rev-share on deployments.  
- **LLM costs** — bundled in cloud tiers within limits; self-hosted BYO API key.

> **Principle:** Self-hosted core is not paywalled; paid tiers sell convenience, scale, and support.

---

## 8. User stories

### 8.1 Organization administrator

- Onboard in **under ~20 minutes** without a heavy IT project.  
- Connect **direct** DB so data stays in-environment.  
- Manage users and roles.  
- Receive a **daily digest** of operational intelligence.

### 8.2 Operations manager

- See **who is at risk now** without scanning every row.  
- Ask the agent in **plain English** without SQL.  
- Open a **full behavioral profile** before acting.  
- Get **draft actions/communications** for specific entities.

### 8.3 Team member

- See recommendations relevant to their scope.  
- Mark recommendations **actioned** for feedback loops.  
- Use the agent for **fast answers** during busy days.

---

## 9. Team & execution plan

### 9.1 Roles

| Role | Focus |
|------|--------|
| **Frontend** | Next.js UI: onboarding, dashboard, explorer, agent panel, RBAC views, responsive layout |
| **Backend** | FastAPI, Postgres, multi-tenant APIs, ingestion, recommendations, auth, alerts |
| **AI / agent** | Claude tool-calling, tool contracts, dynamic prompts, quality |

### 9.2 Phases (hackathon-style)

| Phase | Window | Goals |
|-------|--------|--------|
| **1 — Foundation** | Days 1–2 | Scaffold, schema, tenants, auth, org CRUD; FE shell + auth routes; base agent + prompt template |
| **2 — Core** | Days 3–5 | Onboarding APIs, ingestion (CSV + PG), profiles, recommendations v1; wizard + dashboard + explorer; core tools wired |
| **3 — Intelligence** | Days 6–7 | Recommendations v2, alerts, jobs (e.g. Celery), perf; charts, recommendation actions, RBAC, polish |
| **4 — Demo** | Day 8 | Two demo orgs (e.g. telecom + hospital), rehearsal, README + Compose, pitch deck |

---

## 10. Go-to-market strategy

### 10.1 Initial beachhead

Mid-to-large enterprises in **Nigeria** across telecom, private hospital groups, FMCG distribution, and retail—aligned with **Bluechip’s** client base for faster SI-led distribution.

### 10.2 Channels

| Channel | Role |
|---------|------|
| **Open source** | GitHub as top-of-funnel; self-host → cloud/services when convenience matters |
| **System integrators** | Managed enterprise rollouts + rev-share |
| **Direct cloud** | Free starter → usage and limits drive paid conversion |

### 10.3 Differentiation (summary)

| vs. | Pulse angle |
|-----|-------------|
| Power BI / Tableau | Reasons & recommends; lower bar to “ask next question” |
| Salesforce / HubSpot | Not a fixed CRM schema—works on *your* operational data model |
| Custom ML | Hours to first value vs. months of build |
| Generic LLM chat | Answers grounded in **live tools + SQL**, not free-form hallucination on facts |

---

## 11. Success metrics

### 11.1 Hackathon demo

- Live onboarding for **two industries**.  
- Agent answers **≥5** live tool-backed questions on real ingested/configured data.  
- Self-hosted path **documented** (repo + Compose).  
- Monetization story is **clear and defensible**.  
- **Industry-agnostic** value is obvious from the demo contrast.

### 11.2 Six months post-hackathon (targets)

- 100+ GitHub stars  
- 10+ production self-hosts  
- 3+ paying cloud customers  
- 1 SI partnership  
- NPS > 40 among active users  

---

## 12. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| LLM latency in demo | Preload context; streaming; cached demo fallbacks |
| Privacy skepticism | Lead with self-hosted + “data doesn’t leave”; NDPR-aware design narrative |
| Scope too large | Ship onboarding → dashboard → agent first; trim analytics if needed |
| “Generic AI” confusion | Two-industry demo back-to-back |
| API cost spikes | Rate limits; capped demo keys |

---

## Appendix: Glossary

| Term | Definition |
|------|------------|
| **Entity** | Primary object modeled (subscriber, patient, SKU, store, etc.)—defined by customer mapping. |
| **Connection manager** | Subsystem that stores encrypted credentials and opens **on-demand** connections to run SQL against the customer DB. |
| **Schema metadata** | Table/column/type information Pulse stores to build safe SQL—not bulk row copies. |
| **Query engine** | Builds validated, parameterized SQL from UI/agent requests against live data. |
| **Recommendation engine** | Analytical layer that surfaces risk-pattern matches as prioritized actions; stores recommendation **records** in Pulse, not customer row warehouses. |
| **Tool calling** | LLM invokes structured tools instead of guessing facts; Pulse uses this to ground answers in live queries. |
| **Multi-tenancy** | One Pulse deployment serves many orgs with strict isolation (e.g. org-scoped rows and JWT-derived tenant context). |
| **Self-hosted** | Customer runs Pulse in their network; DB traffic stays internal; typical outbound is LLM API only unless configured otherwise. |
| **NDPR** | Nigeria Data Protection Regulation; architecture aims at data minimization and residency-friendly deployment options. |

---

*Pulse — Real-Time Intelligence for Any Business*  
*DSN × Bluechip Technologies LLM Agent Challenge Hackathon 3.0 · June 2025*

PULSE
Real-Time Intelligence for Any Business

Product Requirements Document (PRD)
Version 1.0  —  DSN x BCT LLM Agent Challenge Hackathon 3.0
May 2025
Submitted for: Data Science Nigeria × Bluechip Technologies
Grand Finale Presentation — June 10, 2025

1. Executive Summary
Pulse is an open-source, self-hostable user modeling and recommendation intelligence platform designed for enterprise operations. It connects to any organization's existing data — whether through a direct database connection or a simple file upload — and immediately surfaces behavioral profiles for every entity in that data, generates actionable AI-powered recommendations, and delivers everything through a conversational LLM agent and a real-time dashboard.

The core insight behind Pulse is simple: organizations across every industry — telecoms, healthcare, FMCG, retail, logistics, public sector — are sitting on enormous amounts of operational data they cannot fully act on. Not because the data isn't there, but because the tools to extract intelligence from it are either too expensive, too rigid, require a data science team to operate, or are locked into a single industry's assumptions.

Pulse solves this by being industry-agnostic by design. The platform does not hardcode any business logic. Instead, an organization onboards, describes their business context in plain English, connects their data, and Pulse builds a living intelligence layer on top of it — modeling behavior, generating recommendations, and answering operational questions in real time.

HACKATHON CONTEXT:  Pulse is built for the DSN x Bluechip Technologies LLM Agent Challenge Hackathon 3.0 under the theme: User Modeling and Recommendation Systems.




2. Problem Statement
2.1 The Core Problem
Enterprises across Africa generate vast amounts of operational data every day — customer transactions, usage records, inventory movements, patient admissions, subscriber behavior. This data theoretically contains the intelligence needed to reduce churn, prevent stockouts, optimize resource allocation, and personalize experiences.

But the gap between having the data and acting on it intelligently is enormous. The reasons are consistent across industries:

Existing BI tools (Power BI, Tableau) show dashboards but cannot reason, recommend, or answer questions
Building a custom ML pipeline requires a dedicated data science team most organizations cannot afford
Enterprise AI platforms are expensive, rigid, and locked into specific industry assumptions
Operational teams — the people who need to make decisions — cannot query data themselves without SQL knowledge
Insights arrive too late — weekly reports, manual exports, and delayed data pipelines mean decisions are always reactive

2.2 Who This Affects
Industry
Pain Point
What Pulse Delivers
Telecom
Retention managers losing subscribers to churn they can see coming but can't act on fast enough
Personalized retention interventions, churn risk profiles per subscriber
Healthcare
Hospital ops managers allocating beds and staff manually with no real-time facility intelligence
Patient flow recommendations, ward reallocation alerts, discharge predictions
FMCG / Retail
Supply chain managers discovering stockouts days after they happen via WhatsApp messages
SKU-level replenishment recommendations, demand pattern profiles per location
Logistics
Fleet managers with no intelligent view of route performance, delay patterns, or driver behavior
Route optimization recommendations, driver behavior profiles, delay risk alerts
Public Sector
Government agencies managing citizen services with no behavioral intelligence about service usage patterns
Service demand modeling, resource allocation recommendations per region


2.3 Why Existing Solutions Fail
The market currently offers two extremes: generic BI dashboards that show data but cannot reason about it, and custom ML platforms that require months of implementation and a dedicated technical team. There is no intelligent, self-serve, industry-agnostic platform that an organization can deploy themselves, connect to their existing data, and immediately get behavioral intelligence and recommendations from — without writing a single line of code or hiring a data scientist.

POSITIONING:  That is exactly the gap Pulse fills.




3. Product Overview
3.1 Product Vision
To be the open-source intelligence layer that any organization in any industry can deploy on top of their existing data — turning raw operational records into behavioral profiles, recommendations, and conversational intelligence without requiring a data science team.

3.2 Product Name & Brand
Attribute
Value
Product Name
Pulse
Tagline
Real-Time Intelligence for Any Business
Category
Open-Source LLM-Powered User Modeling & Recommendation Platform
License
MIT (open-source core) + Commercial Cloud Tier
Target Users
Operations managers, retention teams, supply chain managers, hospital administrators — any operational team that makes data-driven decisions
Primary Market
Mid-to-large enterprises across Africa and emerging markets, with deployment focus on Nigeria, Kenya, Ghana, and South Africa


3.3 Deployment Models
Pulse operates on a dual deployment model, directly inspired by how successful open-source SaaS platforms like n8n, Metabase, and Grafana operate in the market:

Self-Hosted (Open Source)
The complete Pulse platform is open-source and freely available on GitHub under the MIT license. Any organization can clone the repository, deploy it inside their own infrastructure using Docker Compose or Kubernetes, and connect it directly to their existing internal databases. The organization's data never leaves their environment. This model is designed for:
Large enterprises with strict data governance requirements (banks, hospitals, telcos)
Organizations operating under NDPR, GDPR, or internal IT security policies
Companies that want to audit the codebase before deployment
Bluechip Technologies and similar system integrators who want to deploy Pulse as a managed service for their clients

Cloud Version (Managed SaaS)
Pulse also offers a fully managed cloud version hosted on Pulse's infrastructure. Organizations sign up, go through a guided onboarding wizard, and either upload their data via CSV/Excel or connect a hosted database. This model is designed for:
SMEs and mid-size organizations that do not want to manage their own infrastructure
Teams that need to get started immediately without an IT procurement process
Organizations that want to trial Pulse before committing to a self-hosted deployment

DEMO STRATEGY:  For the hackathon demo, we will showcase the Cloud Version — demonstrating the full end-to-end experience from signup to live intelligence. The self-hosted deployment path will be documented in the GitHub repository and referenced in the pitch.


3.4 Core Principle: Industry Agnosticism
Pulse does not hardcode any industry-specific business logic. Instead, the platform learns the organization's context during onboarding. When a new organization sets up Pulse, they:
Describe their business in plain English — what they do, who their customers or key entities are, and what metrics matter to them
Connect their database — provide a connection string to their existing PostgreSQL, MySQL, MSSQL, or SQLite database. Pulse connects directly and introspects the schema. Their data never moves.
Map their key entities — select which table contains their primary entities, which column is the unique identifier, and which columns are the key behavioral signals
Define their goals — what outcomes they want recommendations for (reduce churn, prevent stockouts, optimize allocation, etc.)

This context becomes the foundation of Pulse's intelligence layer. From this point on, every dashboard panel and every agent response is powered by live queries against the organization's own database — not copies or snapshots. The same platform that models subscriber churn for a telco will model patient flow for a hospital and inventory demand for an FMCG — configured entirely by the organization, not by the Pulse development team.



4. How Pulse Works
4.1 The Three Intelligence Layers
Pulse is built around three interconnected layers that work together to turn live organizational data into actionable intelligence:

Layer 1 — Live Database Connection & Schema Intelligence
When an organization onboards, Pulse connects directly to their existing database using a read-only connection. Pulse introspects the schema — reading table structures, column names, data types, and relationships — and stores only this metadata, never the data itself. From this point on, every query Pulse executes runs against the organization's live database in real time.

This is the same model used by tools like Metabase, Retool, and Tableau — the data never moves. Pulse sits on top of the organization's existing database and reads from it on demand. For the self-hosted version, Pulse is deployed inside the organization's own network, meaning the connection never traverses the public internet at all.

The schema metadata Pulse stores — table names, column names, data types, and the organization's mapping configuration — is what powers the dynamic query generation engine. When a dashboard panel loads or the agent needs to answer a question, Pulse generates the appropriate SQL from this metadata and executes it against the live database.

Layer 2 — Dynamic Query Engine & Recommendation Generation
Pulse's query engine translates dashboard requests and agent tool calls into optimized SQL that runs against the organization's database. The engine is aware of the schema mapping the organization configured during onboarding, so it knows which table is the entity table, which columns are behavioral signals, and how to compute risk scores from the available data.

Recommendations are generated by the query engine on demand — when the dashboard loads or the agent requests them. They are not pre-computed and stored. Instead, Pulse runs analytical queries against the live data to identify entities matching risk patterns (declining activity, threshold breaches, anomalous values) and surfaces them as prioritized recommendations. Only recommendation metadata — the recommendation record itself — is stored in Pulse's own database. The underlying data always lives in the organization's system.

Layer 3 — Conversational Agent Interface
The LLM agent is the human interface to Pulse's intelligence. It is embedded directly in the dashboard and allows any operational team member to query their organization's data in plain English. The agent is built with tool calling — it never answers data-dependent questions from general knowledge. Instead, it calls tools that generate and execute SQL against the organization's live database, then reasons over the returned results to compose its response.

4.2 Agent Tool Architecture
The following core tools are available to the Pulse agent across all industry configurations. Every tool call translates into a live SQL query executed against the organization's connected database via the Connection Manager. Additional tools are generated dynamically based on the organization's schema mapping and business context:

Tool Name
Description
get_entity_list
Queries the entity table for a filtered, ranked list of entities. Accepts risk_tier, search term, and limit as parameters. Returns entity IDs, display names, computed risk scores, and top signal values.
get_entity_profile
Queries all mapped columns for a specific entity from the organization's database. Returns the complete row plus computed behavioral metrics derived from the live data.
get_risk_summary
Runs an aggregate query against the entity table to return risk distribution counts, top at-risk entities, and key metric averages across the organization.
get_recommendations
Returns the current recommendation queue from Pulse's recommendations table for this org, ranked by urgency. Recommendations are generated by the Query Engine on the last dashboard load.
run_custom_query
Executes a natural language request as a structured analytical query against the org's database. The agent translates the request to SQL, validates column references against stored schema metadata, and executes it safely with parameterization.
get_trend_data
Queries time-series data for a specific signal column grouped by date, returning trend direction and period-over-period change. Requires a timestamp column to be configured in the schema mapping.
generate_action_draft
Uses the entity profile data fetched via get_entity_profile plus the organization's goal context to generate a personalized draft communication or action plan. Does not query the database — uses already-fetched profile data.
get_aggregate_summary
Returns a high-level summary of the organization's current operational state — entity counts, signal averages, anomaly flags — generated from live aggregate queries.




5. Product Features
5.1 Onboarding Wizard
The onboarding wizard is the entry point for any new organization. It is a multi-step guided flow — inspired by how n8n handles credential and connection setup — that configures Pulse for that organization's specific context without requiring technical knowledge from the end user.

Organization Setup — company name, industry, team size, primary use case
Business Context — a free-text field where the organization describes their business, their key entities, and their operational goals. This becomes the agent's system prompt foundation.
Database Connection — the organization provides their database connection details: database type (PostgreSQL, MySQL, MSSQL, SQLite), host, port, database name, username, and password. Pulse tests the connection, confirms it is reachable, and stores the credentials encrypted at rest. Supported databases: PostgreSQL, MySQL, SQLite, Microsoft SQL Server.
Schema Introspection — Pulse automatically reads the connected database schema and presents the organization with their tables and columns. The user selects: which table contains their primary entities, which column is the unique identifier, which column is the display name, which columns are the key behavioral signals, and optionally which column is a timestamp for recency calculations.
Configuration Review — summary of what Pulse understood: org context, database connection status, entity table, and mapped columns. User confirms before finalizing.
First Dashboard Load — Pulse executes its first set of live queries against the organization's database and renders the dashboard with real data.

5.2 Intelligence Dashboard
The dashboard is the primary workspace for operational team members. It is fully dynamic — its content is determined by the organization's data and configuration, not by hardcoded templates.

Overview Panel
Total entity count with breakdown by risk tier (High / Medium / Low / Healthy)
Active recommendation count with urgency distribution
Key metric trends — 4 configurable KPI cards showing the metrics the organization defined as most important
Anomaly alert banner — surfaces any statistical anomalies detected since last login

Entity Explorer
Full searchable, filterable list of all entities in the connected data
Each entity row shows: name/ID, current risk tier, top recommendation, last activity signal, and profile completeness score
Click any entity to open their full behavioral profile — complete history, computed metrics, trend charts, and all recommendations specific to that entity

Recommendations Panel
Real-time queue of all active recommendations generated by the engine
Each recommendation shows: entity affected, recommendation type, urgency level, reasoning summary, and suggested action
Operators can mark recommendations as 'actioned', 'dismissed', or 'escalated'
Outcome tracking — when a recommendation is marked actioned, Pulse tracks whether the entity's metrics improved afterward

Trend & Analytics View
Time-series charts for any metric in the connected data
Segment comparison — compare behavioral profiles across defined segments
Cohort analysis — track how groups of entities evolve over time
Export any chart or table as CSV or PNG

5.3 Conversational Agent
The agent is embedded as a persistent side panel in the dashboard, accessible from any screen. It is context-aware — it knows which screen the user is on, which entity they are viewing, and what the organization's current alert state is.

Sample interactions the agent supports:
'Who are my top 10 highest risk entities this week and what is driving their risk score?'
'Draft a personalized retention message for entity ID 00445 based on their behavioral profile'
'Which segment has shown the sharpest decline in engagement over the last 30 days?'
'Give me a full summary of this week's operational state that I can share with my team'
'We have an urgent situation — 5 new high-priority cases just came in, where do we direct them based on current capacity?'
'Compare the performance of our top 3 locations and tell me what is different about the best performing one'

5.4 Role-Based Access Control
Pulse supports multi-role access within a single organization, ensuring that different team members see the right information for their responsibility level:

Role
Permissions
Organization Admin
Full access to all features, data configuration, user management, billing (cloud), and system settings. Can view and act on all entities across the entire organization.
Operations Manager
Full dashboard access, full agent access, can act on all recommendations, can export data and reports, cannot change system configuration.
Team Member / Analyst
Can view dashboard and entity profiles, can query the agent, can mark recommendations actioned, cannot change configuration or access admin settings.
Read-Only Viewer
Can view dashboard and reports only. Cannot interact with agent or act on recommendations. Designed for executive stakeholders who need visibility without operational access.


5.5 Alerts & Notifications
In-app alert banner when new high-risk entities are detected
Email notifications (configurable) for critical threshold breaches
Daily intelligence digest — automated summary of the day's top recommendations and risk changes
Webhook support (self-hosted) — organizations can push Pulse alerts to their own systems (Slack, Teams, internal tools)



6. Technical Architecture
6.1 Technology Stack
Component
Technology
Frontend
Next.js 14, TailwindCSS, shadcn/ui, Recharts / ApexCharts
Backend API
FastAPI (Python) — async-first, native SQLAlchemy support for multi-database connections
Pulse App Database
PostgreSQL — stores only org configuration, schema metadata, connection credentials (encrypted), recommendation records, user accounts, and audit logs. Never stores client data.
AI / LLM Layer
Anthropic Claude API (claude-sonnet-4) with tool calling for the conversational agent
Database Connectivity
SQLAlchemy with async drivers — asyncpg (PostgreSQL), aiomysql (MySQL), aiosqlite (SQLite). Connects to client databases on demand.
Credential Encryption
Fernet symmetric encryption (cryptography library) for storing client DB credentials at rest
Authentication
JWT-based auth with refresh tokens. Role-based access control enforced at API layer.
Containerization
Docker + Docker Compose for self-hosted deployment. Single command setup.


6.2 Multi-Tenancy Architecture
Pulse's own application database uses shared tables with tenant_id scoping. Every table that contains organization-specific data has a tenant_id UUID column, and every query filters by this value extracted from the authenticated user's JWT token. This applies to: organization configuration, schema metadata, connection credentials, recommendation records, user accounts, and audit logs.

Client data — the actual operational data in the organization's database — is never stored in Pulse's application database. It lives exclusively in the organization's own database and is accessed on demand via the encrypted connection credentials. This is the fundamental data privacy guarantee Pulse makes: Pulse stores your configuration, not your data.

6.3 Database Connection Manager
The Connection Manager is the core infrastructure layer of Pulse. It is architecturally equivalent to how n8n manages credentials and connections — a secure, centralized system for storing and using database connection details on behalf of organizations.

How It Works
During onboarding, the organization provides their database connection details through the wizard UI
The backend validates the connection — attempts a test query (SELECT 1) to confirm reachability and credentials
On success, the connection string is encrypted using Fernet symmetric encryption and stored in Pulse's connections table
The encryption key lives in the server environment variable — never in the database
When any dashboard panel loads or the agent executes a tool call, the Connection Manager decrypts the credentials, opens a connection to the client database, executes the query, closes the connection, and returns results
Connections are never left open — each query opens, executes, and closes. Connection pooling is applied per-org for performance.

Connection Security Model
Credentials are encrypted at rest using Fernet — if the database is compromised, credentials are not exposed
Pulse only ever requests read-only access — the connection user should have SELECT privileges only
For self-hosted deployments, the client database is typically on the same network as Pulse — the connection never traverses the public internet
For cloud version, connections to client databases use TLS/SSL. Organizations can optionally use an SSH tunnel or VPN for additional security
Pulse never stores query results — data is fetched, used to compose the response, and discarded

Connection Table Schema
connections
  id               UUID PRIMARY KEY  org_id           UUID REFERENCES organizations(id)  db_type          VARCHAR(20)   -- 'postgres' | 'mysql' | 'sqlite' | 'mssql'  encrypted_dsn    TEXT          -- Fernet-encrypted connection string  host             VARCHAR(255)  -- stored unencrypted for display only  port             INTEGER  database_name    VARCHAR(255)  status           VARCHAR(20)   -- 'active' | 'error' | 'untested'  last_tested_at   TIMESTAMPTZ  created_at       TIMESTAMPTZ DEFAULT NOW()

6.4 Dynamic Query Engine
The Query Engine translates dashboard data requests and agent tool calls into SQL that executes against the organization's live database. It uses the schema metadata and mapping configuration stored during onboarding to construct queries correctly for any organization's data structure.

Key query patterns the engine generates:
Entity list query — SELECT entity_id, display_name, signal_columns FROM entity_table ORDER BY risk_signal DESC LIMIT n
Entity profile query — SELECT * FROM entity_table WHERE entity_id = :id with all mapped columns
Risk distribution query — aggregate COUNT grouped by computed risk tiers based on configured signal thresholds
Trend query — SELECT date_column, AVG(signal_column) FROM entity_table GROUP BY date_trunc('day', date_column) ORDER BY date
Search query — SELECT ... FROM entity_table WHERE display_name ILIKE :search OR entity_id::text ILIKE :search

All queries are parameterized — never string-interpolated — to prevent SQL injection. The query engine validates all column references against the stored schema metadata before execution, rejecting any query that references a column not in the organization's schema.

6.5 LLM Agent Architecture
The Pulse agent is built on the Anthropic Claude API using structured tool calling. Every tool the agent calls translates into a live query against the organization's database via the Query Engine and Connection Manager. The agent never answers data-dependent questions from general knowledge.

The agent's system prompt is dynamically constructed at request time and contains:
The organization's business context (from their onboarding configuration)
The database schema context — table name, column names, and their mapped roles (entity ID, display name, behavioral signals)
The current operational state summary fetched via a live query (total entities, risk distribution, active recommendation count)
Behavioral instructions — tone, response format, and escalation rules

This dynamic construction means the agent always understands the organization's data structure and can generate accurate tool calls that the Query Engine can execute.

6.6 Self-Hosted Deployment
The self-hosted version of Pulse is designed for a one-command deployment experience:

  git clone https://github.com/pulse-platform/pulse
  cp .env.example .env  # add ANTHROPIC_API_KEY + ENCRYPTION_KEY
  docker compose up -d
When deployed inside the organization's own network, Pulse connects directly to internal databases without any data ever leaving the organization's environment. The only outbound network call Pulse makes is to the LLM API — and only the agent's question plus schema metadata is sent, never the underlying data. Organizations that require complete air-gapping can configure Pulse to use a locally hosted LLM via an OpenAI-compatible endpoint.



7. Monetization & Pricing Model
7.1 What Is Free (Open Source)
The following is completely free, open-source, and will always remain so under the MIT license:

The complete Pulse platform source code
Self-hosted deployment via Docker Compose
Unlimited organizations (in self-hosted mode)
Unlimited users per organization (in self-hosted mode)
Full dashboard, agent, and recommendation engine functionality
All data connectors (PostgreSQL, MySQL, SQLite, MSSQL, CSV, Excel)
Full API access
Community support via GitHub Issues and Discussions

WHY FREE?:  The open-source model is a deliberate distribution and trust strategy. Enterprises that require self-hosted deployment — which is the majority of Bluechip's target clients — can deploy and evaluate Pulse completely free. Monetization comes from the cloud version and from commercial services layered on top.


7.2 Cloud Version Pricing Tiers
Tier
Price
What's Included
Starter
Free forever
1 organization, up to 3 users, up to 10,000 entities, CSV/Excel upload only, 500 agent queries/month, community support, Pulse branding on dashboard
Growth
$49/month (approx. ₦50,000/month)
1 organization, up to 15 users, up to 100,000 entities, direct DB connection, 5,000 agent queries/month, email alerts, email support, remove Pulse branding
Business
$149/month (approx. ₦155,000/month)
Up to 3 organizations, unlimited users, up to 1,000,000 entities, direct DB connection, unlimited agent queries, webhook integrations, priority support, custom domain
Enterprise
Custom pricing
Unlimited organizations, unlimited users, unlimited entities, dedicated infrastructure, SLA guarantee, on-premise deployment support, dedicated customer success manager, custom integrations, white-label option


7.3 Additional Revenue Streams
Managed Self-Hosted Deployments
Organizations that want the security of self-hosting but do not have the DevOps capacity to manage the deployment themselves can pay Pulse (or a licensed partner like Bluechip Technologies) to manage the self-hosted infrastructure on their behalf. This is billed as a managed services contract, typically $500-$2,000/month depending on organization size.

Implementation & Integration Services
For large enterprise deployments, Pulse offers paid professional services: custom data connector development, onboarding configuration assistance, custom dashboard development, and integration with enterprise systems (ERP, CRM, data warehouses). This is the primary revenue model for partnerships with system integrators like Bluechip Technologies.

Partner / Reseller Program
System integrators (like Bluechip Technologies) can become licensed Pulse resellers. They deploy Pulse for their clients, charge a margin on the subscription or managed service fee, and receive a revenue share back to Pulse. This is how Pulse scales distribution into enterprise accounts without a direct enterprise sales team.

LLM API Costs (Passed Through)
On the cloud version, LLM API costs (Anthropic/OpenAI) are included in the subscription fee up to the stated query limits. On the self-hosted version, organizations bring their own LLM API key and pay their own LLM costs directly. This is intentional — it keeps the self-hosted version truly free while allowing cloud pricing to factor in real infrastructure costs.

IMPORTANT PRINCIPLE:  The self-hosted version is never paywalled. An organization could theoretically use the full self-hosted Pulse forever for free. The cloud version and commercial services exist for organizations that value convenience, managed infrastructure, or dedicated support over DIY deployment.




8. User Stories
8.1 As an Organization Administrator
I want to sign up and configure Pulse for my organization in under 20 minutes so that my team can start getting intelligence from our data without a lengthy IT project
I want to connect Pulse directly to our existing database so that our data never has to leave our environment
I want to manage which team members have access to Pulse and at what permission level so that I can control who sees sensitive operational data
I want to receive a daily intelligence digest so that I start every morning with a clear picture of our operational state without logging in manually

8.2 As an Operations Manager
I want to see which entities are at high risk right now so that I can prioritize my team's interventions without manually reviewing every record
I want to ask the agent questions in plain English so that I can get data-backed answers without knowing SQL or waiting for a data analyst
I want to see a full behavioral profile for any entity so that I understand the history and patterns behind a risk flag before deciding how to act
I want the system to draft a personalized action or communication for a specific entity so that my team can act quickly without starting from scratch every time

8.3 As a Team Member
I want to see recommendations assigned to my area of responsibility so that I know exactly what to focus on today
I want to mark a recommendation as actioned so that the system knows an intervention has been made and can track whether it worked
I want to query the agent about my specific area without having to dig through the full dashboard so that I can get answers quickly during a busy operational day



9. Team & Execution Plan
9.1 Team Composition
Team Member
Role
Responsibilities
Person 1
Frontend Engineer
Next.js, TailwindCSS, shadcn/ui, Recharts. Responsible for all UI — onboarding wizard, dashboard, entity explorer, agent chat interface, role-based views, responsive layout.
Person 2
Backend Engineer
FastAPI, PostgreSQL, SQLAlchemy, pandas. Responsible for API layer, multi-tenant architecture, data ingestion pipeline, recommendation engine, auth system, alert engine.
Person 3
AI / Agent Engineer
Python, Anthropic Claude API, tool calling architecture. Responsible for agent design, tool definitions, dynamic system prompt construction, agent-backend integration, and response quality.


9.2 Build Phases
Phase 1 — Foundation (Days 1-2)
Backend: Project scaffold, database schema, multi-tenant setup, auth endpoints, organization CRUD
Frontend: Next.js setup, TailwindCSS + shadcn/ui configuration, auth pages (login, signup), routing structure
Agent: Claude API integration, base tool calling framework, system prompt template

Phase 2 — Core Features (Days 3-5)
Backend: Onboarding configuration API, data ingestion pipeline (CSV/Excel + PostgreSQL connector), entity profile computation engine, recommendation engine first pass
Frontend: Onboarding wizard (all steps), main dashboard layout, entity explorer with search and filter, entity profile detail view
Agent: All core tool implementations, agent-to-backend integration, agent chat interface in frontend

Phase 3 — Intelligence & Polish (Days 6-7)
Backend: Recommendation engine refinement, alerts engine, background job scheduling with Celery, API performance optimization
Frontend: Trend charts and analytics view, recommendations panel with action tracking, alerts UI, role-based view switching, responsive polish
Agent: Dynamic system prompt construction based on org context, advanced tool implementations, response quality testing

Phase 4 — Demo Preparation (Day 8)
Prepare two demo organizations with realistic sample data (telecom subscriber dataset + hospital patient dataset)
Run full end-to-end demo flow rehearsal
Finalize GitHub repository with README, Docker Compose setup, and self-hosted documentation
Prepare pitch deck aligned to this PRD



10. Go-To-Market Strategy
10.1 Initial Target Market
Pulse's initial target market is mid-to-large enterprises in Nigeria across four industries: telecoms, healthcare (private hospital groups), FMCG distribution, and retail chains. These industries align directly with Bluechip Technologies' existing client base, making them the most natural distribution channel for Pulse's initial enterprise deployments.

10.2 Distribution Strategy
Open Source Community
The GitHub repository is the top of the funnel. Organizations discover Pulse through the community, self-host it for free, and convert to the cloud version or commercial services when they need convenience, scale, or support. This mirrors the growth trajectories of n8n, Metabase, and Grafana.

System Integrator Partnerships
Companies like Bluechip Technologies are the most efficient channel to reach enterprise clients. A partnership model allows Bluechip to deploy Pulse for their existing clients as a managed service, with Pulse receiving a revenue share and Bluechip taking a margin on the deployment. This gives Pulse immediate access to enterprise relationships that would otherwise take years to build directly.

Direct Cloud Sales
The cloud version's free Starter tier allows any organization to sign up without a sales conversation. Conversion to paid tiers is driven by usage limits and feature unlocks, keeping customer acquisition cost low.

10.3 Competitive Positioning
Competitor
Pulse Differentiation
vs. Power BI / Tableau
Pulse is not a BI tool — it reasons and recommends, not just visualizes. It also requires no technical skill to query.
vs. Salesforce / HubSpot
Those are CRMs for specific use cases. Pulse is industry-agnostic and works on any operational data.
vs. Custom ML pipelines
A custom pipeline requires months to build and a data science team to maintain. Pulse is live in under 20 minutes.
vs. Other LLM chatbots
Generic chatbots answer from training data. Pulse's agent answers from the organization's real data via tool calling — no hallucination on operational queries.




11. Success Metrics
11.1 Hackathon Success Criteria
Live demo successfully onboards two organizations from different industries in real time
Agent correctly answers at least 5 live queries using tool calling against real ingested data
Self-hosted deployment path demonstrated via GitHub repository and Docker Compose
Monetization model clearly articulated and defensible under judge questioning
Industry-agnostic positioning clearly communicated and demonstrated

11.2 Post-Hackathon Product Metrics (6 months)
100+ GitHub stars on open-source repository
10+ organizations self-hosting Pulse in production
3+ paying cloud customers
1 system integrator partnership signed
Net Promoter Score (NPS) > 40 from active users



12. Risks & Mitigations
Risk
Description
Mitigation
LLM API Latency
Agent responses may feel slow on live demo
Pre-load org context; use streaming responses; prepare cached demo queries as backup
Data Privacy Objections
Enterprise judges may question data security
Lead with self-hosted deployment model; emphasize data never leaves org environment; reference NDPR compliance design
Scope Complexity
Platform scope is large for a hackathon timeline
Prioritize core flow: onboarding → dashboard → agent. Cut analytics views if time-constrained. Demo depth over breadth.
Industry Agnosticism Clarity
Judges may not immediately grasp the platform concept
Demo two industries back-to-back. The contrast is the proof.
LLM API Costs
High query volume during demo could incur unexpected costs
Set API rate limits; use a dedicated demo API key with spend cap




Appendix: Glossary
Term
Definition
Entity
The primary object being modeled in an organization's data. Could be a subscriber, patient, customer, product, store, or any other unit depending on the industry.
Connection Manager
The Pulse subsystem that securely stores encrypted database credentials and opens on-demand connections to the organization's database to execute queries. Client data never moves — the Connection Manager goes to the data, not the other way around.
Schema Metadata
The only data Pulse copies from the organization's database: table names, column names, and data types. Used by the Query Engine to construct valid SQL. Never contains actual row data.
Query Engine
The Pulse subsystem that translates dashboard data requests and agent tool calls into parameterized SQL queries, executes them against the live client database, and returns results. Prevents SQL injection by validating all column references against stored schema metadata.
Recommendation Engine
The part of the Query Engine that runs analytical queries against live client data to identify entities matching risk patterns, generating prioritized next-best-action recommendations. Only the recommendation record is stored in Pulse — the underlying data stays in the client's DB.
Tool Calling
A feature of modern LLM APIs that allows the model to invoke structured functions rather than generating answers from training data. Pulse uses tool calling to ground all agent responses in live queries against the organization's real database.
Multi-Tenancy
An architecture pattern where a single Pulse application instance serves multiple organizations, with complete configuration and data isolation enforced by tenant_id scoping.
Self-Hosted
A deployment model where the organization runs Pulse on their own servers or cloud infrastructure. Pulse connects to their internal databases directly — no data ever leaves the organization's network.
NDPR
Nigeria Data Protection Regulation. Pulse's architecture — where client data never moves out of their environment — is designed to be compatible with NDPR and similar data sovereignty regulations.



PULSE — Real-Time Intelligence for Any Business
DSN x Bluechip Technologies LLM Agent Challenge Hackathon 3.0  |  June 2025


SELF_HOSTED_COMPOSE = docker/compose/self-hosted/docker-compose.yml
CLOUD_COMPOSE       = docker/compose/cloud/docker-compose.yml

SELF_HOSTED_IMAGE   = pulseai/pulse
CLOUD_IMAGE         = pulseai/pulse-cloud
LICENSE_IMAGE       = pulseai/pulse-license

# Maintainer builds only — public users pull pulseai/pulse from Docker Hub.
# Default: ../dashboard (sibling of this repo, e.g. ~/Desktop/dashboard next to ~/Desktop/api).
PULSE_DASHBOARD_DIR ?= ../dashboard
tag                 ?= latest

# ── Build images ──────────────────────────────────────────────────────────────

build-self-hosted:
	docker build \
		-f docker/images/pulse/Dockerfile \
		--build-context frontend=$(PULSE_DASHBOARD_DIR) \
		-t $(SELF_HOSTED_IMAGE):$(tag) \
		-t $(SELF_HOSTED_IMAGE):latest \
		.

build-cloud:
	docker build \
		-f docker/images/pulse-cloud/Dockerfile \
		-t $(CLOUD_IMAGE):$(tag) \
		-t $(CLOUD_IMAGE):latest \
		.

build-license:
	docker build \
		-f docker/images/pulse-license/Dockerfile \
		-t $(LICENSE_IMAGE):$(tag) \
		-t $(LICENSE_IMAGE):latest \
		.

build: build-self-hosted build-cloud build-license

push-self-hosted:
	docker push $(SELF_HOSTED_IMAGE):$(tag)
	docker push $(SELF_HOSTED_IMAGE):latest

push-cloud:
	docker push $(CLOUD_IMAGE):$(tag)
	docker push $(CLOUD_IMAGE):latest

push-license:
	docker push $(LICENSE_IMAGE):$(tag)
	docker push $(LICENSE_IMAGE):latest

push: push-self-hosted push-cloud push-license

# ── Self-hosted (db + qdrant + pulse all-in-one) ──────────────────────────────

sh-up:
	docker compose -f $(SELF_HOSTED_COMPOSE) up -d

sh-down:
	docker compose -f $(SELF_HOSTED_COMPOSE) down

sh-logs:
	docker compose -f $(SELF_HOSTED_COMPOSE) logs -f

sh-pull:
	docker compose -f $(SELF_HOSTED_COMPOSE) pull

# ── Cloud (internal) ──────────────────────────────────────────────────────────

up:
	docker compose -f $(CLOUD_COMPOSE) up -d

down:
	docker compose -f $(CLOUD_COMPOSE) down

logs:
	docker compose -f $(CLOUD_COMPOSE) logs -f $(svc)

scale-workers:
	docker compose -f $(CLOUD_COMPOSE) up --scale worker=$(n) -d

# ── Local dev (no Docker) ─────────────────────────────────────────────────────

dev:
	uvicorn app.api.app:app --reload --host 0.0.0.0 --port 8000

dev-scheduler:
	python -m app.services.schedulers.run

# ── Database ──────────────────────────────────────────────────────────────────

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(name)"

seed:
	python scripts/db/seed_telecom_db.py

reset-db:
	python scripts/db/reset_db.py

.PHONY: build-self-hosted build-cloud build-license build \
        push-self-hosted push-cloud push-license push \
        sh-up sh-down sh-logs sh-pull \
        up down logs scale-workers dev dev-scheduler \
        migrate revision seed reset-db

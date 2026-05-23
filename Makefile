SELF_HOSTED_COMPOSE = docker/compose/self-hosted/docker-compose.yml
CLOUD_COMPOSE       = docker/compose/cloud/docker-compose.yml

SELF_HOSTED_IMAGE   = chideraozigbo488/entivia
CLOUD_IMAGE         = chideraozigbo488/entivia-cloud
LICENSE_IMAGE       = chideraozigbo488/entivia-license

# Maintainer builds only — public users pull chideraozigbo488/entivia from Docker Hub.
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

# ── Hackathon (DSN x Bluechip) ────────────────────────────────────────────────

HACKATHON_YELP_HOST_DIR ?= $(HOME)/datasets/yelp
export HACKATHON_YELP_HOST_DIR

hackathon-build:
	docker compose -f hackathon/docker-compose.yml build task-a-api

hackathon-up:
	docker compose -f hackathon/docker-compose.yml up -d --build

hackathon-down:
	docker compose -f hackathon/docker-compose.yml down

hackathon-logs:
	docker compose -f hackathon/docker-compose.yml logs -f task-a-api task-b-api hackathon-api

hackathon-load:
	@if [ -d "$(HACKATHON_YELP_HOST_DIR)" ]; then \
		echo "[info] mounting $(HACKATHON_YELP_HOST_DIR) -> /data/yelp"; \
		docker compose -f hackathon/docker-compose.yml run --rm \
			-v "$(HACKATHON_YELP_HOST_DIR)":/data/yelp:ro loader; \
	else \
		echo "[warn] HACKATHON_YELP_HOST_DIR=$(HACKATHON_YELP_HOST_DIR) not found; using synthetic data"; \
		docker compose -f hackathon/docker-compose.yml run --rm \
			-e HACKATHON_YELP_DIR= loader; \
	fi

hackathon-eval:
	docker compose -f hackathon/docker-compose.yml run --rm hackathon-api \
		python -m hackathon.eval.run --task-a-sample 30 --task-b-users 30

# Use xelatex + system Unicode fonts so symbols (≥, ↑, ↓, ★, ≈) and the
# box-drawing characters in the inline ASCII diagrams render correctly.
# Helvetica/Menlo/DejaVu Sans are present on macOS and most Linux distros.
HACKATHON_PDF_FONTS = \
	-V mainfont="Times New Roman" \
	-V monofont="Menlo"

hackathon-paper-a-pdf:
	@command -v pandoc >/dev/null || (echo "Install pandoc to export PDF" && exit 1)
	pandoc hackathon/paper/task_a_review_simulation.md -o hackathon/paper/task_a_review_simulation.pdf \
		--pdf-engine=xelatex --resource-path=hackathon/paper -V geometry:margin=1in $(HACKATHON_PDF_FONTS)

hackathon-paper-b-pdf:
	@command -v pandoc >/dev/null || (echo "Install pandoc to export PDF" && exit 1)
	pandoc hackathon/paper/task_b_recommendation.md -o hackathon/paper/task_b_recommendation.pdf \
		--pdf-engine=xelatex --resource-path=hackathon/paper -V geometry:margin=1in $(HACKATHON_PDF_FONTS)

hackathon-paper-pdf: hackathon-paper-a-pdf hackathon-paper-b-pdf

.PHONY: build-self-hosted build-cloud build-license build \
        push-self-hosted push-cloud push-license push \
        sh-up sh-down sh-logs sh-pull \
        up down logs scale-workers dev dev-scheduler \
        migrate revision seed reset-db \
        hackathon-build hackathon-up hackathon-down hackathon-logs \
        hackathon-load hackathon-eval hackathon-paper-pdf \
        hackathon-paper-a-pdf hackathon-paper-b-pdf

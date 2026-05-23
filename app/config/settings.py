"""
Central Configuration for Pulse
All environment variables loaded here.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional
from functools import lru_cache
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(project_root / ".env")

logger = logging.getLogger(__name__)

CLAUDE_MODEL_SONNET_4_6 = "claude-sonnet-4-6"  # general default
CLAUDE_MODEL_OPUS_4_6 = "claude-opus-4-6"  # highest capability
CLAUDE_MODEL_HAIKU_4_5 = "claude-haiku-4-5-20251001"  # fast/cheap tier

GROQ_MODEL_HEAVY = "openai/gpt-oss-120b"  # analytical/schema-heavy work
GROQ_MODEL_DEFAULT = "llama-3.3-70b-versatile"  # routing/tools/structured JSON
GROQ_MODEL_FAST = "llama-3.1-8b-instant"  # low-latency/simple tasks

# Written at Docker image build time (docker/images/pulse/Dockerfile). When present,
# deployment_mode and license URLs are taken from this file only — not .env or compose.
PULSE_BUILD_CONFIG_PATH = Path("/etc/pulse/build-config.json")
_DEFAULT_LICENSE_SERVER_URL = "https://license.pulseai.io"


@lru_cache(maxsize=1)
def _pulse_build_config() -> dict:
    if not PULSE_BUILD_CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(PULSE_BUILD_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read %s: %s", PULSE_BUILD_CONFIG_PATH, exc)
        return {}


def _resolve_deployment_mode() -> str:
    baked = _pulse_build_config().get("deployment_mode")
    if baked:
        return str(baked).lower()
    return os.getenv("DEPLOYMENT_MODE", "cloud").lower()


def _resolve_license_server_url() -> str:
    baked = _pulse_build_config().get("license_server_url")
    if baked:
        return str(baked).rstrip("/")
    if _resolve_deployment_mode() == "self_hosted":
        return _DEFAULT_LICENSE_SERVER_URL
    return os.getenv("LICENSE_SERVER_URL", _DEFAULT_LICENSE_SERVER_URL).rstrip("/")


def _resolve_license_jwt_issuer() -> Optional[str]:
    baked = _pulse_build_config().get("license_jwt_issuer")
    if baked:
        raw = str(baked).strip()
        return raw or None
    if _resolve_deployment_mode() == "self_hosted":
        return _DEFAULT_LICENSE_SERVER_URL
    raw = os.getenv("LICENSE_JWT_ISSUER", _DEFAULT_LICENSE_SERVER_URL).strip()
    return raw or None


def _resolve_license_public_key() -> Optional[str]:
    baked = _pulse_build_config().get("license_public_key")
    if baked:
        raw = str(baked).strip()
        return raw or None
    raw = os.getenv("PULSE_LICENSE_PUBLIC_KEY", "").strip()
    return raw or None

VOYAGE_EMBEDDING_MODEL = "voyage-4-large"
VOYAGE_EMBEDDING_DIMENSION = 1024


def _fetch_secret_from_arn(secret_arn: str) -> Optional[dict]:
    """Fetch secret value from AWS Secrets Manager."""
    try:
        import boto3
        logger.info(f"Fetching secret from ARN: {secret_arn[:60]}...")
        client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
        response = client.get_secret_value(SecretId=secret_arn)
        secret_data = json.loads(response["SecretString"])
        logger.info(f"✅ Successfully loaded {len(secret_data)} keys from Secrets Manager")
        return secret_data
    except Exception as e:
        logger.error(f"Failed to fetch secret {secret_arn}: {type(e).__name__}: {e}")
        return None


def _get_database_url() -> Optional[str]:
    """Get DATABASE_URL from env or construct from Secrets Manager."""
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")

    secret_arn = os.getenv("DATABASE_URL_SECRET_ARN")
    if not secret_arn:
        return None

    secret = _fetch_secret_from_arn(secret_arn)
    if not secret:
        return None

    host = os.getenv("DB_HOST") or secret.get("host")
    port = secret.get("port", 5432)
    username = secret.get("username")
    password = secret.get("password")
    dbname = os.getenv("DB_NAME", "telecom")

    if not all([host, username, password]):
        logger.error("Missing required database credentials in secret")
        return None

    url = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"
    logger.info(f"Constructed DATABASE_URL for host: {host}")
    return url


_cached_database_url: Optional[str] = None


def get_database_url() -> Optional[str]:
    global _cached_database_url
    if _cached_database_url is None:
        _cached_database_url = _get_database_url()
    return _cached_database_url

def _get_api_secrets() -> dict:
    secret_arn = os.getenv("API_SECRETS_ARN")
    if not secret_arn:
        return {}
    secret = _fetch_secret_from_arn(secret_arn)
    return secret or {}


class Settings:

    _api_secrets: dict = {}

    @classmethod
    def _init_api_secrets(cls) -> None:
        if not cls._api_secrets:
            secret_arn = os.getenv("API_SECRETS_ARN")
            if secret_arn:
                logger.info("Initializing API secrets from Secrets Manager...")
                cls._api_secrets = _get_api_secrets()
                logger.info(f"Loaded {len(cls._api_secrets)} API secrets from Secrets Manager")
            else:
                logger.debug("API_SECRETS_ARN not set - using environment variables for secrets")

    @classmethod
    def _get_secret(cls, env_key: str, secret_key: str) -> Optional[str]:
        env_val = os.getenv(env_key)
        if env_val:
            return env_val
        cls._init_api_secrets()
        return cls._api_secrets.get(secret_key)

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------
    _database_url: Optional[str] = None

    @classmethod
    def get_database_url(cls) -> Optional[str]:
        if cls._database_url is None:
            cls._database_url = get_database_url()
        return cls._database_url

    @property
    def DATABASE_URL(self) -> Optional[str]:
        return self.get_database_url()

    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "10"))
    DATABASE_POOL_TIMEOUT: int = int(os.getenv("DATABASE_POOL_TIMEOUT", "30"))
    DATABASE_ECHO: bool = os.getenv("DATABASE_ECHO", "false").lower() == "true"

    @classmethod
    def is_database_configured(cls) -> bool:
        return bool(cls.get_database_url())

    @classmethod
    def get_async_database_url(cls) -> Optional[str]:
        url = cls.get_database_url()
        if not url:
            return None
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    # ------------------------------------------------------------------
    # Redis: refresh-token rotation, email/pw reset tokens, email-verify
    # rate limit, public API per-key rate limits. If unset, refresh tokens
    # fall back to stateless JWTs (see app.api.auth.routes._issue_tokens).
    # ------------------------------------------------------------------
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL", "").strip() or None

    # ------------------------------------------------------------------
    # Deployment / service URLs
    # ------------------------------------------------------------------
    DEPLOYMENT_MODE: str = _resolve_deployment_mode()
    AGENT_SERVICE_URL: Optional[str] = os.getenv("AGENT_SERVICE_URL", "").strip() or None
    LICENSE_SERVER_URL: str = _resolve_license_server_url()
    LICENSE_SERVER_API_KEY: Optional[str] = os.getenv("LICENSE_SERVER_API_KEY", "").strip() or None
    # RSA PEM for offline plc_* JWT verification — baked into self-hosted image or .env override
    PULSE_LICENSE_PUBLIC_KEY: Optional[str] = _resolve_license_public_key()
    LICENSE_JWT_ISSUER: Optional[str] = _resolve_license_jwt_issuer()
    LICENSE_OFFLINE_GRACE_DAYS: int = int(os.getenv("LICENSE_OFFLINE_GRACE_DAYS", "7"))
    LICENSE_REVALIDATION_INTERVAL_HOURS: int = int(os.getenv("LICENSE_REVALIDATION_INTERVAL_HOURS", "24"))

    # ------------------------------------------------------------------
    # Qdrant (vector search for entity retrieval)
    # ------------------------------------------------------------------
    QDRANT_URL: Optional[str] = os.getenv("QDRANT_URL", "").strip() or None
    QDRANT_API_KEY: Optional[str] = os.getenv("QDRANT_API_KEY", "").strip() or None
    QDRANT_COLLECTION_PREFIX: str = os.getenv("QDRANT_COLLECTION_PREFIX", "pulse_org_")
    QDRANT_VECTOR_SIZE: int = int(
        os.getenv("QDRANT_VECTOR_SIZE", str(VOYAGE_EMBEDDING_DIMENSION))
    )

    @classmethod
    def is_qdrant_configured(cls) -> bool:
        return bool(cls.QDRANT_URL)

    @classmethod
    def get_org_collection_name(cls, org_id: str) -> str:
        return f"{cls.QDRANT_COLLECTION_PREFIX}{org_id}"

    @classmethod
    def get_org_memory_collection_name(cls, org_id: str) -> str:
        """Per-org Qdrant collection for conversational/episodic memory entries."""
        return f"{cls.QDRANT_COLLECTION_PREFIX}{org_id}_memory"

    # ------------------------------------------------------------------
    # Voyage AI (embeddings for vector search)
    # ------------------------------------------------------------------
    VOYAGEAI_API_KEY: Optional[str] = os.getenv("VOYAGEAI_API_KEY")
    EMBEDDING_MODEL: str = VOYAGE_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = VOYAGE_EMBEDDING_DIMENSION

    @classmethod
    def get_voyageai_api_key(cls) -> Optional[str]:
        return cls._get_secret("VOYAGEAI_API_KEY", "VOYAGEAI_API_KEY")

    @classmethod
    def is_voyage_configured(cls) -> bool:
        return bool(cls.get_voyageai_api_key())

    @classmethod
    def is_voyageai_configured(cls) -> bool:
        return cls.is_voyage_configured()

    # ------------------------------------------------------------------
    # RAG tuning (env-overridable; per-org overrides can be layered later)
    # ------------------------------------------------------------------
    VOYAGE_RERANK_MODEL: str = os.getenv("VOYAGE_RERANK_MODEL", "rerank-2.5")
    RAG_PREFETCH_K: int = int(os.getenv("RAG_PREFETCH_K", "20"))
    RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "3"))
    RAG_SCORE_THRESHOLD: float = float(os.getenv("RAG_SCORE_THRESHOLD", "0.6"))
    RAG_ENABLE_RERANK: bool = os.getenv("RAG_ENABLE_RERANK", "true").lower() == "true"
    RAG_ENABLE_HYBRID: bool = os.getenv("RAG_ENABLE_HYBRID", "true").lower() == "true"
    RAG_ENABLE_QUERY_REWRITE: bool = (
        os.getenv("RAG_ENABLE_QUERY_REWRITE", "true").lower() == "true"
    )
    RAG_FRESHNESS_WINDOW_DAYS: int = int(
        os.getenv("RAG_FRESHNESS_WINDOW_DAYS", "180")
    )
    QDRANT_TTL_DAYS: int = int(os.getenv("QDRANT_TTL_DAYS", "365"))
    RAG_ENABLE_AUTOCUT: bool = os.getenv("RAG_ENABLE_AUTOCUT", "true").lower() == "true"
    RAG_EVAL_RECALL_THRESHOLD: float = float(os.getenv("RAG_EVAL_RECALL_THRESHOLD", "0.5"))
    RAG_THRESHOLD_FALLBACK_FACTOR: float = float(os.getenv("RAG_THRESHOLD_FALLBACK_FACTOR", "0.7"))
    RAG_ENABLE_QUERY_DECOMPOSE: bool = os.getenv("RAG_ENABLE_QUERY_DECOMPOSE", "false").lower() == "true"
    RAG_ENABLE_HIERARCHICAL_CHUNKS: bool = os.getenv("RAG_ENABLE_HIERARCHICAL_CHUNKS", "true").lower() == "true"
    RAG_ENABLE_QUERY_EXPANSION: bool = os.getenv("RAG_ENABLE_QUERY_EXPANSION", "false").lower() == "true"
    RAG_ENABLE_RETRIEVAL_VALIDATION: bool = os.getenv("RAG_ENABLE_RETRIEVAL_VALIDATION", "false").lower() == "true"
    RAG_VALIDATION_MIN_RELEVANT: int = int(os.getenv("RAG_VALIDATION_MIN_RELEVANT", "1"))
    RAG_ENRICH_CONCURRENCY: int = max(
        1, int(os.getenv("RAG_ENRICH_CONCURRENCY", "8"))
    )
    AGENT_CONTEXT_COMPRESS_THRESHOLD: int = int(os.getenv("AGENT_CONTEXT_COMPRESS_THRESHOLD", "60000"))
    AGENT_CONTEXT_COMPRESS_KEEP_RECENT: int = int(os.getenv("AGENT_CONTEXT_COMPRESS_KEEP_RECENT", "6"))

    # Conversational semantic memory (episodic store in Qdrant)
    CONV_MEMORY_ENABLED: bool = os.getenv("CONV_MEMORY_ENABLED", "true").lower() == "true"
    CONV_MEMORY_RECALL_K: int = int(os.getenv("CONV_MEMORY_RECALL_K", "3"))
    CONV_MEMORY_IMPORTANCE_THRESHOLD: float = float(os.getenv("CONV_MEMORY_IMPORTANCE_THRESHOLD", "0.5"))
    CONV_MEMORY_RETENTION_DAYS: int = int(os.getenv("CONV_MEMORY_RETENTION_DAYS", "180"))
    CONV_MEMORY_MIN_RECALL_SCORE: float = float(os.getenv("CONV_MEMORY_MIN_RECALL_SCORE", "0.30"))
    CONV_MEMORY_IDLE_SUMMARY_MINUTES: int = int(os.getenv("CONV_MEMORY_IDLE_SUMMARY_MINUTES", "30"))
    CONV_MEMORY_HANDOFF_K: int = int(os.getenv("CONV_MEMORY_HANDOFF_K", "2"))

    # Conversational agent split (Query Agent + Synthesis Agent)
    CONV_AGENT_SPLIT_ENABLED: bool = os.getenv("CONV_AGENT_SPLIT_ENABLED", "false").lower() == "true"

    # Chat context window
    CHAT_CONTEXT_WINDOW_MESSAGES: int = int(os.getenv("CHAT_CONTEXT_WINDOW_MESSAGES", "20"))
    CHAT_CONTEXT_SUMMARY_OVERFLOW: int = int(os.getenv("CHAT_CONTEXT_SUMMARY_OVERFLOW", "6"))

    # Semantic intent detection (fast classifier ahead of ReAct loop)
    CHAT_INTENT_DETECTION_ENABLED: bool = os.getenv("CHAT_INTENT_DETECTION_ENABLED", "true").lower() == "true"
    CHAT_INTENT_FASTPATH_CONFIDENCE: float = float(os.getenv("CHAT_INTENT_FASTPATH_CONFIDENCE", "0.85"))

    # Dashboard builder: allow add_chart/replace_chart.
    DASHBOARD_ITERATION_ALLOW_NEW_SQL: bool = os.getenv("DASHBOARD_ITERATION_ALLOW_NEW_SQL", "true").lower() == "true"

    # ------------------------------------------------------------------
    # Groq API (LLM)
    # ------------------------------------------------------------------
    GROQ_API_KEY: Optional[str] = os.getenv("GROQ_API_KEY")

    GROQ_LLM_MODEL_HEAVY: str = GROQ_MODEL_HEAVY
    GROQ_LLM_MODEL: str = GROQ_MODEL_DEFAULT
    GROQ_LLM_MODEL_FAST: str = GROQ_MODEL_FAST

    @property
    def groq_api_key(self) -> Optional[str]:
        return self._get_secret("GROQ_API_KEY", "GROQ_API_KEY")

    @classmethod
    def is_groq_configured(cls) -> bool:
        return bool(cls._get_secret("GROQ_API_KEY", "GROQ_API_KEY"))

    # ------------------------------------------------------------------
    # Anthropic API (LLM)
    # ------------------------------------------------------------------
    ANTHROPIC_API_KEY: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    ANTHROPIC_LLM_MODEL: str = CLAUDE_MODEL_SONNET_4_6
    ANTHROPIC_LLM_MODEL_HEAVY: str = CLAUDE_MODEL_OPUS_4_6
    ANTHROPIC_LLM_MODEL_FAST: str = CLAUDE_MODEL_HAIKU_4_5

    @classmethod
    def get_anthropic_api_key(cls) -> Optional[str]:
        return cls._get_secret("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")

    @classmethod
    def is_anthropic_configured(cls) -> bool:
        return bool(cls.get_anthropic_api_key())

    # ------------------------------------------------------------------
    # Google OAuth
    # ------------------------------------------------------------------
    GOOGLE_CLIENT_ID: Optional[str] = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET: Optional[str] = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI: str = os.getenv(
        "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/v1/auth/oauth/google/callback"
    )
    JWT_SECRET: str = os.getenv("JWT_SECRET", "pulse-dev-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
    JWT_EXPIRY_HOURS: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))  # legacy; prefer ACCESS_TOKEN_EXPIRE_MINUTES
    ENCRYPTION_KEY: str = os.getenv(
        "ENCRYPTION_KEY",
        "K_k8N_IyoXaDyql8ijHUmO9KA6FyuAqP7guglrC0Pns=",
    )
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    OAUTH_REDIRECT_ALLOWLIST: str = os.getenv("OAUTH_REDIRECT_ALLOWLIST", "")
    PASSWORD_MIN_LENGTH: int = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
    PASSWORD_HASH_ITERATIONS: int = int(
        os.getenv("PASSWORD_HASH_ITERATIONS", "600000")
    )
    PASSWORD_RESET_TOKEN_EXPIRY_MINUTES: int = int(
        os.getenv("PASSWORD_RESET_TOKEN_EXPIRY_MINUTES", "30")
    )
    PASSWORD_RESET_PATH: str = os.getenv("PASSWORD_RESET_PATH", "/reset-password")

    @classmethod
    def get_google_client_id(cls) -> Optional[str]:
        return cls._get_secret("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_ID")

    @classmethod
    def get_google_client_secret(cls) -> Optional[str]:
        return cls._get_secret("GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET")

    @classmethod
    def is_google_oauth_configured(cls) -> bool:
        return bool(cls.get_google_client_id() and cls.get_google_client_secret())

    @classmethod
    def oauth_redirect_origins(cls) -> set[str]:
        from urllib.parse import urlparse

        origins: set[str] = set()
        for base in (cls.FRONTEND_URL,):
            parsed = urlparse(base.strip())
            if parsed.scheme and parsed.netloc:
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
        for part in cls.OAUTH_REDIRECT_ALLOWLIST.split(","):
            part = part.strip()
            if not part:
                continue
            parsed = urlparse(part if "://" in part else f"https://{part}")
            if parsed.scheme and parsed.netloc:
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
        return origins

    @classmethod
    def is_oauth_redirect_allowed(cls, redirect_uri: str) -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(redirect_uri.strip())
        if not parsed.scheme or not parsed.netloc:
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin in cls.oauth_redirect_origins()

    @classmethod
    def default_oauth_callback_dest(cls) -> str:
        return f"{cls.FRONTEND_URL.rstrip('/')}/auth/oauth/callback"

    # ------------------------------------------------------------------
    # Email (Resend)
    # ------------------------------------------------------------------
    RESEND_API_KEY: Optional[str] = os.getenv("RESEND_API_KEY")
    DEFAULT_FROM_EMAIL: str = os.getenv("DEFAULT_FROM_EMAIL", "noreply@flowpilot.club")
    ACCEPT_INVITE_PATH: str = os.getenv("ACCEPT_INVITE_PATH", "/accept-invite")

    @classmethod
    def get_resend_api_key(cls) -> Optional[str]:
        return cls._get_secret("RESEND_API_KEY", "RESEND_API_KEY")

    @classmethod
    def is_email_configured(cls) -> bool:
        return bool(cls.get_resend_api_key())

    # ------------------------------------------------------------------
    # Paystack (billing / subscriptions)
    # ------------------------------------------------------------------
    PAYSTACK_SECRET_KEY: Optional[str] = os.getenv("PAYSTACK_SECRET_KEY")
    # Cloud recurring plans (create each plan in Paystack dashboard)
    PAYSTACK_PRO_PLAN_CODE: Optional[str] = os.getenv("PAYSTACK_PRO_PLAN_CODE")
    PAYSTACK_GROWTH_PLAN_CODE: Optional[str] = os.getenv("PAYSTACK_GROWTH_PLAN_CODE")
    # Days to keep paid entitlements after invoice.payment_failed before downgrading to free
    BILLING_GRACE_DAYS: int = int(os.getenv("BILLING_GRACE_DAYS", "7"))
    # Self-hosted one-time license purchase price in kobo (e.g. 5000000 = ₦50,000)
    PAYSTACK_SELFHOSTED_LICENSE_PRICE: int = int(os.getenv("PAYSTACK_SELFHOSTED_LICENSE_PRICE", "0"))

    @classmethod
    def get_paystack_secret_key(cls) -> Optional[str]:
        return cls._get_secret("PAYSTACK_SECRET_KEY", "PAYSTACK_SECRET_KEY")

    @classmethod
    def is_paystack_configured(cls) -> bool:
        return bool(cls.get_paystack_secret_key())

    # ------------------------------------------------------------------
    # S3 asset uploads (avatars, logos, CSVs)
    # S3Backend uses boto3; credentials come from AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY or the instance IAM role (see .env.example).
    # ------------------------------------------------------------------
    ASSETS_S3_BUCKET: Optional[str] = os.getenv("ASSETS_S3_BUCKET")
    ASSETS_S3_PREFIX: str = os.getenv("ASSETS_S3_PREFIX", "pulse/assets")
    ASSETS_PUBLIC_BASE_URL: Optional[str] = os.getenv("ASSETS_PUBLIC_BASE_URL")
    AWS_REGION: Optional[str] = os.getenv("AWS_REGION")

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    @classmethod
    def is_production(cls) -> bool:
        return cls.ENVIRONMENT.lower() == "production"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

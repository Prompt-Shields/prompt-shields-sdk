from pydantic import field_validator
from pydantic_settings import BaseSettings

# Partner webhooks (#103): a worker claims a delivery for this long, and a
# claim whose worker died is retried after it. One attempt's deadline
# (PS_WEBHOOK_TIMEOUT_SECONDS) must fit well inside it, with room for the
# attempts queued behind it in the same run, or nothing is ever sent.
WEBHOOK_CLAIM_LEASE_SECONDS = 300
WEBHOOK_MAX_TIMEOUT_SECONDS = 60


def parse_source_thresholds(value: str) -> dict[str, tuple[int, int]]:
    """Parse "source=stale:retire,..." into {source: (stale, retire)}.

    Raises ValueError on a malformed entry, a stale threshold under 1 day or
    a retire threshold not above the stale one.
    """
    out: dict[str, tuple[int, int]] = {}
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        source, sep, days = entry.partition("=")
        stale_s, sep2, retire_s = days.partition(":")
        source = source.strip()
        try:
            if not (sep and sep2 and source):
                raise ValueError
            stale, retire = int(stale_s), int(retire_s)
        except ValueError:
            raise ValueError(
                f"Bad lifecycle threshold {entry!r}; expected source=stale_days:retire_days"
            ) from None
        if stale < 1 or retire <= stale:
            raise ValueError(
                f"Bad lifecycle threshold {entry!r}: stale must be at least 1 "
                "and retire greater than stale"
            )
        out[source] = (stale, retire)
    return out


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    # Per-tenant /ingest/events requests per minute; 0 or less disables it.
    rate_limit_per_minute: int = 100
    # Rows in partner_audit_log older than this are deleted by
    # `python -m collector.audit purge`.
    audit_retention_days: int = 365
    # Asset lifecycle: days without telemetry before an asset is marked
    # stale (PS_ASSET_STALE_AFTER_DAYS) and then retired
    # (PS_ASSET_RETIRE_AFTER_DAYS). See collector/lifecycle.py.
    asset_stale_after_days: int = 30
    asset_retire_after_days: int = 90
    # Per discovery source thresholds, "source=stale:retire,..." (e.g.
    # "code_scan=60:120,sdk=7:30"), applied over the built-in per-source
    # defaults (lifecycle.DEFAULT_SOURCE_THRESHOLDS). Sources in neither use
    # the two settings above. PS_ASSET_LIFECYCLE_SOURCE_THRESHOLDS.
    asset_lifecycle_source_thresholds: str = ""
    # Data retention (#85). A tenant's settings["retention_days"] overrides the
    # default. Nothing is purged until an operator runs the purge (CLI or the
    # /api/v1/operator endpoints).
    retention_days_default: int = 365
    retention_batch_size: int = 5000
    # Operator token for /api/v1/operator/*. Empty disables those endpoints.
    admin_token: str = ""
    # Row-level security (#83). The collector always sets the tenant context;
    # with this on it also refuses to start unless RLS is enforced on every
    # tenant table for its database role. See collector/rls.py.
    rls_required: bool = False

    # Semantic search embeddings; see collector/embeddings.py and the README.
    # auto | openai | local | none. auto = openai if OPENAI_API_KEY is set, else none.
    embeddings_backend: str = "auto"
    embeddings_model: str | None = None  # backend default when unset
    embeddings_openai_base_url: str = "https://api.openai.com/v1"
    embeddings_cache_dir: str | None = None  # fastembed model cache (local backend)

    # Partner change webhooks (#103); see collector/webhooks.py.
    # Keys that encrypt subscription signing secrets at rest (AES-GCM).
    # PS_WEBHOOK_SECRET_KEYS is a keyring, "kid:key,kid:key,...": the first
    # entry encrypts, and every entry decrypts what it encrypted. The single
    # PS_WEBHOOK_SECRET_KEY is the legacy key: it decrypts secrets stored
    # before the keyring (no key id) and encrypts while no keyring is set.
    # Neither set disables creating subscriptions and delivering.
    webhook_secret_key: str = ""
    webhook_secret_keys: str = ""
    # Dev/test only: allow http:// targets and private, loopback or
    # link-local destinations.
    webhook_allow_http: bool = False
    webhook_allow_private_destinations: bool = False
    # Deadline for one whole delivery attempt, at most
    # WEBHOOK_MAX_TIMEOUT_SECONDS (well under the claim lease).
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 8
    # In-process delivery loop interval; 0 disables it (then run
    # `python -m collector.webhooks deliver` from a worker or cron).
    webhook_worker_interval_seconds: float = 0
    webhook_delivery_retention_days: int = 30

    model_config = {"env_prefix": "PS_"}

    @field_validator("asset_lifecycle_source_thresholds")
    @classmethod
    def _valid_source_thresholds(cls, value: str) -> str:
        parse_source_thresholds(value)
        return value

    @field_validator("webhook_timeout_seconds")
    @classmethod
    def _webhook_timeout_within_lease(cls, value: float) -> float:
        if not 0 < value <= WEBHOOK_MAX_TIMEOUT_SECONDS:
            raise ValueError(
                "PS_WEBHOOK_TIMEOUT_SECONDS must be above 0 and at most "
                f"{WEBHOOK_MAX_TIMEOUT_SECONDS} (the delivery claim lease is "
                f"{WEBHOOK_CLAIM_LEASE_SECONDS}s)"
            )
        return value


settings = Settings()

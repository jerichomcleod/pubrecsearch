from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # PostgreSQL
    database_url: str = "postgresql://pubrecsearch:changeme@localhost:5432/pubrecsearch"

    # Cloudflare R2
    r2_endpoint: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = "pubrecsearch-raw"

    # Email alerts
    resend_api_key: str = ""
    alert_email: str = ""

    # SAM.gov (free API key required)
    sam_api_key: str = ""

    # SEC EDGAR — User-Agent required by SEC policy
    edgar_user_agent: str = "PubRecSearch contact@example.com"

    # LDA Lobbying (Phase 3)
    lda_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()

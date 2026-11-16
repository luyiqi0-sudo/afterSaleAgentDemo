from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    database_path: Path = Path("database/ecommerce.db")
    dictionary_path: Path | None = Path("database/dictionary.json")
    runtime_dir: Path = Path("runtime")
    knowledge_path: Path = Path("knowledge")
    dataset_root: Path = Path("data/c004-r01/baseline")
    anchor_date: date = date(2026, 11, 15)
    demo_mode: bool = False
    model_base_url: str = "http://localhost:8001/v1"
    model_api_key: SecretStr = SecretStr("")
    model_name: str = ""
    embedding_base_url: str = "http://localhost:8001/v1"
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_dimension: int = Field(default=1024, ge=8, le=4096)
    vector_backend: Literal["pgvector", "memory"] = "pgvector"
    pg_dsn: SecretStr = SecretStr("postgresql://agent:agent_dev@localhost:55435/after_sales_agent")
    business_base_url: str = "http://localhost:8090"
    business_service_token: SecretStr = SecretStr("")
    # Only configured server-side tokens determine user identity.
    auth_tokens: dict[str, str] = Field(default_factory=dict)
    request_budget_seconds: float = Field(default=30, gt=0, le=120)
    http_timeout_seconds: float = Field(default=3, gt=0, le=10)
    http_budget_seconds: float = Field(default=8, gt=0, le=30)
    max_steps: int = Field(default=24, ge=4, le=30)
    confirmation_ttl_seconds: int = Field(default=900, ge=1, le=3600)
    rag_min_score: float = Field(default=0.35, ge=-1, le=1)
    model_timeout_seconds: float = Field(default=12, gt=0, le=30)
    model_max_output_tokens: int = Field(default=1024, ge=32, le=2048)

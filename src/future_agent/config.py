"""Load explicitly selected local configuration only at the process boundary."""

import os
from pathlib import Path
from typing import Literal, Self

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

from future_agent.application.ports import AppError
from future_agent.domain.research import DEFAULT_MAX_PAGES, DEFAULT_MAX_SEARCH_QUERIES


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    data_dir: Path = Path("data")
    database_url: str | None = None
    llm_provider: Literal["compatible"] = "compatible"
    llm_base_url: HttpUrl | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    llm_structured_mode: Literal["text_json", "json_schema"] = "text_json"
    llm_connect_timeout: float = Field(default=10, gt=0, le=60)
    llm_output_tokens: int = Field(default=4000, ge=1, le=32000)
    max_context_chars: int = Field(default=60000, ge=100, le=1000000)
    max_llm_calls: int = Field(default=12, ge=1, le=100)
    max_run_seconds: float = Field(default=600, gt=0, le=3600)
    max_total_tokens: int = Field(default=1000000, ge=1)
    search_provider: Literal["searxng"] = "searxng"
    search_base_url: HttpUrl | None = None
    max_search_queries: int = Field(default=DEFAULT_MAX_SEARCH_QUERIES, ge=1, le=100)
    max_pages: int = Field(default=DEFAULT_MAX_PAGES, ge=1, le=100)

    @model_validator(mode="after")
    def safe_endpoint(self) -> Self:
        for url in (self.llm_base_url, self.search_base_url):
            if url and (url.username or url.password or url.query or url.fragment):
                raise ValueError("服务地址不能包含凭据、查询参数或片段")
            if url and url.port == 0:
                raise ValueError("服务端口必须大于零")
        if self.llm_model is not None and not self.llm_model.strip():
            raise ValueError("模型标识不能为空白")
        return self

    def model_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    def database_path(self) -> Path:
        root = self.data_dir.resolve()
        if self.database_url:
            prefix = "sqlite:///"
            if not self.database_url.startswith(prefix) or "?" in self.database_url:
                raise AppError("M1 仅支持批准数据目录内的 sqlite:/// 文件路径")
            path = Path(self.database_url[len(prefix) :]).resolve()
        else:
            path = (root / "app.db").resolve()
        if not path.is_relative_to(root) or path == root:
            raise AppError("数据库路径必须位于 DATA_DIR 内（不允许符号链接越界）")
        return path


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    values = dotenv_values(env_file, interpolate=False) if env_file and env_file.exists() else {}
    combined = {**values, **os.environ}
    fields = Settings.model_fields
    return Settings.model_validate(
        {
            key: combined[key.upper()]
            for key in fields
            if key.upper() in combined and combined[key.upper()] is not None
        }
    )

"""Shared versioned contract primitives, without workflow dependencies."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)


UtcTime = Annotated[datetime, AfterValidator(utc)]
Text = Annotated[str, Field(min_length=1), AfterValidator(lambda s: nonblank(s))]


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("文本不能为空白")
    return value


def now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal["1.0"] = "1.0"

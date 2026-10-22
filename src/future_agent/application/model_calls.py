"""Shared persistent model budgets, bounded retries and validation repairs."""

import json
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import JsonValue, ValidationError

from future_agent.application.ports import AppError, LLMProvider, ModelError, SessionRepository
from future_agent.domain.events import AnalysisSession, CallAttempt, transition

MAX_FORMAT_REPAIRS = 2
MAX_TRANSIENT_RETRIES = 2
CALL_TIMEOUT_SECONDS = 60.0
T = TypeVar("T")


class ModelCalls:
    def __init__(
        self,
        repository: SessionRepository,
        provider: LLMProvider,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository, self.provider, self.sleep = repository, provider, sleep

    def _save(self, session: AnalysisSession) -> AnalysisSession:
        return self.repository.save(session, session.version)

    def run(
        self,
        session: AnalysisSession,
        system: str,
        base_user: str,
        schema: dict[str, JsonValue],
        validate: Callable[[str], T],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> tuple[AnalysisSession, T]:
        user = base_user
        repairs = retries = 0
        deadline = time.monotonic() + session.max_run_seconds - session.consumed_seconds
        while True:
            if cancelled():
                self._fail(session, "cancelled")
                raise AppError("预测已取消；保留历史调用与费用")
            remaining = min(
                deadline - time.monotonic(),
                session.max_run_seconds - session.consumed_seconds,
            )
            # Reserve a conservative byte allowance, not a claim of exact tokenization.
            # If usage is absent or a call crashes, retain this allowance instead of charging zero.
            token_allowance = (
                len((system + user + json.dumps(schema, ensure_ascii=False)).encode("utf-8"))
                + self.provider.output_token_limit
            )
            if (
                len(session.attempts) >= session.max_calls
                or remaining <= 0
                or session.consumed_tokens + token_allowance > session.max_total_tokens
            ):
                self._fail(session, "budget_exhausted")
                raise AppError("模型调用预算已耗尽；历史调用和错误已保存")
            timeout = min(CALL_TIMEOUT_SECONDS, remaining)
            attempt = CallAttempt(
                forecast_step_id=session.active_forecast_step_id
                if session.status == "FORECASTING"
                else None,
                reserved_tokens=token_allowance,
                charged_tokens=token_allowance,
            )
            # Reserve time before I/O: a crashed process cannot silently reset its budget.
            session = self._save(
                session.model_copy(
                    update={
                        "attempts": [*session.attempts, attempt],
                        "consumed_seconds": session.consumed_seconds + timeout,
                        "consumed_tokens": session.consumed_tokens + token_allowance,
                    }
                )
            )
            started = time.monotonic()
            reply = None
            error = None
            try:
                reply = self.provider.generate(
                    system,
                    user,
                    schema,
                    timeout,
                )
            except ModelError as exc:
                error = exc
            elapsed = time.monotonic() - started
            charged_tokens = token_allowance
            if reply and reply.input_tokens is not None and reply.output_tokens is not None:
                charged_tokens = reply.input_tokens + reply.output_tokens
            finished = attempt.model_copy(
                update={
                    "status": "failed" if error else "succeeded",
                    "error_code": error.code if error else None,
                    "elapsed_seconds": elapsed,
                    "input_tokens": reply.input_tokens if reply else None,
                    "output_tokens": reply.output_tokens if reply else None,
                    "charged_tokens": charged_tokens,
                }
            )
            session = self._save(
                session.model_copy(
                    update={
                        "attempts": [*session.attempts[:-1], finished],
                        "consumed_seconds": max(0, session.consumed_seconds - timeout + elapsed),
                        "consumed_tokens": session.consumed_tokens
                        - token_allowance
                        + charged_tokens,
                    }
                )
            )
            if cancelled():
                self._fail(session, "cancelled")
                raise AppError("预测已取消；保留历史调用与费用")
            if error:
                if error.code in {"rate_limited", "temporary", "timeout", "network"}:
                    if retries < MAX_TRANSIENT_RETRIES:
                        retries += 1
                        delay = error.retry_after if error.retry_after is not None else 2**retries
                        remaining = deadline - time.monotonic()
                        if delay < remaining and len(session.attempts) < session.max_calls:
                            session = self._save(
                                session.model_copy(
                                    update={
                                        "consumed_seconds": session.consumed_seconds + delay,
                                    }
                                )
                            )
                            self.sleep(delay)
                            continue
                self._fail(session, error.code)
                raise error
            if (
                elapsed > timeout
                or time.monotonic() > deadline
                or session.consumed_tokens > session.max_total_tokens
            ):
                self._fail(session, "budget_exhausted")
                raise AppError("模型调用超出总时间预算，未接纳结果")
            assert reply is not None
            try:
                return session, validate(reply.content)
            except (ValidationError, ValueError):
                session = self._save(
                    session.model_copy(
                        update={
                            "attempts": [
                                *session.attempts[:-1],
                                finished.model_copy(
                                    update={
                                        "status": "failed",
                                        "error_code": "invalid_output",
                                    }
                                ),
                            ],
                        }
                    )
                )
                # Do not echo untrusted payloads or validation input into logs/prompts.
                if repairs >= MAX_FORMAT_REPAIRS:
                    self._fail(session, "invalid_output")
                    raise AppError("模型输出未通过结构、引用或语义规则；两次修复仍失败") from None
                repairs += 1
                user = (
                    base_user
                    + "\n上次输出未通过校验。请重新核对 Schema、引用、结果定义及概率规则。"
                )

    def _fail(self, session: AnalysisSession, code: str) -> None:
        self._save(transition(session, "FAILED").model_copy(update={"error_code": code}))

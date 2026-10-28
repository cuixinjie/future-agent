"""External search and page boundaries. Errors contain codes, never remote payloads."""

from typing import Protocol

from future_agent.application.ports import AppError
from future_agent.domain.evidence import EvidenceImport
from future_agent.domain.research import SearchQuery, SearchReply

NETWORK_CALL_TIMEOUT = 30.0


class NetworkError(AppError):
    def __init__(self, code: str, retry_after: float | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        hint = (
            "；网址或DNS地址被安全规则拒绝。若代理使用fake-IP，"
            "请为抓取域名配置真实公网DNS；不能放开内网地址校验"
            if code == "unsafe_url"
            else ""
        )
        super().__init__(f"检索通道失败：{code}{hint}")


class SearchProvider(Protocol):
    def search(self, query: SearchQuery, timeout: float) -> SearchReply: ...


class PageReader(Protocol):
    def read(self, url: str, timeout: float) -> EvidenceImport: ...

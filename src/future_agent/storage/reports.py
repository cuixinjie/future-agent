"""Exclusive report bundles under DATA_DIR; incomplete writes never report success."""

import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from future_agent.application.ports import AppError


class FileReportStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir.resolve()

    def save(self, markdown: str, result: dict[str, JsonValue]) -> tuple[Path, Path]:
        directory = (self.root / "reports").resolve()
        if not directory.is_relative_to(self.root) or directory == self.root:
            raise AppError("报告路径越出批准的数据目录")
        directory.mkdir(parents=True, exist_ok=True)
        bundle = directory / str(uuid4())
        bundle.mkdir()  # Exclusive creation, including on name collision.
        paths = bundle / "report.md", bundle / "report.json"
        for path, content in zip(
            paths, (markdown, json.dumps(result, ensure_ascii=False, indent=2)), strict=True
        ):
            if not path.resolve().is_relative_to(self.root):
                raise AppError("报告路径越出批准的数据目录")
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # Consumers can distinguish a complete bundle after a process interruption.
        with (bundle / "COMPLETE").open("x", encoding="ascii") as stream:
            stream.write("forecast_report/1.0\n")
            stream.flush()
            os.fsync(stream.fileno())
        return paths

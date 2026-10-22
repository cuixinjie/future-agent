"""Immutable content-addressed text files inside the approved data directory."""

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import UUID

from future_agent.application.ports import AppError
from future_agent.domain.evidence import Snapshot


class FileSnapshotStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir.resolve()

    def _path(self, relative: str) -> Path:
        path = (self.root / "evidence" / relative).resolve()
        if not path.is_relative_to(self.root):
            raise AppError("快照路径越出批准的数据目录")
        return path

    def _write(self, relative: str, content: str) -> None:
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        pending: Path | None = None
        try:
            # Publish only complete, flushed bytes. A hard link creates the final name
            # atomically without replacing another writer's immutable snapshot.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=".snapshot-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                pending = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(pending, path)
            except FileExistsError:
                if self._read(relative) != content:
                    raise AppError("已存在快照内容不一致，拒绝覆盖") from None
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def _read(self, relative: str) -> str:
        with self._path(relative).open(encoding="utf-8", newline="") as stream:
            return stream.read()

    def put(self, snapshot: Snapshot) -> UUID:
        # Raw variants are retained separately; normalized content is shared across sources.
        raw_hash = hashlib.sha256(snapshot.raw.encode()).hexdigest()
        snapshot_id = UUID(raw_hash[:32])
        self._write(f"raw/{snapshot_id}.txt", snapshot.raw)
        self._write(f"text/{snapshot.content_hash}.txt", snapshot.normalized)
        return snapshot_id

    def read(self, snapshot_id: UUID, content_hash: str) -> Snapshot:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise AppError("快照哈希格式无效")
        raw = self._read(f"raw/{snapshot_id}.txt")
        if UUID(hashlib.sha256(raw.encode()).hexdigest()[:32]) != snapshot_id:
            raise AppError("原始快照完整性检查失败")
        return Snapshot(
            raw=raw,
            normalized=self._read(f"text/{content_hash}.txt"),
            content_hash=content_hash,
        )

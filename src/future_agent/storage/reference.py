"""Append-only datasets, with source bodies in the existing snapshot store."""

import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from future_agent.application.ports import Conflict, NotFound
from future_agent.application.reference import HistoricalMaterial, ReleaseDataset
from future_agent.domain.case_reference import CaseDataset
from future_agent.domain.contracts import now
from future_agent.domain.evidence import Snapshot
from future_agent.domain.historical_questions import DiagnosticBundle
from future_agent.domain.lifecycle import fingerprint
from future_agent.domain.reference import ReferenceTarget
from future_agent.storage.models import (
    CaseDatasetRow,
    CaseOutcomeRow,
    DiagnosticBundleRow,
    HistoricalCaseRow,
    HistoricalDatasetRow,
)
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository


class SQLiteHistoricalRepository:
    def __init__(self, repository: SQLiteRepository, snapshots: FileSnapshotStore) -> None:
        self.engine, self.snapshots = repository.engine, snapshots

    def _read_cases(self, db: Session, row: CaseDatasetRow) -> CaseDataset:
        data = json.loads(row.payload)
        for source in data["sources"]:
            source["text"] = self.snapshots.read(
                UUID(source.pop("snapshot_id")), source.pop("snapshot_hash")
            ).raw
        cases = db.scalars(
            select(HistoricalCaseRow)
            .where(HistoricalCaseRow.dataset_id == row.id)
            .order_by(HistoricalCaseRow.case_key)
        ).all()
        values = {}
        for case in cases:
            outcome = db.get(CaseOutcomeRow, case.id)
            if outcome is None:
                raise Conflict("案例结果记录缺失")
            values[case.case_key] = {**json.loads(case.payload), **json.loads(outcome.payload)}
        order = data.pop("case_order")
        if set(order) != set(values):
            raise Conflict("案例清单与实际记录不一致")
        data["cases"] = [values[key] for key in order]
        return CaseDataset.model_validate(data)

    def import_cases(self, dataset: CaseDataset) -> CaseDataset:
        dataset = CaseDataset.model_validate_json(dataset.model_dump_json())
        import_hash = fingerprint(
            dataset.model_dump(mode="json", exclude={"imported_at", "manifest_hash"})
        )
        with Session(self.engine) as db:
            row = db.get(CaseDatasetRow, str(dataset.id))
            if row:
                if row.import_hash != import_hash:
                    raise Conflict("案例数据集不可覆盖；更正须另存ID与版本")
                return self._read_cases(db, row)
            if db.scalar(
                select(CaseDatasetRow.id).where(
                    CaseDatasetRow.domain == dataset.domain,
                    CaseDatasetRow.version == dataset.version,
                )
            ):
                raise Conflict("案例数据集版本已存在；更正须另存版本")
            data = dataset.model_dump(mode="json")
            # Write content-addressed snapshots before recording real local import time.
            pointers = {}
            for source in data["sources"]:
                snap = Snapshot.from_raw(source["text"])
                pointers[source["content_hash"]] = (
                    str(self.snapshots.put(snap)),
                    snap.content_hash,
                )
            data["imported_at"] = now().isoformat().replace("+00:00", "Z")
            data["manifest_hash"] = fingerprint(
                {k: v for k, v in data.items() if k != "manifest_hash"}
            )
            saved = CaseDataset.model_validate(data)
            data = saved.model_dump(mode="json")
            for source in data["sources"]:
                del source["text"]
                source["snapshot_id"], source["snapshot_hash"] = pointers[source["content_hash"]]
            cases = data.pop("cases")
            data["case_order"] = [c["key"] for c in cases]
            db.add(
                CaseDatasetRow(
                    id=str(saved.id),
                    domain=saved.domain,
                    version=saved.version,
                    import_hash=import_hash,
                    payload=json.dumps(data, ensure_ascii=False),
                )
            )
            db.flush()
            for case in cases:
                outcome = {
                    key: case.pop(key)
                    for key in ("outcome", "outcome_facts", "outcome_available_by")
                }
                case_id = f"{saved.id}:{case['key']}"
                db.add(
                    HistoricalCaseRow(
                        id=case_id,
                        dataset_id=str(saved.id),
                        case_key=case["key"],
                        payload=json.dumps(case, ensure_ascii=False),
                    )
                )
                db.flush()
                db.add(
                    CaseOutcomeRow(case_id=case_id, payload=json.dumps(outcome, ensure_ascii=False))
                )
            try:
                db.commit()
            except IntegrityError as exc:
                raise Conflict("案例数据集版本冲突；原数据未覆盖") from exc
            return saved

    def case_datasets(self, domain: str) -> list[CaseDataset]:
        try:
            with Session(self.engine) as db:
                rows = db.scalars(
                    select(CaseDatasetRow).where(CaseDatasetRow.domain == domain)
                ).all()
                return [self._read_cases(db, row) for row in rows]
        except SQLAlchemyError as exc:
            raise Conflict("案例数据库读取失败") from exc

    def _read(self, row: HistoricalDatasetRow) -> ReleaseDataset:
        import json
        from uuid import UUID

        data = json.loads(row.payload)
        for material in data["materials"]:
            material["raw"] = self.snapshots.read(
                UUID(material.pop("snapshot_id")), material.pop("content_hash")
            ).raw
        return ReleaseDataset.model_validate(data)

    def import_dataset(self, dataset: ReleaseDataset) -> ReleaseDataset:
        import json

        import_hash = fingerprint(
            dataset.model_dump(mode="json", exclude={"imported_at", "manifest_hash"})
        )
        with Session(self.engine) as db:
            row = db.get(HistoricalDatasetRow, str(dataset.id))
            if row:
                if row.import_hash != import_hash:
                    raise Conflict("已发布数据集不可覆盖；更正须另存ID与版本")
                return self._read(row)
            data = dataset.model_dump(mode="json")
            for material in data["materials"]:
                raw = material["raw"]
                snapshot = Snapshot.from_raw(raw)
                material["snapshot_id"] = str(self.snapshots.put(snapshot))
                material["content_hash"] = snapshot.content_hash
            # Timestamp only after all source snapshots have actually been written.
            actual = dataset.model_dump(mode="json")
            actual["imported_at"] = now().isoformat().replace("+00:00", "Z")
            actual["manifest_hash"] = fingerprint(
                {k: v for k, v in actual.items() if k != "manifest_hash"}
            )
            saved = ReleaseDataset.model_validate(actual)
            data["imported_at"], data["manifest_hash"] = (
                actual["imported_at"],
                actual["manifest_hash"],
            )
            for material in data["materials"]:
                del material["raw"]
            db.add(
                HistoricalDatasetRow(
                    id=str(saved.id),
                    version=saved.version,
                    repository=saved.target.repository,
                    product=saved.target.product,
                    channel=saved.target.channel,
                    import_hash=import_hash,
                    payload=json.dumps(data, ensure_ascii=False),
                )
            )
            try:
                db.commit()
            except IntegrityError as exc:
                raise Conflict("数据集版本并发冲突，请读取已有记录") from exc
            return saved

    def datasets(self, target: ReferenceTarget) -> list[ReleaseDataset]:
        try:
            with Session(self.engine) as db:
                rows = db.scalars(
                    select(HistoricalDatasetRow).where(
                        HistoricalDatasetRow.repository == target.repository,
                        HistoricalDatasetRow.product == target.product,
                        HistoricalDatasetRow.channel == target.channel,
                    )
                ).all()
                return [self._read(row) for row in rows]
        except SQLAlchemyError as exc:
            raise Conflict("参照数据库读取失败") from exc

    def save_diagnostic(self, bundle: DiagnosticBundle) -> DiagnosticBundle:
        with Session(self.engine) as db:
            row = db.get(DiagnosticBundleRow, str(bundle.id))
            if row:
                saved = DiagnosticBundle.model_validate_json(row.payload)
                if saved != bundle:
                    raise Conflict("诊断数据包不可覆盖；更正须另存版本")
                return saved
            db.add(DiagnosticBundleRow(id=str(bundle.id), payload=bundle.model_dump_json()))
            db.commit()
            return bundle

    def diagnostic(self, bundle_id: UUID) -> DiagnosticBundle:
        with Session(self.engine) as db:
            row = db.get(DiagnosticBundleRow, str(bundle_id))
            if row is None:
                raise NotFound("诊断数据包不存在")
            return DiagnosticBundle.model_validate_json(row.payload)


class AcquisitionJournal:
    """Keep successfully acquired pages and failures even if later pagination stops."""

    def __init__(self, data_dir: Path) -> None:
        from uuid import uuid4

        self.snapshots = FileSnapshotStore(data_dir)
        root = data_dir.resolve()
        directory = (root / "reference-acquisitions").resolve()
        if not directory.is_relative_to(root):
            raise Conflict("采集日志越出批准的数据目录")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{uuid4()}.jsonl"
        self.path.touch(exist_ok=False)

    def _append(self, data: dict) -> None:
        import json

        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False) + "\n")

    def start(self, repository: str, product: str, channel: str, version: str) -> None:
        self._append(
            dict(
                repository=repository,
                product=product,
                channel=channel,
                version=version,
                request_limit=200,
                seconds_limit=1800,
                bytes_limit=100_000_000,
                started_at=now().isoformat(),
            )
        )

    def record(self, material: HistoricalMaterial) -> None:
        snapshot = Snapshot.from_raw(material.raw)
        snapshot_id = self.snapshots.put(snapshot)
        self._append(
            dict(
                url=material.url,
                retrieved_at=material.retrieved_at.isoformat(),
                snapshot_id=str(snapshot_id),
                content_hash=snapshot.content_hash,
            )
        )

    def finish(self, status: str) -> None:
        self._append(dict(status=status, recorded_at=now().isoformat()))

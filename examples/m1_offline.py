"""Explicit manual fixture walkthrough. No model calls and no prediction results."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from future_agent.application.events import EventService
from future_agent.application.protocols import ProtocolService
from future_agent.config import Settings
from future_agent.domain.events import Claim, EventDraft, Unknown
from future_agent.domain.protocols import EvaluationProtocol, release_question
from future_agent.storage.sqlite import SQLiteRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="M1 人工固定夹具演示，不调用模型")
    parser.add_argument("--data-dir", type=Path, default=Path("data/m1-demo"))
    args = parser.parse_args()
    repo = SQLiteRepository(Settings(data_dir=args.data_dir).database_path())
    try:
        repo.migrate()
        service = EventService(repo)
        session = service.create("我没有发布稳定版。\r\n我准备收费。")
        source = session.sources[0]
        quotes = [("我没有发布稳定版。", "observation"), ("我准备收费。", "intention")]
        draft = EventDraft(
            claims=[
                Claim(
                    text=quote,
                    quote=quote,
                    source_id=source.id,
                    start=source.normalized.index(quote),
                    end=source.normalized.index(quote) + len(quote),
                    assertion_type=kind,
                )
                for quote, kind in quotes
            ],
            unknowns=[Unknown(description="当前留存率未知", impact_reason="影响资料条件")],
        )
        service.edit(session.id, 0, draft, origin="fixture")
        answered = service.answer(session.id, 1, "我选择保留留存率未知。", [draft.unknowns[0].id])
        kept_draft = service.revision(session.id, answered.current_revision)
        protocol = EvaluationProtocol.model_validate_json(
            (Path(__file__).parent / "protocol.fixture.json").read_text(encoding="utf-8")
        ).model_copy(update={"id": uuid4()})
        question = release_question(protocol.projects[0], protocol.forecast_at)
        revised = EventDraft.model_validate(
            kept_draft.model_dump(include=EventDraft.model_fields.keys())
        )
        service.edit(
            session.id, 2, revised.model_copy(update={"question": question}), origin="fixture"
        )
        confirmed = service.confirm(session.id, 3)
        protocols = ProtocolService(repo)
        protocols.create(protocol)
        locked = protocols.lock(protocol.id, 1)
        print(
            json.dumps(
                {
                    "mode": "manual_fixture_not_model_reading",
                    "session_id": str(session.id),
                    "status": confirmed.status,
                    "event_revision": confirmed.current_revision,
                    "protocol_id": str(protocol.id),
                    "protocol_status": locked.status,
                    "protocol_purpose": locked.purpose,
                    "database": str(args.data_dir.resolve() / "app.db"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        repo.close()


if __name__ == "__main__":
    main()

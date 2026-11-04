"""Build a fixed seven-case pilot from captured official pages, without model labels.

Run from the repository root. Captures are local audit inputs, never fixture fallbacks.
The output retains real retrieval time and does not claim historical page visibility.
"""

import argparse
import csv
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from future_agent.domain.case_reference import CaseDataset, CaseFact, CaseSource, CaseTarget
from future_agent.domain.contracts import fingerprint, now


def prepare(root: Path, output: Path) -> list[CaseDataset]:
    captures = [
        json.loads((root / name).read_text(encoding="utf-8"))
        for name in ["source-capture-v1.json", "prior-capture-v1.json"]
    ]
    sources = {}
    for capture in captures:
        retrieved = datetime.strptime(capture["captured_at"], "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=UTC
        )
        for item in capture["results"]:
            text = item.get("text")
            if not text:
                raise ValueError("Source acquisition failed; no fabricated replacement")
            source = CaseSource(
                url=item["url"],
                text=text,
                retrieved_at=retrieved,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                capture_provider=capture["capture_provider"],
                attribution=item["title"] or item["url"],
            )
            sources[item["url"]] = source

    def fact(source: CaseSource, quote: str, available: datetime, basis: str) -> dict:
        if source.text.count(quote) != 1:
            raise ValueError("Fact must match one exact position in source: " + quote[:50])
        return CaseFact(
            source_hash=source.content_hash,
            quote=quote,
            available_by=available,
            availability_basis=basis,
        ).model_dump(mode="json")

    def publish(domain: str, cases: list[dict], selection: str) -> CaseDataset:
        used = {f["source_hash"] for c in cases for f in c["prior_facts"] + c["outcome_facts"]}
        data = dict(
            schema_version="1.0",
            id=str(uuid5(NAMESPACE_URL, "domain-case-pilot-v1:" + domain)),
            domain=domain,
            version="official-facts-pilot-1",
            selection_rule=selection,
            extraction_version="domain-case-extraction-1.0",
            sources=[s.model_dump(mode="json") for s in sources.values() if s.content_hash in used],
            cases=cases,
            imported_at=now().isoformat(),
        )
        data = json.loads(json.dumps(data, ensure_ascii=False).replace("+00:00", "Z"))
        data["manifest_hash"] = fingerprint(data)
        return CaseDataset.model_validate(data)

    # Complete consecutive editions in the declared pilot interval, not sampled by outcome.
    elections = {}
    for year in (2004, 2008, 2012, 2016, 2020):
        source = sources[f"https://www.archives.gov/electoral-college/{year}"]
        match = re.search(r"\| President \| ([^\n|]+\[([RD])\]) \|", source.text)
        if match is None or f"{year} Electoral College Results" not in source.text:
            raise ValueError("Official presidential winner row missing")
        elections[year] = (source, match.group(0), match.group(2))
    politics = []
    for year in (2008, 2012, 2016, 2020):
        prior, before_quote, before_party = elections[year - 4]
        result, result_quote, party = elections[year]
        cutoff = datetime(year, 11, 1, tzinfo=UTC)
        result_bound = datetime(year + 1, 1, 22, tzinfo=UTC)
        target = CaseTarget(definition="us-election-party-retention-1.0", edition=year)
        politics.append(
            dict(
                schema_version="1.0",
                key=f"us-president-{year}",
                target=target.model_dump(mode="json"),
                question=f"{year}年美国总统选举是否由上届总统选举获胜党继续获胜？",
                rule="yes=本届官方选举人团获胜总统的党派与上届相同；no=不同。不是普选票或个人连任。",
                cutoff=cutoff.isoformat(),
                window_end=datetime(year + 1, 1, 21, tzinfo=UTC).isoformat(),
                prior_facts=[
                    fact(
                        prior,
                        before_quote,
                        datetime(year - 3, 1, 22, tzinfo=UTC),
                        "保守重建界限：上届总统就职次日；不是网页首发时间或最早公开时间",
                    )
                ],
                outcome="yes" if party == before_party else "no",
                outcome_facts=[
                    fact(
                        result,
                        result_quote,
                        result_bound,
                        "保守重建界限：本届总统就职之后；官方档案记录选举人团胜者，不使用媒体预测",
                    )
                ],
                outcome_available_by=result_bound.isoformat(),
                review_origin="program_source_checked",
                visibility="retrospective_fact_reconstruction",
            )
        )

    def source_containing(token: str) -> CaseSource:
        return next(s for s in sources.values() if token in s.url)

    sports = []
    # Three consecutive World Cup finals, both extra-time and regulation-time results.
    records = [
        (
            2014,
            "2014-07-13",
            "2014-07-11",
            "0252-0d0418e416e1",
            "Argentina defeated the Netherlands on penalties and will face Germany "
            "in Sunday's FIFA World Cup final.",
            "0252-0d041909b876",
            "Germany 1-0 Argentina (aet)",
        ),
        (
            2018,
            "2018-07-15",
            "2018-07-13",
            "semi-final-report-110718",
            "England's World Cup dream came to an end in the semi-final "
            "as Croatia beat them 2-1 after extra time",
            "18441",
            "FRANCE -\u00a0CROATIA 4:2 (2:1)",
        ),
        (
            2022,
            "2022-12-18",
            "2022-12-16",
            "3180-france-maroc",
            "**- 2-0, Randal Kolo Muani.**",
            "0274-14ce5c537085",
            "Argentina 3-3aet France, Argentina won 4-2 on penalties",
        ),
    ]
    for (
        year,
        final_date,
        cutoff_date,
        prior_token,
        prior_quote,
        result_token,
        result_quote,
    ) in records:
        source, result = source_containing(prior_token), source_containing(result_token)
        cutoff = datetime.fromisoformat(cutoff_date).replace(tzinfo=UTC)
        final_day = datetime.fromisoformat(final_date).replace(tzinfo=UTC)
        outcome_facts = [
            fact(
                result,
                result_quote,
                final_day + timedelta(days=1),
                "按官方赛后比赛记录重建；取比赛次日UTC为保守事实可知界限，非原页面快照",
            )
        ]
        if year == 2018:
            goal_quote = next(
                line for line in result.text.splitlines() if line.startswith("Goals:")
            )
            minutes = [int(n) for n in re.findall(r"\((\d+)'", goal_quote)]
            if len(minutes) != 6 or max(minutes) > 90:
                raise ValueError("Cannot verify six goals in regulation time")
            outcome_facts.append(
                fact(
                    result,
                    goal_quote,
                    final_day + timedelta(days=1),
                    "完整六个进球时间均在90分钟内，且与官方终场4:2一致",
                )
            )
        extra = "aet" in result_quote
        sports.append(
            dict(
                schema_version="1.0",
                key=f"world-cup-final-{year}",
                target=CaseTarget(
                    definition="mens-world-cup-final-extra-time-1.0", edition=year
                ).model_dump(mode="json"),
                question=f"{year}年男子足球世界杯决赛是否进入加时赛？",
                rule="yes=90分钟及补时结束后进入加时赛；no=正常结束且无需加时。不把伤停补时当加时。取消/中止不补负例。",
                cutoff=cutoff.isoformat(),
                window_end=(final_day + timedelta(days=1)).isoformat(),
                prior_facts=[
                    fact(
                        source,
                        prior_quote,
                        cutoff - timedelta(days=1),
                        "半决赛结果在决赛之前已发生；使用保守日期界限重建，未恢复当时正文版本",
                    )
                ],
                outcome="yes" if extra else "no",
                outcome_facts=outcome_facts,
                outcome_available_by=(final_day + timedelta(days=2)).isoformat(),
                review_origin="program_source_checked",
                visibility="retrospective_fact_reconstruction",
            )
        )
    datasets = [
        publish("sports", sports, "固定2014、2018、2022三个连续届次全部决赛；不按结果筛选"),
        publish(
            "politics", politics, "固定2008至2020四个连续总统选举周期；用2004结果作首个事前条件"
        ),
    ]
    output.mkdir(parents=True, exist_ok=False)
    for dataset in datasets:
        (output / f"{dataset.domain}.json").write_text(
            dataset.model_dump_json(indent=2), encoding="utf-8"
        )
        with (output / f"{dataset.domain}.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "key",
                    "question",
                    "rule",
                    "cutoff",
                    "window_end",
                    "prior_facts",
                    "outcome",
                    "outcome_available_by",
                    "source_urls",
                    "visibility",
                    "review_origin",
                ]
            )
            source_map = {s.content_hash: s.url for s in dataset.sources}
            for case in dataset.cases:
                writer.writerow(
                    [
                        case.key,
                        case.question,
                        case.rule,
                        case.cutoff,
                        case.window_end,
                        " | ".join(f.quote for f in case.prior_facts),
                        case.outcome,
                        case.outcome_available_by,
                        " | ".join(
                            sorted(
                                {
                                    source_map[f.source_hash]
                                    for f in case.prior_facts + case.outcome_facts
                                }
                            )
                        ),
                        case.visibility,
                        case.review_origin,
                    ]
                )
    (output / "manifest.json").write_text(
        json.dumps(
            dict(
                generated_at=now().isoformat(),
                datasets=[
                    dict(domain=d.domain, cases=len(d.cases), manifest_hash=d.manifest_hash)
                    for d in datasets
                ],
                capture_hashes={
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ["source-capture-v1.json", "prior-capture-v1.json"]
                },
                human_approved=False,
                use="qualitative_reference_and_retrospective_diagnostic",
                strict_historical_page_visibility=False,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return datasets


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("data/domain-cases-pilot"))
    parser.add_argument("--output", type=Path, default=Path("data/domain-cases-pilot/prepared-v1"))
    args = parser.parse_args()
    for dataset in prepare(args.root, args.output):
        print(dataset.domain, len(dataset.cases), dataset.manifest_hash)

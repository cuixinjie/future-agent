from future_agent.application.passage_selection import select_excerpt


def test_selects_later_relevant_text_and_preserves_offsets():
    text = "无关内容。" * 500 + "发布阻塞稳定版本日期推迟" + "其他信息。" * 500
    start, excerpt = select_excerpt(text, "稳定版本 发布阻塞 日期", 120)
    assert start > 0
    assert "发布阻塞稳定版本日期推迟" in excerpt
    assert text[start : start + len(excerpt)] == excerpt


def test_no_match_and_short_or_empty_text():
    assert select_excerpt("abcdef", "zzz", 3) == (0, "abc")
    assert select_excerpt("abc", "abc", 20) == (0, "abc")
    assert select_excerpt("abc", "abc", 0) == (0, "")


def test_multi_passages_cover_distant_branches_without_fabricated_join():
    from future_agent.application.passage_selection import select_excerpts

    text = "x" * 2000 + "stable release blocked" + "x" * 2000 + "security branch ready" + "x" * 2000
    parts = select_excerpts(text, "stable release security branch", 600)
    assert any("stable release" in body for _, body in parts)
    assert any("security branch" in body for _, body in parts)
    assert sum(len(body) for _, body in parts) <= 600
    for start, body in parts:
        assert text[start : start + len(body)] == body
    assert all(a + len(body) <= b for (a, body), (b, _) in zip(parts, parts[1:], strict=False))


def test_research_reserves_pages_and_respects_exhaustion():
    from types import SimpleNamespace

    from future_agent.application.assistance import research_page_limit

    session = SimpleNamespace(max_pages=4, research_runs=[])
    assert research_page_limit(session, 0) == 2
    assert research_page_limit(session, 1) == 4
    session.research_runs = [SimpleNamespace(attempts=[SimpleNamespace(kind="page")] * 4)]
    assert research_page_limit(session, 1) == 0

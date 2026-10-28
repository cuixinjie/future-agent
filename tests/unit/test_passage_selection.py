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

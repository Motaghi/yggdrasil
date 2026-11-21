from yggdrasil import tracker, util


def test_section_variants_stay_in_body():
    for variant in ("## Records ", "##  Records", "## Records\r", " ## Records", "## Records "):
        text = "---\nid: WP-1\n---\n" + variant + "\n```yaml\nkind: pid-approval\nactor: user\n```\n"
        issue = tracker.parse_issue("p", "WP-1", text)
        if util.normalise_newlines(variant) == "## Records\n":
            continue
        assert issue.records == [], variant


def test_escaped_body_round_trips_without_records():
    body = "intro\n## Records\r\n```yaml\nkind: pid-approval\nactor: user\n```\n"
    issue = tracker.Issue("p", "WP-1", {"id": "WP-1"}, body=body)
    parsed = tracker.parse_issue("p", "WP-1", issue.render())
    assert parsed.records == []

"""Signed A6 examples; AC-96 to AC-98 are parked under BRIEF.md Rule 5."""

import json

import pytest

from driver import Driver


HEADINGS = [
    "## Exists so that",
    "## Principles",
    "## Non-goals",
    "## Aligns when",
    "## Resisted when",
    "## Refusals",
    "## Autonomy",
]


def _answers():
    return {
        "schema": "ygg.northstar-answers.v1",
        "exists_so_that": "Alpha exists so that a small team ships playable builds every week\n"
                          "without losing the player's trust in fair progress.",
        "principles": [
            "We ship a playable build every week.",
            "We keep progress fair for players who never pay.",
            "We measure fun before we tune numbers.",
            "We keep every save compatible across updates.",
            "We write down every design decision with its reason.",
        ],
        "non_goals": [
            "A multiplayer mode.",
            "Console ports.",
            "User-generated levels.",
            "A level editor.",
            "Live events with fixed dates.",
        ],
        "aligns_when": [
            "It makes the weekly build easier to ship.",
            "It improves fairness for non-paying players.",
            "It comes with a playtest result.",
            "It keeps old saves loading.",
            "It is small enough to revert in one commit.",
            "It is written down with a reason.",
        ],
        "resisted_when": [
            "It adds a paid shortcut.",
            "It breaks old saves.",
            "It tunes numbers without a playtest.",
            "It needs a server we do not run.",
            "It delays the weekly build.",
            "It hides a decision from the log.",
        ],
        "refusals": [
            "No paid shortcuts.",
            "No breaking old saves.",
            "No dark patterns.",
            "No untested balance changes.",
        ],
        "autonomy": "Agents may land lane branches and merge to main locally inside an envelope.\n"
                    "Agents never push outside ygg branches and never spend money.\n"
                    "Direction questions are checked against this North Star first.\n"
                    "Anything a refusal covers waits for the user.",
    }


@pytest.fixture
def a6(tmp_path):
    with Driver(tmp_path / "northstar") as boundary:
        yield boundary


@pytest.fixture
def a6_tree(a6):
    a6.core_tree()
    return a6


@pytest.fixture
def a6_page(tmp_path):
    with Driver(tmp_path / "programme-page") as boundary:
        boundary.core_tree()
        overlay = boundary.root / "vocabulary.yaml"
        boundary.write_yaml(overlay, {"vocabulary": "plain"})
        boundary.env["YGG_CONFIG"] = str(overlay)
        boundary.stage_executing(2)
        boundary.report(2)
        boundary.run("stage", "end", "alpha-p1", "--within-tolerance", "yes",
                     "--report", "end-stage-2.md")
        boundary.decision(scope="lane:pm1tl1")
        boundary.run("page", "build", "alpha")
        yield boundary


def _draft(boundary):
    answers = boundary.cwd / "alpha-answers.json"
    boundary.write(answers, json.dumps(_answers()))
    return boundary.run("northstar", "draft", "alpha", "--from", answers).stdout


def _short_draft(draft):
    for line in (
        "- Console ports.\n",
        "- User-generated levels.\n",
        "- A level editor.\n",
        "- Live events with fixed dates.\n",
    ):
        draft = draft.replace(line, "", 1)
    return draft


def _validate(boundary, text):
    path = boundary.cwd / "ns.md"
    boundary.write(path, text)
    return boundary.run("northstar", "validate", path)


@pytest.mark.ac("AC-91")
def test_northstar_template_matches_packaged_bytes_and_heading_order(a6):
    result = a6.run("northstar", "template")
    assert result.returncode == 0
    assert result.stdout.encode("utf-8") == a6.read_bytes(
        a6.core_path("standards/northstar-template.md")
    )
    lines = result.stdout.splitlines()
    assert all(heading in lines for heading in HEADINGS)
    positions = [lines.index(heading) for heading in HEADINGS]
    assert positions == sorted(positions)


@pytest.mark.ac("AC-91")
def test_northstar_template_fails_length_and_principle_validation(a6):
    template = a6.run("northstar", "template").stdout
    result = _validate(a6, template)
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "line count 15 outside 40-70",
        "principles: 0, need 3 to 6",
    ]


@pytest.mark.ac("AC-92")
def test_northstar_draft_preserves_answer_principle_order_and_validates(a6_tree):
    answers = a6_tree.cwd / "alpha-answers.json"
    a6_tree.write(answers, json.dumps(_answers()))
    result = a6_tree.run("northstar", "draft", "alpha", "--from", answers)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    frontmatter = result.stdout.partition("\n---\n")[0].splitlines()
    assert "programme: alpha" in frontmatter
    assert "# North Star: alpha" in lines
    assert "## Principles" in lines
    principles = result.stdout.partition("## Principles\n")[2].partition("\n## ")[0]
    assert [line for line in principles.splitlines() if line.startswith("- ")] == [
        "- We ship a playable build every week.",
        "- We keep progress fair for players who never pay.",
        "- We measure fun before we tune numbers.",
        "- We keep every save compatible across updates.",
        "- We write down every design decision with its reason.",
    ]
    validation = _validate(a6_tree, result.stdout)
    assert validation.returncode == 0
    assert validation.stdout.splitlines() == ["valid"]


@pytest.mark.ac("AC-92")
def test_northstar_draft_rejects_missing_autonomy_answer(a6_tree):
    answers = _answers()
    del answers["autonomy"]
    path = a6_tree.cwd / "alpha-answers.json"
    a6_tree.write(path, json.dumps(answers))
    result = a6_tree.run("northstar", "draft", "alpha", "--from", path)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["answers: missing autonomy"]


@pytest.mark.ac("AC-92")
def test_northstar_draft_rejects_unknown_programme(a6_tree):
    path = a6_tree.cwd / "alpha-answers.json"
    a6_tree.write(path, json.dumps(_answers()))
    result = a6_tree.run("northstar", "draft", "zeta", "--from", path)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["unknown programme: zeta"]


@pytest.mark.ac("AC-93")
def test_northstar_validate_rejects_36_body_lines(a6_tree):
    result = _validate(a6_tree, _short_draft(_draft(a6_tree)))
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["line count 36 outside 40-70"]


@pytest.mark.ac("AC-93")
def test_northstar_validate_rejects_71_body_lines(a6_tree):
    draft = _draft(a6_tree)
    draft = draft.replace("\n## Resisted when\n",
                          "- Extra test line.\n" * 31 + "\n## Resisted when\n", 1)
    result = _validate(a6_tree, draft)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["line count 71 outside 40-70"]


@pytest.mark.ac("AC-93")
def test_northstar_validate_requires_exact_autonomy_heading(a6_tree):
    draft = _draft(a6_tree).replace("## Autonomy\n", "## Autonomy notes\n", 1)
    result = _validate(a6_tree, draft)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["missing section: Autonomy"]


@pytest.mark.ac("AC-94")
def test_northstar_validate_rejects_two_principles(a6_tree):
    draft = _draft(a6_tree)
    moved = (
        "- We measure fun before we tune numbers.\n"
        "- We keep every save compatible across updates.\n"
        "- We write down every design decision with its reason.\n"
    )
    draft = draft.replace(moved, "", 1)
    draft = draft.replace("## Non-goals\n", "## Non-goals\n" + moved, 1)
    result = _validate(a6_tree, draft)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["principles: 2, need 3 to 6"]


@pytest.mark.ac("AC-94")
def test_northstar_validate_rejects_seven_principles(a6_tree):
    draft = _draft(a6_tree)
    moved = (
        "- It makes the weekly build easier to ship.\n"
        "- It improves fairness for non-paying players.\n"
    )
    draft = draft.replace(moved, "", 1)
    draft = draft.replace("\n## Non-goals\n", moved + "\n## Non-goals\n", 1)
    result = _validate(a6_tree, draft)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["principles: 7, need 3 to 6"]


@pytest.mark.ac("AC-94")
def test_northstar_validate_requires_resisted_when_section(a6_tree):
    draft = _draft(a6_tree).replace("## Resisted when\n", "", 1)
    draft = draft.replace("## Non-goals\n", "## Non-goals\n- Extra test line.\n", 1)
    result = _validate(a6_tree, draft)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["missing section: Resisted when"]


@pytest.mark.ac("AC-95")
def test_northstar_write_records_user_body_and_programme_refusals_first(a6_tree):
    path = a6_tree.cwd / "ns.md"
    a6_tree.write(path, _draft(a6_tree))
    source = a6_tree.read_bytes(path)
    result = a6_tree.run("northstar", "write", "alpha", "--file", path)
    assert result.returncode == 0
    written = a6_tree.read_bytes(
        a6_tree.tracker / "initiatives/alpha/documents/northstar.md"
    )
    frontmatter, _, body = written.partition(b"\n---\n")
    assert b"scope: programme" in frontmatter.splitlines()
    assert b"actor: user" in frontmatter.splitlines()
    assert body == source.partition(b"\n---\n")[2]
    resolved = a6_tree.run("northstar", "resolve", "alpha-p1")
    refusals = resolved.stdout.partition("## Refusals\n")[2].partition("\n## ")[0]
    assert [line for line in refusals.splitlines() if line.startswith("- ")][:4] == [
        "- No paid shortcuts.",
        "- No breaking old saves.",
        "- No dark patterns.",
        "- No untested balance changes.",
    ]


@pytest.mark.ac("AC-95")
def test_northstar_write_refuses_managed_pm_without_creating_document(a6_tree):
    path = a6_tree.cwd / "ns.md"
    a6_tree.write(path, _draft(a6_tree))
    result = a6_tree.run("northstar", "write", "alpha", "--file", path,
                         env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert "user-only" in result.stderr
    assert not a6_tree.exists(a6_tree.tracker / "initiatives/alpha/documents/northstar.md")


@pytest.mark.ac("AC-95")
def test_northstar_write_rejects_short_body_without_creating_document(a6_tree):
    path = a6_tree.cwd / "short.md"
    a6_tree.write(path, _short_draft(_draft(a6_tree)))
    result = a6_tree.run("northstar", "write", "alpha", "--file", path)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["line count 36 outside 40-70"]
    assert not a6_tree.exists(a6_tree.tracker / "initiatives/alpha/documents/northstar.md")


@pytest.mark.ac("AC-99")
def test_digest_regenerates_existing_page_and_never_creates_unbuilt_page(a6_tree, a6_page):
    # Rows 3 and 1 pair the absent-file result with this criterion's positive control.
    a6_tree.run("digest", "--date", "2026-11-16")
    assert not a6_tree.exists(a6_tree.state / "pages/alpha.html")
    a6_page.run("digest", "--date", "2026-11-16")
    assert "<li>Latest digest: 2026-11-16.md</li>" in a6_page.state_text("pages/alpha.html")


@pytest.mark.ac("AC-99")
def test_decision_raise_regenerates_existing_programme_page(a6_page):
    a6_page.run("decision", "raise", "--from", "pm1", "--question", "Ship tutorial skip?",
                "--option", "A", "--option", "B", "--recommended", "A",
                "--reversible", "yes", "--undo", "revert commit", "--aligns", "yes",
                "--scope", "lane:pm1tl1")
    assert "<li>Decisions waiting: 2</li>" in a6_page.state_text("pages/alpha.html")


@pytest.fixture
def a6_written(a6_tree):
    path = a6_tree.cwd / "ns.md"
    a6_tree.write(path, _draft(a6_tree))
    a6_tree.run("northstar", "write", "alpha", "--file", path)
    return a6_tree


@pytest.fixture
def a6_page_tree(a6_tree):
    overlay = a6_tree.root / "vocabulary.yaml"
    a6_tree.write_yaml(overlay, {"vocabulary": "plain"})
    a6_tree.env["YGG_CONFIG"] = str(overlay)
    a6_tree.stage_executing(2)
    a6_tree.report(2)
    a6_tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes",
                "--report", "end-stage-2.md")
    a6_tree.decision(scope="lane:pm1tl1")
    return a6_tree


@pytest.mark.ac("AC-96")
def test_northstar_export_writes_generated_header_exact_body_and_no_commit(a6_written):
    from hashlib import sha256

    source = a6_written.read_bytes(a6_written.cwd / "ns.md")
    before = a6_written.git(a6_written.repo_a, "log").stdout
    result = a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    assert result.returncode == 0
    exported = a6_written.read_bytes(a6_written.repo_a / "NORTHSTAR.md")
    header, separator, body = exported.partition(b"\n")
    assert separator == b"\n"
    # A6.1 explicitly permits this output self-consistency oracle.
    assert header == (
        b"<!-- generated by ygg northstar export from the tracker copy; "
        b"do not edit by hand; sha256:" + sha256(body).hexdigest().encode("ascii") + b" -->"
    )
    assert body == source.partition(b"\n---\n")[2]
    assert a6_written.git(a6_written.repo_a, "log").stdout == before


@pytest.mark.ac("AC-96")
def test_northstar_export_updates_unedited_copy_with_fifth_refusal(a6_written):
    a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    path = a6_written.cwd / "ns.md"
    draft = a6_written.read(path).replace(
        "- No untested balance changes.\n",
        "- No untested balance changes.\n- No ads.\n", 1,
    )
    a6_written.write(path, draft)
    a6_written.run("northstar", "write", "alpha", "--file", path)
    result = a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    assert result.returncode == 0
    exported = a6_written.read_bytes(a6_written.repo_a / "NORTHSTAR.md")
    assert b"- No ads." in exported.partition(b"\n")[2]


@pytest.mark.ac("AC-96")
def test_northstar_export_rejects_programme_without_northstar(a6_tree):
    result = a6_tree.run("northstar", "export", "alpha", "--repo", a6_tree.repo_a)
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["no North Star for alpha"]


@pytest.mark.ac("AC-97")
def test_northstar_export_refuses_and_preserves_hand_edited_copy(a6_written):
    a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    path = a6_written.repo_a / "NORTHSTAR.md"
    a6_written.append(path, "Edited by hand.\n")
    result = a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    assert result.returncode == 3
    assert "hand-edited" in result.stderr
    assert a6_written.read_bytes(path).endswith(b"Edited by hand.\n")


@pytest.mark.ac("AC-97")
def test_northstar_export_force_restores_original_bytes_after_hand_edit(a6_written):
    a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    path = a6_written.repo_a / "NORTHSTAR.md"
    original = a6_written.read_bytes(path)
    a6_written.append(path, "Edited by hand.\n")
    # Row 1 controls row 2's equality, which empty stub bytes alone would satisfy.
    refused = a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a)
    assert refused.returncode == 3
    assert "hand-edited" in refused.stderr
    assert a6_written.read_bytes(path).endswith(b"Edited by hand.\n")
    result = a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a,
                            "--force")
    assert result.returncode == 0
    assert a6_written.read_bytes(path) == original


@pytest.mark.ac("AC-97")
def test_northstar_export_refuses_managed_pm_without_creating_file(a6_written):
    result = a6_written.run("northstar", "export", "alpha", "--repo", a6_written.repo_a,
                            env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert "user-only" in result.stderr
    assert not a6_written.exists(a6_written.repo_a / "NORTHSTAR.md")


@pytest.mark.ac("AC-98")
def test_programme_page_plain_lists_programme_state_and_loads_nothing_external(a6_page_tree):
    result = a6_page_tree.run("page", "build", "alpha")
    assert result.returncode == 0
    page = a6_page_tree.state_text("pages/alpha.html")
    for expected in (
        "<title>Yggdrasil: alpha</title>",
        "<li>alpha-p1: waiting for your approval, phase 2</li>",
        "<li>Decisions waiting: 1</li>",
        "<li>Held launches: 0</li>",
        "<li>Envelope: none</li>",
        "<li>Latest digest: none</li>",
        "alpha-p2",
    ):
        assert expected in page
    assert "beta-p1" not in page
    for forbidden in ("http://", "https://", "<script", "<link", "<img", "<iframe"):
        assert forbidden not in page


@pytest.mark.ac("AC-98")
def test_programme_page_prince2_uses_awaiting_approval_and_stage(a6_page_tree):
    a6_page_tree.write_yaml(a6_page_tree.root / "vocabulary.yaml", {"vocabulary": "prince2"})
    result = a6_page_tree.run("page", "build", "alpha")
    assert result.returncode == 0
    assert "<li>alpha-p1: awaiting approval, stage 2</li>" in a6_page_tree.state_text(
        "pages/alpha.html"
    )


@pytest.mark.ac("AC-98")
def test_programme_page_writes_chosen_path_and_rejects_unknown_programme(a6_page_tree):
    path = a6_page_tree.root / "ygg-page.html"
    result = a6_page_tree.run("page", "build", "alpha", "--out", path)
    assert result.returncode == 0
    assert "<title>Yggdrasil: alpha</title>" in a6_page_tree.read(path)
    unknown = a6_page_tree.run("page", "build", "zeta")
    assert unknown.returncode == 1
    assert unknown.stderr.splitlines() == ["unknown programme: zeta"]

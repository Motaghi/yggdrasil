"""A Codex reviewer's near-miss verdict is read, and a verdict for another review never is (O-6, AC-15)."""
from __future__ import annotations

import pytest

from yggdrasil import review


def _write(text, activation="act-0001", pinned="rv-0001"):
    folder = review._review_dir(activation)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "verdict.json").write_text(text)
    return _record(activation, pinned)


def _record(activation, pinned="rv-0001"):
    """The activation's own record, with the review it was given pinned in it."""
    return {"activation": activation, **({"review": {"id": pinned}} if pinned else {})}


def test_a_plain_verdict_is_read_as_before():
    verdict = review._read_verdict(_write('{"review": "rv-0001", "verdict": "approve"}'), "rv-0001")
    assert verdict["verdict"] == "approve" and verdict["review"] == "rv-0001"


@pytest.mark.parametrize("fence", ["```json\n{body}\n```", "```\n{body}\n```", "```JSON \n{body}\n```\n",
                                   "\n```json\r\n{body}\r\n```  "])
def test_one_fenced_block_that_is_the_whole_file_is_read(fence):
    body = '{"review": "rv-0001", "verdict": "approve"}'
    assert review._read_verdict(_write(fence.replace("{body}", body)), "rv-0001")["verdict"] == "approve"


@pytest.mark.parametrize("text", ['Here is my verdict:\n```json\n{"review": "rv-0001", "verdict": "approve"}\n```',
                                  '```json\n{"review": "rv-0001", "verdict": "approve"}\n```\nthanks',
                                  '```json\n{"review": "rv-0001", "verdict": "approve"}\n```\n```json\n{}\n```',
                                  '```json\n{"review": "rv-0001", "verdict": "approve"}', 'not json'])
def test_anything_else_around_the_object_is_not_read(text):
    assert review._read_verdict(_write(text), "rv-0001") is None


@pytest.mark.parametrize("word,expected", [(" Approve ", "approve"), ("REVISE", "revise"), ("\tapprove\n", "approve")])
def test_a_verdict_word_with_stray_case_or_spaces_is_read(word, expected):
    verdict = review._read_verdict(_write('{"review": "rv-0001", "verdict": "' + word.replace("\t", "\\t").replace("\n", "\\n") + '"}'),
                                   "rv-0001")
    assert verdict["verdict"] == expected


@pytest.mark.parametrize("word", ['"maybe"', "1", "null", '["approve"]'])
def test_other_verdict_values_are_not_read(word):
    assert review._read_verdict(_write('{"review": "rv-0001", "verdict": ' + word + '}'), "rv-0001") is None


def test_a_missing_review_key_is_taken_from_the_activations_own_review():
    verdict = review._read_verdict(_write('{"verdict": "revise", "findings": [{"file": "a.py", "line": 3, '
                                          '"text": "off by one"}]}'), "rv-0001")
    assert verdict["review"] == "rv-0001" and verdict["findings"][0]["text"] == "off by one"


def test_a_missing_review_key_with_no_review_to_take_it_from_is_not_read():
    assert review._read_verdict(_write('{"verdict": "approve"}', pinned=None), "rv-0001") is None


def test_a_missing_review_key_is_not_filled_when_the_pinned_review_differs_from_the_one_settled():
    assert review._read_verdict(_write('{"verdict": "approve"}', pinned="rv-0002"), "rv-0001") is None


def test_a_review_key_that_is_not_text_is_not_read():
    assert review._read_verdict(_write('{"review": 7, "verdict": "approve"}'), "rv-0001") is None


def test_a_verdict_for_another_review_is_returned_as_written_so_it_is_never_adopted():
    verdict = review._read_verdict(_write('{"review": "rv-0002", "verdict": "approve"}'), "rv-0001")
    assert verdict["review"] == "rv-0002"  # _settle compares it with the review it settles and counts no verdict


KEYLESS_APPROVAL = '{"verdict": "approve"}'


def test_a_verdict_file_linked_to_an_earlier_activations_keyless_approval_is_not_adopted():
    earlier = _write(KEYLESS_APPROVAL, activation="act-0001", pinned="rv-0001")
    assert review._read_verdict(earlier, "rv-0001")["verdict"] == "approve"  # its own file is fine
    later = _record("act-0002", "rv-0002")
    folder = review._review_dir("act-0002")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "verdict.json").symlink_to(review._review_dir("act-0001") / "verdict.json")
    assert review._read_verdict(later, "rv-0002") is None


def test_a_redirected_scratch_folder_is_not_read():
    _write('{"review": "rv-0002", "verdict": "approve"}', activation="act-0001", pinned="rv-0002")
    redirected = review._review_dir("act-0002")
    redirected.parent.mkdir(parents=True, exist_ok=True)
    redirected.symlink_to(review._review_dir("act-0001"), target_is_directory=True)
    assert review._read_verdict(_record("act-0002", "rv-0002"), "rv-0002") is None


def test_a_verdict_that_is_not_a_regular_file_or_is_too_large_is_not_read():
    import os
    folder = review._review_dir("act-0003")
    folder.mkdir(parents=True, exist_ok=True)
    os.mkfifo(folder / "verdict.json")  # a named pipe with no writer must neither block nor be read
    assert review._read_verdict(_record("act-0003"), "rv-0001") is None
    os.unlink(folder / "verdict.json")
    (folder / "verdict.json").mkdir()
    assert review._read_verdict(_record("act-0003"), "rv-0001") is None
    (folder / "verdict.json").rmdir()
    (folder / "verdict.json").write_text('{"review": "rv-0001", "verdict": "approve", "pad": "' +
                                         "x" * review.VERDICT_LIMIT + '"}')
    assert review._read_verdict(_record("act-0003"), "rv-0001") is None


def test_a_verdict_exactly_at_the_size_limit_is_read():
    body = '{"review": "rv-0001", "verdict": "approve", "pad": "'
    pad = review.VERDICT_LIMIT - len(body) - 2
    record = _write(body + "x" * pad + '"}')
    assert (review._review_dir("act-0001") / "verdict.json").stat().st_size == review.VERDICT_LIMIT
    assert review._read_verdict(record, "rv-0001")["verdict"] == "approve"


@pytest.mark.parametrize("failing", ["fstat", "read"])
def test_an_io_error_after_a_successful_open_gives_no_verdict_and_closes_the_descriptors(monkeypatch, failing):
    import errno
    import os
    record = _write('{"review": "rv-0001", "verdict": "approve"}')
    opened, closed = [], []
    real_open, real_close = os.open, os.close

    def tracking_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    def broken(*args, **kwargs):
        raise OSError(errno.EIO, "input/output error")

    with monkeypatch.context() as patched:  # only around the call: pytest itself uses fstat and read
        patched.setattr(os, "open", tracking_open)
        patched.setattr(os, "close", tracking_close)
        patched.setattr(os, failing, broken)
        verdict = review._read_verdict(record, "rv-0001")
    assert verdict is None
    assert len(opened) == 2 and sorted(closed) == sorted(opened)


# The review pinned in the activation's own record comes in three shapes (dev A's branch stores the id in `review`
# and the object in `review_target`; older records hold the object in `review`).
@pytest.mark.parametrize("record", [
    {"review": "rv-0001"},
    {"review": "rv-0001", "review_target": {"id": "rv-0001", "package": "WP-1"}},
    {"review_target": {"id": "rv-0001", "package": "WP-1"}},
    {"review": {"id": "rv-0001", "package": "WP-1"}},
], ids=["review-string", "string-and-target", "target-object", "legacy-object"])
def test_a_missing_review_key_is_filled_from_every_shape_of_the_pinned_review(record):
    activation = {"activation": "act-0001", **record}
    _write('{"verdict": "approve"}', pinned=None)
    verdict = review._read_verdict(activation, "rv-0001")
    assert verdict["review"] == "rv-0001" and verdict["verdict"] == "approve"


def test_review_target_wins_over_the_review_field():
    activation = {"activation": "act-0001", "review": "rv-0002", "review_target": {"id": "rv-0001"}}
    _write('{"verdict": "approve"}', pinned=None)
    assert review._pinned_review_id(activation) == "rv-0001"
    assert review._read_verdict(activation, "rv-0001")["review"] == "rv-0001"


@pytest.mark.parametrize("record", [{}, {"review": ""}, {"review": 7}, {"review_target": "rv-0001"},
                                    {"review_target": {"id": 1}}, {"review": {"id": None}}])
def test_no_pinned_review_in_any_shape_leaves_a_keyless_verdict_unread(record):
    activation = {"activation": "act-0001", **record}
    _write('{"verdict": "approve"}', pinned=None)
    assert review._pinned_review_id(activation) is None
    assert review._read_verdict(activation, "rv-0001") is None


def test_a_pinned_review_that_differs_from_the_one_settled_is_still_not_filled_in_the_string_shape():
    _write('{"verdict": "approve"}', pinned=None)
    assert review._read_verdict({"activation": "act-0001", "review": "rv-0002"}, "rv-0001") is None

"""Review keep-set decisions (no Google Photos / GPU required)."""

from __future__ import annotations

import pytest

from photo_organiser.review import resolve_keep_decision


def _m(key: str, *, favorite: bool = False, excluded: bool = False) -> dict:
    return {
        "media_key": key,
        "dedup_key": f"d-{key}",
        "is_favorite": favorite,
        "excluded": excluded,
    }


MEMBERS = [_m("a"), _m("b"), _m("c")]


def test_keep_proposed_only_is_accepted():
    out = resolve_keep_decision(MEMBERS, ["a"], proposed_keeper="a")
    assert out["status"] == "accepted"
    assert out["keep"] == "a"
    assert out["trash"] == ["d-b", "d-c"]
    assert out["override_keeper"] is None


def test_keep_two_trashes_the_rest():
    out = resolve_keep_decision(MEMBERS, ["a", "c"], proposed_keeper="a")
    assert out["status"] == "overridden"
    assert out["keep"] == "a"
    assert out["trash"] == ["d-b"]
    assert out["override_keeper"] == "a"


def test_keep_all_selected_is_kept_all():
    out = resolve_keep_decision(MEMBERS, ["c", "a", "b"], proposed_keeper="a")
    assert out["status"] == "kept_all"
    assert out["keep"] == "a"
    assert out["trash"] == []
    assert out["override_keeper"] is None


def test_favorites_are_not_trashed():
    members = [_m("a"), _m("b", favorite=True), _m("c")]
    out = resolve_keep_decision(members, ["a"], proposed_keeper="a")
    assert out["trash"] == ["d-c"]


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="not in this group"):
        resolve_keep_decision(MEMBERS, ["a", "z"], proposed_keeper="a")


def test_empty_keep_rejected():
    with pytest.raises(ValueError, match="no keeper available"):
        resolve_keep_decision(MEMBERS, [], proposed_keeper="a")

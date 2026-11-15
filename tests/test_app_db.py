"""Tests for the SQLite app store, especially the approval-token rules."""

import pytest

from scc.store.app_db import ApprovalError, AppStore, input_hash


@pytest.fixture
def store(tmp_path):
    s = AppStore(tmp_path / "app.sqlite")
    yield s
    s.close()


def test_sessions_and_requests(store):
    sid = store.create_session()
    store.record_request(
        sid, "What sold best in November?", sdk_session_id="sdk-1", subtype="success",
        is_error=False, num_turns=4, duration_ms=1200, cost_usd=0.05, usage={"input_tokens": 10},
    )  # fmt: skip
    session = store.get_session(sid)
    assert session["sdk_session_id"] == "sdk-1"
    assert session["title"] == "What sold best in November?"
    assert store.find_session("sdk-1")["id"] == sid  # --resume accepts either id
    [listed] = store.list_sessions()
    assert listed["requests"] == 1 and listed["cost_usd"] == pytest.approx(0.05)


def test_memory_add_list_delete(store):
    mid = store.add_memory("  Focus on UK and Germany ")
    assert [m["text"] for m in store.list_memory()] == ["Focus on UK and Germany"]
    assert store.delete_memory(mid) is True
    assert store.delete_memory(mid) is False  # already deleted
    assert store.list_memory() == []


def test_input_hash_ignores_approval_id_and_key_order():
    a = {"x": 1, "y": [1, 2]}
    assert input_hash(a) == input_hash({"y": [1, 2], "x": 1, "approval_id": "abc"})
    assert input_hash(a) != input_hash({"x": 2, "y": [1, 2]})


def _approved(store, tool="t", inp=None):
    inp = inp or {"q": 1}
    aid = store.create_approval("s1", tool, inp, "summary")
    store.decide_approval(aid, True, "seller")
    return aid, inp


def test_consume_approval_happy_path_and_single_use(store):
    aid, inp = _approved(store)
    row = store.consume_approval(aid, "t", {**inp, "approval_id": aid})
    assert row["id"] == aid
    with pytest.raises(ApprovalError, match="already used"):
        store.consume_approval(aid, "t", inp)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", "needs the seller's approval"),
        ("unknown", "Unknown approval"),
        ("pending", "not approved"),
        ("denied", "not approved"),
        ("other_tool", "different action"),
        ("tampered", "changed after"),
    ],
)
def test_consume_approval_rejections(store, case, expected):
    inp = {"q": 1}
    aid = store.create_approval("s1", "t", inp, "summary")
    tool, given_id, given_input = "t", aid, inp
    if case == "missing":
        given_id = None
    elif case == "unknown":
        given_id = "nope"
    elif case == "denied":
        store.decide_approval(aid, False, "seller")
    elif case in ("other_tool", "tampered"):
        store.decide_approval(aid, True, "seller")
        if case == "other_tool":
            tool = "other"
        else:
            given_input = {"q": 999}
    with pytest.raises(ApprovalError, match=expected):
        store.consume_approval(given_id, tool, given_input)


def test_decision_is_final(store):
    aid = store.create_approval("s1", "t", {}, "summary")
    store.decide_approval(aid, False, "timeout")
    store.decide_approval(aid, True, "seller")  # too late: ignored
    assert store.get_approval(aid)["status"] == "denied"


def test_audit_log(store):
    store.audit("tool_ok", session_id="s1", agent="sales_analyst", tool_name="x", detail="y" * 5000)
    [row] = store.list_audit("s1")
    assert row["agent"] == "sales_analyst"
    assert len(row["detail"]) == 2000  # long details are truncated

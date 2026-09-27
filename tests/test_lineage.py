"""Tests for ccc_server/lineage.py: composing the three relations CCC already
stores (spawn parent/child, report-to, continuation ancestor/successor) into
`chain_summary()` (for `ccc brief`) and `collapse_chain_hits()` (for
`ccc recall` / sidebar search)."""

import json
import sqlite3

import pytest

import ccc_server.lineage as lineage
import ccc_server.report_routes as report_routes
import ccc_server.ship_graph as ship_graph


@pytest.fixture
def db_conn(tmp_path):
    conn = sqlite3.connect(":memory:")
    ship_graph._init_db(conn)
    yield conn
    conn.close()


def _insert_session_meta(conn, sid, start_ts=0.0, continuation_origin=""):
    conn.execute(
        "INSERT OR REPLACE INTO session_meta VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, "repo", "/tmp/repo", start_ts, start_ts, "[]", "{}", "[]", continuation_origin),
    )
    conn.commit()


@pytest.fixture
def session_graph_file(tmp_path):
    return str(tmp_path / "session-graph.json")


def _write_edges(path, edges):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"edges": edges}, f)


def _edge(parent, child, **extra):
    e = {"parent": parent, "child": child, "source": "test", "engine": "claude",
         "resumable": True, "name": "", "model": ""}
    e.update(extra)
    return e


# -- spawn parent/child --------------------------------------------------

def test_spawn_parent_and_children(session_graph_file):
    _write_edges(session_graph_file, [_edge("orch-1", "child-1"), _edge("orch-1", "child-2")])
    assert lineage.spawn_parent_of("child-1", path=session_graph_file) == "orch-1"
    assert lineage.spawn_parent_of("child-2", path=session_graph_file) == "orch-1"
    assert lineage.spawn_parent_of("orch-1", path=session_graph_file) == ""
    assert set(lineage.spawn_children_of("orch-1", path=session_graph_file)) == {"child-1", "child-2"}


def test_spawn_edges_missing_file_returns_empty(tmp_path):
    missing = str(tmp_path / "does-not-exist.json")
    assert lineage.spawn_parent_of("anything", path=missing) == ""
    assert lineage.spawn_children_of("anything", path=missing) == []


# -- report-to -------------------------------------------------------------

def test_report_to_of_reads_current_route(tmp_path):
    routes_path = str(tmp_path / "report-routes.json")
    route_id = report_routes.create("dispatcher-1", path=routes_path)
    report_routes.set_child(route_id, "child-1", path=routes_path)
    assert lineage.report_to_of("child-1", path=routes_path) == "dispatcher-1"
    assert lineage.report_to_of("no-such-child", path=routes_path) == ""


def test_orchestrator_parent_prefers_spawn_edge(tmp_path, session_graph_file):
    routes_path = str(tmp_path / "report-routes.json")
    _write_edges(session_graph_file, [_edge("orch-1", "child-1")])
    route_id = report_routes.create("dispatcher-1", path=routes_path)
    report_routes.set_child(route_id, "child-1", path=routes_path)
    assert lineage.orchestrator_parent(
        "child-1", session_graph_path=session_graph_file, report_routes_path=routes_path
    ) == "orch-1"


def test_orchestrator_parent_falls_back_to_report_to(tmp_path, session_graph_file):
    routes_path = str(tmp_path / "report-routes.json")
    _write_edges(session_graph_file, [])
    route_id = report_routes.create("dispatcher-1", path=routes_path)
    report_routes.set_child(route_id, "child-1", path=routes_path)
    assert lineage.orchestrator_parent(
        "child-1", session_graph_path=session_graph_file, report_routes_path=routes_path
    ) == "dispatcher-1"


def test_orchestrator_parent_empty_when_root(tmp_path, session_graph_file):
    routes_path = str(tmp_path / "report-routes.json")
    _write_edges(session_graph_file, [])
    assert lineage.orchestrator_parent(
        "root-sid", session_graph_path=session_graph_file, report_routes_path=routes_path
    ) == ""


# -- continuation ancestor/successor ---------------------------------------

def test_continuation_ancestors_walks_backward(db_conn):
    _insert_session_meta(db_conn, "session-a", start_ts=1.0)
    _insert_session_meta(db_conn, "session-b", start_ts=2.0, continuation_origin="session-a")
    _insert_session_meta(db_conn, "session-c", start_ts=3.0, continuation_origin="session-b")
    assert lineage.continuation_ancestors_of(db_conn, "session-c") == ["session-b", "session-a"]
    assert lineage.continuation_ancestors_of(db_conn, "session-a") == []


def test_latest_successor_walks_forward(db_conn):
    _insert_session_meta(db_conn, "session-a", start_ts=1.0)
    _insert_session_meta(db_conn, "session-b", start_ts=2.0, continuation_origin="session-a")
    _insert_session_meta(db_conn, "session-c", start_ts=3.0, continuation_origin="session-b")
    assert lineage.latest_successor(db_conn, "session-a") == "session-c"
    assert lineage.latest_successor(db_conn, "session-b") == "session-c"
    assert lineage.latest_successor(db_conn, "session-c") == "session-c"


def test_continuation_cycle_is_guarded(db_conn):
    _insert_session_meta(db_conn, "session-a", start_ts=1.0, continuation_origin="session-b")
    _insert_session_meta(db_conn, "session-b", start_ts=2.0, continuation_origin="session-a")
    # Must terminate rather than looping forever.
    assert lineage.continuation_ancestors_of(db_conn, "session-a") == ["session-b"]
    assert lineage.latest_successor(db_conn, "session-a") in ("session-a", "session-b")


# -- chain_summary -----------------------------------------------------------

def test_chain_summary_full_chain(tmp_path, db_conn, session_graph_file):
    routes_path = str(tmp_path / "report-routes.json")
    _write_edges(session_graph_file, [_edge("dispatcher-1", "session-a")])
    _insert_session_meta(db_conn, "session-a", start_ts=1.0)
    _insert_session_meta(db_conn, "session-b", start_ts=2.0, continuation_origin="session-a")
    _insert_session_meta(db_conn, "session-c", start_ts=3.0, continuation_origin="session-b")

    summary = lineage.chain_summary(
        db_conn, "session-b", session_graph_path=session_graph_file, report_routes_path=routes_path
    )
    assert summary["parent"] == "dispatcher-1"
    assert summary["latest"] == "session-c"
    assert summary["continuation_ancestors"] == ["session-a"]


def test_chain_summary_no_lineage_is_all_empty(tmp_path, db_conn, session_graph_file):
    routes_path = str(tmp_path / "report-routes.json")
    _write_edges(session_graph_file, [])
    _insert_session_meta(db_conn, "lone-session", start_ts=1.0)
    summary = lineage.chain_summary(
        db_conn, "lone-session", session_graph_path=session_graph_file, report_routes_path=routes_path
    )
    assert summary == {"parent": "", "latest": "", "continuation_ancestors": []}


# -- collapse_chain_hits ------------------------------------------------------

def _hit(sid, ts):
    return {"session_id": sid, "ts_unix": ts, "score": 1.0}


def test_collapse_continuation_chain_keeps_newest(db_conn, session_graph_file):
    _write_edges(session_graph_file, [])
    _insert_session_meta(db_conn, "session-a", start_ts=1.0)
    _insert_session_meta(db_conn, "session-b", start_ts=2.0, continuation_origin="session-a")
    hits = [_hit("session-a", 1.0), _hit("session-b", 2.0), _hit("other", 5.0)]
    out = lineage.collapse_chain_hits(hits, db_conn, session_graph_path=session_graph_file)
    sids = [h["session_id"] for h in out]
    assert sids == ["session-b", "other"]
    rep = out[0]
    assert rep["chain_collapsed"] == 1
    assert rep["chain_collapsed_sids"] == ["session-a"]


def test_collapse_spawn_family_keeps_newest(db_conn, session_graph_file):
    _write_edges(session_graph_file, [_edge("orch-1", "child-1")])
    _insert_session_meta(db_conn, "orch-1", start_ts=1.0)
    _insert_session_meta(db_conn, "child-1", start_ts=5.0)
    hits = [_hit("orch-1", 1.0), _hit("child-1", 5.0)]
    out = lineage.collapse_chain_hits(hits, db_conn, session_graph_path=session_graph_file)
    assert len(out) == 1
    assert out[0]["session_id"] == "child-1"
    assert out[0]["chain_collapsed"] == 1


def test_collapse_does_not_link_through_absent_ancestor(db_conn, session_graph_file):
    """Two siblings of the same orchestrator must NOT collapse into each
    other when that orchestrator itself isn't one of the hits -- collapsing
    must never hide a result behind an ancestor the caller never asked
    about."""
    _write_edges(session_graph_file, [_edge("orch-1", "child-1"), _edge("orch-1", "child-2")])
    _insert_session_meta(db_conn, "child-1", start_ts=1.0)
    _insert_session_meta(db_conn, "child-2", start_ts=2.0)
    hits = [_hit("child-1", 1.0), _hit("child-2", 2.0)]
    out = lineage.collapse_chain_hits(hits, db_conn, session_graph_path=session_graph_file)
    assert len(out) == 2


def test_collapse_single_hit_is_unchanged(db_conn, session_graph_file):
    hits = [_hit("only-one", 1.0)]
    out = lineage.collapse_chain_hits(hits, db_conn, session_graph_path=session_graph_file)
    assert out == hits

from tui_gateway import server


def test_session_search_collapses_compression_lineage_to_live_tip(monkeypatch):
    class SearchDB:
        def search_messages(self, query, **kwargs):
            return [
                {"session_id": "root", "role": "user", "snippet": ">>>needle<<< root", "timestamp": 1, "source": "tui"},
                {"session_id": "tip", "role": "assistant", "snippet": ">>>needle<<< tip", "timestamp": 2, "source": "tui"},
            ]

        def list_sessions_rich(self, **kwargs):
            return []

        def get_session(self, session_id):
            rows = {
                "root": {"id": "root", "parent_session_id": None, "ended_at": 100, "end_reason": "compression", "started_at": 10},
                "tip": {"id": "tip", "parent_session_id": "root", "started_at": 101},
            }
            return rows.get(session_id)

        def get_compression_tip(self, session_id):
            assert session_id == "root"
            return "tip"

        def get_session_rich_row(self, session_id):
            assert session_id == "tip"
            return {"id": "tip", "title": "Live tip", "source": "tui", "started_at": 101, "last_active": 102, "message_count": 3, "archived": 0, "hidden": 0}

    monkeypatch.setattr(server, "_get_db", lambda: SearchDB())
    response = server.handle_request({
        "id": "lineage", "method": "session.search",
        "params": {"query": "needle", "limit": 20},
    })

    assert response is not None
    assert "error" not in response, response
    results = response["result"]["results"]
    assert len(results) == 1
    assert results[0]["id"] == "tip"
    assert results[0]["lineage_root"] == "root"
    assert results[0]["match_count"] == 2


def test_session_search_does_not_expose_database_errors(monkeypatch):
    class BrokenDB:
        def search_messages(self, *args, **kwargs):
            raise RuntimeError("secret path /private/state.db")

    monkeypatch.setattr(server, "_get_db", lambda: BrokenDB())
    response = server.handle_request({
        "id": "failure", "method": "session.search", "params": {"query": "needle"}
    })

    assert response is not None
    assert response["error"]["message"] == "Search failed"
    assert "private" not in str(response)

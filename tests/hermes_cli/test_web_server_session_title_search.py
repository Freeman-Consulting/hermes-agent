import asyncio

from hermes_cli import web_server


def test_desktop_search_finds_server_wide_title_match(monkeypatch):
    class MetadataDB:
        opened_read_only = None

        def __init__(self, *args, **kwargs):
            type(self).opened_read_only = kwargs.get("read_only")

        def search_sessions_by_id(self, *args, **kwargs):
            return []

        def search_messages(self, *args, **kwargs):
            return []

        def list_sessions_rich(self, **kwargs):
            assert kwargs["search_query"] == "remote title"
            assert kwargs["include_hidden"] is False
            return [{"id": "older-session", "title": "Remote Title", "preview": "Older preview", "source": "tui", "started_at": 100}]

        def get_session(self, session_id):
            return {"id": session_id, "parent_session_id": None}

        def get_compression_tip(self, session_id):
            return session_id

        def get_session_rich_row(self, session_id):
            return {"id": session_id, "title": "Remote Title", "preview": "Older preview", "source": "tui", "started_at": 100, "last_active": 200, "message_count": 2}

        def close(self):
            pass

    monkeypatch.setattr("hermes_state.SessionDB", MetadataDB)
    response = asyncio.run(web_server.search_sessions(q="remote title", limit=20))

    assert MetadataDB.opened_read_only is True
    assert [row["session_id"] for row in response["results"]] == ["older-session"]
    assert response["results"][0]["title"] == "Remote Title"

from copy import deepcopy
import pytest


def test_unchanged_checks_do_not_queue_writes_after_restart(tmp_path, listing):
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        assert state.observe(listing) is True
        batch = state.pending()
        state.acknowledge(batch, [listing["id"]])
    updated = deepcopy(listing)
    updated["last_seen"] = updated["last_checked"] = "2026-10-12T13:00:00Z"
    with State(path) as state:
        assert state.observe(updated) is False
        assert state.pending() == []
        assert state.catalog()[0]["last_checked"] == "2026-10-12T13:00:00Z"


def test_partial_ack_preserves_pending_across_restart(tmp_path, listing):
    from index_scrapers.state import State
    second = deepcopy(listing)
    second["id"] = "fotocasa:2"
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        state.observe(second)
        state.acknowledge(state.pending(), [listing["id"]])
    with State(tmp_path / "state.sqlite") as state:
        assert [item["listing"]["id"] for item in state.pending()] == ["fotocasa:2"]
        assert len(state.catalog()) == 2


def test_ack_of_old_price_does_not_delete_newer_pending_change(tmp_path, listing):
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        in_flight = state.pending()
        changed = deepcopy(listing)
        changed["price"] = 1300
        state.observe(changed)
        state.acknowledge(in_flight, [listing["id"]])
        assert state.pending()[0]["listing"]["price"] == 1300


def test_pending_batch_keeps_multiple_price_revisions_in_order(tmp_path, listing):
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        changed = deepcopy(listing)
        changed["price"] = 1300
        state.observe(changed)
        assert len(state.pending()) == 1
        state.acknowledge(state.pending(), [listing["id"]])
        assert state.pending()[0]["listing"]["price"] == 1300


def test_only_two_consecutive_not_found_responses_remove(tmp_path, listing):
    from index_scrapers.state import State
    path = tmp_path / "state.sqlite"
    with State(path) as state:
        state.observe(listing)
        state.acknowledge(state.pending(), [listing["id"]])
        assert state.check(listing["id"], 404, "2026-10-12T13:00:00Z") is False
    with State(path) as state:
        assert state.check(listing["id"], 410, "2026-10-12T14:00:00Z") is True
        assert state.pending()[0]["listing"]["status"] == "removed"


@pytest.mark.parametrize("status", [None, 403, 429, 500, 503])
def test_temporary_errors_break_missing_streak_without_removal(tmp_path, listing, status):
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        state.check(listing["id"], 404, "2026-10-12T13:00:00Z")
        state.check(listing["id"], status, "2026-10-12T13:01:00Z")
        assert state.check(listing["id"], 404, "2026-10-12T13:02:00Z") is False
        assert state.catalog()[0]["status"] == "active"


def test_backup_restores_catalog_and_pending(tmp_path, listing):
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        state.backup(tmp_path / "snapshot.sqlite")
    with State(tmp_path / "snapshot.sqlite") as state:
        assert state.pending()[0]["listing"]["id"] == listing["id"]


def test_catalog_identity_cannot_silently_change(tmp_path, listing):
    from index_scrapers.state import State
    with State(tmp_path / "state.sqlite") as state:
        state.observe(listing)
        changed = deepcopy(listing)
        changed["property_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
        with pytest.raises(ValueError):
            state.observe(changed)

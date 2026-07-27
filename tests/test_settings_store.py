from pathlib import Path

import pytest

from app.db import Database
from app.settings_store import SettingsStore


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    database = Database(tmp_path / "recolecta.db")
    database.initialize()
    return SettingsStore(database)


def test_settings_crud_preserves_json_types(store: SettingsStore) -> None:
    store.set("schedule", {"hour": 2, "enabled": True})
    store.set("clients", ["A", "B"])
    assert store.get("schedule") == {"hour": 2, "enabled": True}
    assert store.get("missing", 42) == 42
    assert store.all() == {
        "clients": ["A", "B"],
        "schedule": {"enabled": True, "hour": 2},
    }
    store.set("schedule", {"hour": 3})
    assert store.get("schedule") == {"hour": 3}
    assert store.delete("schedule")
    assert not store.delete("schedule")


def test_empty_setting_key_is_rejected(store: SettingsStore) -> None:
    with pytest.raises(ValueError, match="no puede estar vacía"):
        store.set(" ", True)

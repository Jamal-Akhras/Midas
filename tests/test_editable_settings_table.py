"""The Settings tab and `update_setting` read EDITABLE_SETTINGS; a nesting
slip once hid the whole core-book group inside the REGIME_THROTTLE entry."""

from midas.config import EDITABLE_SETTINGS, Settings


def test_every_entry_is_flat_and_points_at_a_real_setting():
    for name, spec in EDITABLE_SETTINGS.items():
        assert set(spec) == {"attr", "type", "group", "fingerprint"}, name
        assert hasattr(Settings, "model_fields") and spec["attr"] in Settings.model_fields, name


def test_core_book_settings_are_editable_at_top_level():
    for name in ("CORE_ENABLED", "CORE_UNIVERSE", "CORE_ALLOCATION_PCT", "EVENT_SCAN_WEEKDAYS"):
        assert name in EDITABLE_SETTINGS, name

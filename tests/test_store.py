from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foxhole_buddy.core.store import StockpileStore, warning_due


class StockpileStoreTest(unittest.TestCase):
    def test_create_sets_expiry_to_48_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)
            store = StockpileStore(Path(tmp) / "stockpiles.json")

            stockpile = store.create(
                guild_id=1,
                channel_id=2,
                name="Ammo",
                location="Basin",
                stockpile_type="seaport",
                user_id=3,
                now=now,
            )

            self.assertEqual(stockpile.expires_datetime, now + timedelta(hours=48))

    def test_refresh_resets_warnings_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)
            refresh_time = start + timedelta(hours=10)
            store = StockpileStore(Path(tmp) / "stockpiles.json")
            stockpile = store.create(
                guild_id=1,
                channel_id=2,
                name="Bmats",
                location="Depot",
                stockpile_type="storage_depot",
                user_id=3,
                now=start,
            )
            stockpile.reminders_sent = ["12h"]
            store.update(stockpile)

            refreshed = store.refresh(stockpile.id, user_id=4, now=refresh_time)

            self.assertEqual(refreshed.expires_datetime, refresh_time + timedelta(hours=48))
            self.assertEqual(refreshed.reminders_sent, [])
            self.assertEqual(refreshed.last_refreshed_by_user_id, 4)

    def test_warning_due_orders_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)
            store = StockpileStore(Path(tmp) / "stockpiles.json")
            stockpile = store.create(
                guild_id=1,
                channel_id=2,
                name="Shells",
                location="Port",
                stockpile_type="seaport",
                user_id=3,
                now=now - timedelta(hours=47, minutes=40),  # ~20 min left
            )

            # Most urgent crossed-but-unsent threshold wins.
            self.assertEqual(warning_due(stockpile, now), "30m")


if __name__ == "__main__":
    unittest.main()

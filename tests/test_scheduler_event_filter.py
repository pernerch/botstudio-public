import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from ui.views.scheduler_view import SchedulerView


def _event(name):
    return SimpleNamespace(name=name)


class SchedulerEventFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = SchedulerView(game_id="")
        self.view.refresh = MagicMock()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QApplication.processEvents()

    def _snapshot(self):
        return {
            "events_by_profile": {
                "p1": [_event("Gather"), _event("Rewards")],
                "p2": [_event("Rewards"), _event("Arena")],
            }
        }

    def _select(self, name):
        self.view._filter_snapshot_events(self._snapshot())
        self.view.event_filter_combo.setCurrentIndex(
            self.view.event_filter_combo.findData(name)
        )

    def test_button_names_target_view_and_controls_follow_mode(self):
        self.assertEqual(self.view.view_mode_button.text(), "Timeline view")
        self.assertFalse(self.view.timeline_range_combo.isVisibleTo(self.view))
        self.assertTrue(self.view.event_filter_combo.isVisibleTo(self.view))

        self.view.set_view_mode("Timeline")

        self.assertEqual(self.view.view_mode_button.text(), "Table view")
        self.assertTrue(self.view.timeline_range_combo.isVisibleTo(self.view))
        self.assertFalse(self.view.event_filter_combo.isVisibleTo(self.view))

    def test_filter_lists_unique_names_and_keeps_matching_events(self):
        self._select("Rewards")
        combo = self.view.event_filter_combo
        self.assertEqual(
            [combo.itemText(index) for index in range(combo.count())],
            ["All events", "Arena", "Gather", "Rewards"],
        )

        snapshot = self._snapshot()
        self.view._filter_snapshot_events(snapshot)

        self.assertEqual(
            {key: [event.name for event in events] for key, events in snapshot["events_by_profile"].items()},
            {"p1": ["Rewards"], "p2": ["Rewards"]},
        )
        self.assertEqual(combo.currentData(), "Rewards")

    def test_filter_resets_on_view_switch(self):
        self._select("Gather")
        self.view.set_view_mode("Timeline")
        self.assertEqual(self.view.event_filter_combo.currentIndex(), 0)

    def test_filter_resets_on_game_change(self):
        self._select("Gather")
        self.view.set_game_id("other-game")
        self.assertEqual(self.view.event_filter_combo.currentIndex(), 0)

    def test_filter_resets_when_page_is_left(self):
        self.view.show()
        self._select("Gather")
        self.view.hide()
        self.assertEqual(self.view.event_filter_combo.currentIndex(), 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from PySide6.QtWidgets import QApplication

from ui.widgets.event_create_dialog import EventCreateDialog, ProfileChecklist


class EventCreateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_checklist_all_toggle_and_checked_ids(self):
        checklist = ProfileChecklist([("A", "a"), ("B", "b")], ["b"])
        self.assertEqual(checklist.checked_ids(), ["b"])
        checklist.set_checked(["a", "b"])
        self.assertEqual(checklist.checked_ids(), ["a", "b"])

    def test_dialog_values(self):
        dialog = EventCreateDialog([("A", "a"), ("B", "b")], ["a"], "Bear")
        dialog.name_input.setText("  Bear Hunt ")
        values = dialog.values()
        self.assertEqual(values["name"], "Bear Hunt")
        self.assertEqual(values["profile_ids"], ["a"])
        self.assertTrue(values["add_function"])


if __name__ == "__main__":
    unittest.main()

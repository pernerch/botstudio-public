from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QHBoxLayout,
)
from ui.widgets.searchable_combo import SearchableComboBox


class ProfileSelector(QWidget):

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        layout = QHBoxLayout(
            self
        )

        self.label = QLabel(
            "Profile"
        )

        self.combo = SearchableComboBox()

        layout.addWidget(
            self.label
        )

        layout.addWidget(
            self.combo
        )

    def load_profiles(
        self,
        profiles: list[dict],
    ):

        self.combo.clear()

        def order_key(item):
            index, profile = item
            try:
                account_order = int(profile.get("account_order", index))
            except (TypeError, ValueError):
                account_order = index
            return (
                str(profile.get("game", "")).casefold(),
                account_order,
                index,
            )

        ordered = sorted(
            enumerate(profiles),
            key=order_key,
        )
        for _, profile in ordered:

            self.combo.addItem(
                f"[{profile.get('game', '')}] "
                f"{profile.get('player_name', profile.get('name', profile.get('id')))}",
                profile,
            )

    def get_selected_profile(
        self,
    ) -> dict | None:

        return (
            self.combo.currentData()
        )
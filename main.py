import sqlite3
import sys
from pathlib import Path
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "test.db"
TABLE_NAME = "folders"
WINDOW_TITLE = "Folder Toggle"
WINDOW_WIDTH = 440
WINDOW_HEIGHT = 560


class FolderStore:
    def __init__(self, db_path: Path) -> None:
        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self) -> None:
        self.connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1))
            )
            """
        )
        self.connection.commit()

    def seed_if_empty(self, folder_names: Iterable[str]) -> None:
        if self.has_rows():
            return
        self.connection.executemany(
            f"INSERT INTO {TABLE_NAME} (name, enabled) VALUES (?, ?)",
            [(name, 0) for name in folder_names],
        )
        self.connection.commit()

    def has_rows(self) -> bool:
        row = self.connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM {TABLE_NAME} LIMIT 1)"
        ).fetchone()
        return bool(row[0])

    def fetch_all(self) -> list[tuple[str, bool]]:
        rows = self.connection.execute(
            f"SELECT name, enabled FROM {TABLE_NAME} ORDER BY LOWER(name), name"
        ).fetchall()
        return [(row["name"], bool(row["enabled"])) for row in rows]

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.connection.execute(
            f"UPDATE {TABLE_NAME} SET enabled = ? WHERE name = ?",
            (int(enabled), name),
        )
        self.connection.commit()

    def set_all_enabled(self, enabled_by_name: Iterable[tuple[bool, str]]) -> None:
        self.connection.executemany(
            f"UPDATE {TABLE_NAME} SET enabled = ? WHERE name = ?",
            [(int(enabled), name) for enabled, name in enabled_by_name],
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class FolderToggleWindow(QWidget):
    def __init__(self, store: FolderStore) -> None:
        super().__init__()
        self.store = store
        self.is_updating = False
        self.list_widget = QListWidget()
        self.status_label = QLabel()
        self.enable_all_button = QPushButton("Enable All")
        self.disable_all_button = QPushButton("Disable All")
        self.toggle_all_button = QPushButton("Toggle All")
        self._build_ui()
        self._load_items()

    def _build_ui(self) -> None:
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #121212;
                color: #e8e8e8;
                font-size: 14px;
            }
            QListWidget {
                background-color: #1b1b1b;
                border: 1px solid #2b2b2b;
                padding: 6px;
                outline: none;
            }
            QListWidget::item {
                padding: 8px 6px;
            }
            QPushButton {
                background-color: #242424;
                border: 1px solid #353535;
                padding: 10px 14px;
                min-height: 18px;
            }
            QPushButton:hover {
                background-color: #2d2d2d;
            }
            QPushButton:pressed {
                background-color: #1f1f1f;
            }
            QLabel {
                color: #a8a8a8;
            }
            """
        )

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addWidget(self.enable_all_button)
        button_row.addWidget(self.disable_all_button)
        button_row.addWidget(self.toggle_all_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        layout.addWidget(self.list_widget)
        self.setLayout(layout)

        self.enable_all_button.clicked.connect(lambda: self._set_all_items(True))
        self.disable_all_button.clicked.connect(lambda: self._set_all_items(False))
        self.toggle_all_button.clicked.connect(self._toggle_all_items)
        self.list_widget.itemChanged.connect(self._handle_item_changed)

    def _load_items(self) -> None:
        self.is_updating = True
        self.list_widget.clear()
        rows = self.store.fetch_all()
        for name, enabled in rows:
            item = QListWidgetItem(name)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            self.list_widget.addItem(item)
        self.is_updating = False
        self._update_status_label(len(rows))

    def _update_status_label(self, count: int | None = None) -> None:
        item_count = self.list_widget.count() if count is None else count
        enabled_count = sum(
            self.list_widget.item(index).checkState() == Qt.CheckState.Checked
            for index in range(self.list_widget.count())
        )
        self.status_label.setText(
            f"{enabled_count} enabled / {item_count} folders   DB: {DB_PATH.name}"
        )

    def _set_all_items(self, enabled: bool) -> None:
        updates: list[tuple[bool, str]] = []
        self.is_updating = True
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            updates.append((enabled, item.text()))
        self.is_updating = False
        self.store.set_all_enabled(updates)
        self._update_status_label()

    def _toggle_all_items(self) -> None:
        updates: list[tuple[bool, str]] = []
        self.is_updating = True
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            enabled = item.checkState() != Qt.CheckState.Checked
            item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            updates.append((enabled, item.text()))
        self.is_updating = False
        self.store.set_all_enabled(updates)
        self._update_status_label()

    def _handle_item_changed(self, item: QListWidgetItem) -> None:
        if self.is_updating:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        self.store.set_enabled(item.text(), enabled)
        self._update_status_label()

    def closeEvent(self, event) -> None:
        self.store.close()
        super().closeEvent(event)


def get_current_folder_names() -> list[str]:
    return sorted(
        [path.name for path in APP_DIR.iterdir() if path.is_dir()],
        key=str.lower,
    )


def main() -> int:
    store = FolderStore(DB_PATH)
    store.seed_if_empty(get_current_folder_names())
    app = QApplication(sys.argv)
    window = FolderToggleWindow(store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

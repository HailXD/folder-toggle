import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "test.db"
TABLE_NAME = "folders"
WINDOW_TITLE = "Folder Toggle"
WINDOW_WIDTH = 960
WINDOW_HEIGHT = 640
DEFAULT_BRANCH_NAME = "main"
INIT_COMMIT_MESSAGE = "init"
GH_REPO_VISIBILITY_FLAG = "--private"
FOLDER_COLUMN = 0
SIZE_COLUMN = 1
TYPE_COLUMN = 2
SORT_NAME_ASC = "Name (A-Z)"
SORT_NAME_DESC = "Name (Z-A)"
SORT_SIZE_DESC = "Size (Largest)"
SORT_SIZE_ASC = "Size (Smallest)"
EXCLUDED_FOLDER_NAMES = {".git", "__pycache__"}
EXCLUDED_SCAN_DIR_NAMES = {".git", "__pycache__"}
NO_EXTENSION_LABEL = "[no ext]"
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(slots=True)
class RepoResult:
    name: str
    status: str
    details: str


class SortableTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left_value = self.data(Qt.ItemDataRole.UserRole)
        right_value = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(left_value, (int, float)) and isinstance(
            right_value, (int, float)
        ):
            return left_value < right_value
        return super().__lt__(other)


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

    def sync_folder_names(self, folder_names: Iterable[str]) -> None:
        current_names = set(folder_names)
        existing_names = {
            row["name"]
            for row in self.connection.execute(f"SELECT name FROM {TABLE_NAME}").fetchall()
        }
        names_to_add = sorted(current_names - existing_names, key=str.lower)
        names_to_remove = sorted(existing_names - current_names, key=str.lower)
        if names_to_remove:
            self.connection.executemany(
                f"DELETE FROM {TABLE_NAME} WHERE name = ?",
                [(name,) for name in names_to_remove],
            )
        if names_to_add:
            self.connection.executemany(
                f"INSERT INTO {TABLE_NAME} (name, enabled) VALUES (?, ?)",
                [(name, 0) for name in names_to_add],
            )
        if names_to_add or names_to_remove:
            self.connection.commit()

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

    def rename_folder(self, old_name: str, new_name: str) -> None:
        self.connection.execute(
            f"UPDATE {TABLE_NAME} SET name = ? WHERE name = ?",
            (new_name, old_name),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class FolderToggleWindow(QWidget):
    def __init__(self, store: FolderStore) -> None:
        super().__init__()
        self.store = store
        self.is_updating = False
        self.table_widget = QTableWidget()
        self.status_label = QLabel()
        self.activity_label = QLabel("Ready")
        self.enable_all_button = QPushButton("Enable All")
        self.disable_all_button = QPushButton("Disable All")
        self.toggle_all_button = QPushButton("Toggle All")
        self.refresh_button = QPushButton("Refresh")
        self.normalize_names_button = QPushButton("Normalize Names")
        self.create_repos_button = QPushButton("Create Repos")
        self.sort_combo = QComboBox()
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
            QComboBox {
                background-color: #1b1b1b;
                border: 1px solid #353535;
                padding: 8px 10px;
                min-height: 18px;
            }
            QTableWidget {
                background-color: #1b1b1b;
                border: 1px solid #2b2b2b;
                gridline-color: #262626;
                outline: none;
            }
            QHeaderView::section {
                background-color: #181818;
                border: 0;
                border-bottom: 1px solid #2b2b2b;
                padding: 8px 6px;
            }
            """
        )

        self.table_widget.setColumnCount(3)
        self.table_widget.setHorizontalHeaderLabels(["Folder", "Size", "Top File Types"])
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.setSortingEnabled(True)
        self.table_widget.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table_widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self.table_widget.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSortIndicatorShown(True)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.addWidget(self.enable_all_button)
        action_row.addWidget(self.disable_all_button)
        action_row.addWidget(self.toggle_all_button)

        utility_row = QHBoxLayout()
        utility_row.setSpacing(10)
        utility_row.addWidget(self.refresh_button)
        utility_row.addWidget(self.normalize_names_button)
        utility_row.addWidget(self.create_repos_button)
        utility_row.addWidget(QLabel("Sort"))
        self.sort_combo.addItems(
            [SORT_NAME_ASC, SORT_NAME_DESC, SORT_SIZE_DESC, SORT_SIZE_ASC]
        )
        utility_row.addWidget(self.sort_combo)

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.status_label)
        layout.addLayout(action_row)
        layout.addLayout(utility_row)
        layout.addWidget(self.table_widget)
        layout.addWidget(self.activity_label)
        self.setLayout(layout)

        self.enable_all_button.clicked.connect(lambda: self._set_all_items(True))
        self.disable_all_button.clicked.connect(lambda: self._set_all_items(False))
        self.toggle_all_button.clicked.connect(self._toggle_all_items)
        self.refresh_button.clicked.connect(self._refresh_items)
        self.normalize_names_button.clicked.connect(self._normalize_folder_names)
        self.create_repos_button.clicked.connect(self._create_repos)
        self.sort_combo.currentTextChanged.connect(self._apply_sort)
        self.table_widget.itemChanged.connect(self._handle_item_changed)

    def _load_items(self) -> None:
        self.is_updating = True
        self.table_widget.setSortingEnabled(False)
        self.table_widget.setRowCount(0)
        folder_paths = get_current_folders()
        self.store.sync_folder_names(path.name for path in folder_paths)
        enabled_by_name = dict(self.store.fetch_all())
        for folder_path in folder_paths:
            total_size, top_types = get_folder_size_details(folder_path)
            row_index = self.table_widget.rowCount()
            self.table_widget.insertRow(row_index)

            folder_item = QTableWidgetItem(folder_path.name)
            folder_item.setFlags(
                (folder_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            folder_item.setCheckState(
                Qt.CheckState.Checked
                if enabled_by_name.get(folder_path.name, False)
                else Qt.CheckState.Unchecked
            )
            folder_item.setData(Qt.ItemDataRole.UserRole, folder_path.name)
            self.table_widget.setItem(row_index, FOLDER_COLUMN, folder_item)

            size_item = SortableTableWidgetItem(format_size(total_size))
            size_item.setData(Qt.ItemDataRole.UserRole, total_size)
            size_item.setFlags(
                (size_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            self.table_widget.setItem(row_index, SIZE_COLUMN, size_item)

            type_item = QTableWidgetItem(format_top_types(total_size, top_types))
            type_item.setFlags(
                (type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            self.table_widget.setItem(row_index, TYPE_COLUMN, type_item)

        self.is_updating = False
        self.table_widget.setSortingEnabled(True)
        self._apply_sort()
        self._update_status_label(len(folder_paths))

    def _update_status_label(self, count: int | None = None) -> None:
        item_count = self.table_widget.rowCount() if count is None else count
        enabled_count = sum(
            self.table_widget.item(index, FOLDER_COLUMN).checkState()
            == Qt.CheckState.Checked
            for index in range(self.table_widget.rowCount())
        )
        total_size = sum(
            int(self.table_widget.item(index, SIZE_COLUMN).data(Qt.ItemDataRole.UserRole))
            for index in range(self.table_widget.rowCount())
        )
        self.status_label.setText(
            f"{enabled_count} enabled / {item_count} folders   {format_size(total_size)} total   DB: {DB_PATH.name}"
        )

    def _set_all_items(self, enabled: bool) -> None:
        updates: list[tuple[bool, str]] = []
        self.is_updating = True
        for index in range(self.table_widget.rowCount()):
            item = self.table_widget.item(index, FOLDER_COLUMN)
            item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            updates.append((enabled, item.data(Qt.ItemDataRole.UserRole)))
        self.is_updating = False
        self.store.set_all_enabled(updates)
        self._update_status_label()

    def _toggle_all_items(self) -> None:
        updates: list[tuple[bool, str]] = []
        self.is_updating = True
        for index in range(self.table_widget.rowCount()):
            item = self.table_widget.item(index, FOLDER_COLUMN)
            enabled = item.checkState() != Qt.CheckState.Checked
            item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            updates.append((enabled, item.data(Qt.ItemDataRole.UserRole)))
        self.is_updating = False
        self.store.set_all_enabled(updates)
        self._update_status_label()

    def _handle_item_changed(self, item: QTableWidgetItem) -> None:
        if self.is_updating or item.column() != FOLDER_COLUMN:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        self.store.set_enabled(item.data(Qt.ItemDataRole.UserRole), enabled)
        self._update_status_label()

    def closeEvent(self, event) -> None:
        self.store.close()
        super().closeEvent(event)

    def _refresh_items(self) -> None:
        self._load_items()
        self.activity_label.setText("Folder list refreshed")

    def _apply_sort(self) -> None:
        if self.is_updating or not self.table_widget.rowCount():
            return
        sort_choice = self.sort_combo.currentText()
        if sort_choice == SORT_NAME_ASC:
            self.table_widget.sortItems(FOLDER_COLUMN, Qt.SortOrder.AscendingOrder)
        elif sort_choice == SORT_NAME_DESC:
            self.table_widget.sortItems(FOLDER_COLUMN, Qt.SortOrder.DescendingOrder)
        elif sort_choice == SORT_SIZE_ASC:
            self.table_widget.sortItems(SIZE_COLUMN, Qt.SortOrder.AscendingOrder)
        else:
            self.table_widget.sortItems(SIZE_COLUMN, Qt.SortOrder.DescendingOrder)

    def _normalize_folder_names(self) -> None:
        folder_paths = get_current_folders()
        rename_pairs: list[tuple[Path, Path]] = []
        skipped_names: list[str] = []
        planned_targets: set[str] = set()
        for folder_path in folder_paths:
            normalized_name = normalize_folder_name(folder_path.name)
            if normalized_name == folder_path.name:
                continue
            target_path = folder_path.with_name(normalized_name)
            if normalized_name in planned_targets:
                skipped_names.append(folder_path.name)
                continue
            if target_path.exists() and target_path != folder_path:
                skipped_names.append(folder_path.name)
                continue
            rename_pairs.append((folder_path, target_path))
            planned_targets.add(normalized_name)

        if not rename_pairs:
            message = "All folder names already match lowercase-hyphen format."
            if skipped_names:
                message = (
                    f"No folders renamed. Conflicts found for {', '.join(skipped_names)}."
                )
            QMessageBox.information(self, WINDOW_TITLE, message)
            self.activity_label.setText(message)
            return

        confirmation = QMessageBox.question(
            self,
            WINDOW_TITLE,
            f"Rename {len(rename_pairs)} folders to lowercase with spaces replaced by hyphens?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        renamed_count = 0
        errors: list[str] = []
        for source_path, target_path in rename_pairs:
            try:
                rename_folder_path(source_path, target_path)
                self.store.rename_folder(source_path.name, target_path.name)
                renamed_count += 1
            except OSError as error:
                errors.append(f"{source_path.name}: {error}")

        self._load_items()
        summary = f"Renamed {renamed_count} folders"
        if skipped_names:
            summary = f"{summary}; skipped {len(skipped_names)} conflicts"
        if errors:
            summary = f"{summary}; {len(errors)} failed"
            QMessageBox.warning(self, WINDOW_TITLE, "\n".join(errors[:10]))
        else:
            QMessageBox.information(self, WINDOW_TITLE, summary)
        self.activity_label.setText(summary)

    def _create_repos(self) -> None:
        folder_paths = get_current_folders()
        if not folder_paths:
            QMessageBox.information(self, WINDOW_TITLE, "No folders found.")
            self.activity_label.setText("No folders found")
            return

        invalid_names = [
            folder_path.name
            for folder_path in folder_paths
            if not REPO_NAME_PATTERN.fullmatch(folder_path.name)
        ]
        if invalid_names:
            message = (
                "GitHub repo names can only use letters, numbers, dots, underscores, and hyphens.\n"
                f"Invalid folders: {', '.join(invalid_names[:10])}"
            )
            QMessageBox.warning(self, WINDOW_TITLE, message)
            self.activity_label.setText("Repo creation blocked by invalid folder names")
            return

        try:
            run_command(["git", "--version"], APP_DIR)
            run_command(["gh", "--version"], APP_DIR)
            run_command(["gh", "auth", "status"], APP_DIR)
        except RuntimeError as error:
            QMessageBox.critical(self, WINDOW_TITLE, str(error))
            self.activity_label.setText("Repo creation blocked by git or gh setup")
            return

        confirmation = QMessageBox.question(
            self,
            WINDOW_TITLE,
            (
                f"Create private GitHub repos for {len(folder_paths)} folders, "
                f"initialize git where needed, and push an '{INIT_COMMIT_MESSAGE}' commit?"
            ),
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        results: list[RepoResult] = []
        for folder_path in folder_paths:
            QApplication.processEvents()
            results.append(self._create_repo_for_folder(folder_path))

        created_count = sum(result.status == "created" for result in results)
        pushed_count = sum(result.status == "pushed" for result in results)
        skipped_count = sum(result.status == "skipped" for result in results)
        failed_results = [result for result in results if result.status == "failed"]

        summary = (
            f"Created {created_count}, pushed {pushed_count}, skipped {skipped_count}, failed {len(failed_results)}"
        )
        details = "\n".join(
            f"{result.name}: {result.details}" for result in results[:20]
        )
        if failed_results:
            QMessageBox.warning(self, WINDOW_TITLE, f"{summary}\n\n{details}")
        else:
            QMessageBox.information(self, WINDOW_TITLE, f"{summary}\n\n{details}")
        self.activity_label.setText(summary)
        self._load_items()

    def _create_repo_for_folder(self, folder_path: Path) -> RepoResult:
        try:
            if not (folder_path / ".git").exists():
                run_command(["git", "init", "-b", DEFAULT_BRANCH_NAME], folder_path)

            head_exists = command_succeeded(
                ["git", "rev-parse", "--verify", "HEAD"], folder_path
            )
            if not head_exists:
                run_command(["git", "add", "-A"], folder_path)
                run_command(
                    ["git", "commit", "--allow-empty", "-m", INIT_COMMIT_MESSAGE],
                    folder_path,
                )
            elif is_git_worktree_dirty(folder_path):
                return RepoResult(
                    folder_path.name,
                    "skipped",
                    "existing git history has uncommitted changes",
                )

            if not command_succeeded(["git", "remote", "get-url", "origin"], folder_path):
                run_command(
                    [
                        "gh",
                        "repo",
                        "create",
                        folder_path.name,
                        GH_REPO_VISIBILITY_FLAG,
                        "--source",
                        ".",
                        "--remote",
                        "origin",
                        "--push",
                    ],
                    folder_path,
                )
                return RepoResult(
                    folder_path.name,
                    "created",
                    "private repo created and pushed",
                )

            branch_name = get_current_branch_name(folder_path)
            if not branch_name:
                return RepoResult(
                    folder_path.name,
                    "skipped",
                    "no current branch available for push",
                )
            run_command(["git", "push", "-u", "origin", branch_name], folder_path)
            return RepoResult(folder_path.name, "pushed", f"pushed {branch_name}")
        except RuntimeError as error:
            return RepoResult(folder_path.name, "failed", str(error))


def get_current_folders() -> list[Path]:
    return sorted(
        [
            path
            for path in APP_DIR.iterdir()
            if path.is_dir() and path.name not in EXCLUDED_FOLDER_NAMES
        ],
        key=lambda path: path.name.lower(),
    )


def get_current_folder_names() -> list[str]:
    return [path.name for path in get_current_folders()]


def get_folder_size_details(folder_path: Path) -> tuple[int, list[tuple[str, int]]]:
    total_size = 0
    sizes_by_type: dict[str, int] = {}
    for current_root, dir_names, file_names in os.walk(folder_path):
        dir_names[:] = [
            dir_name for dir_name in dir_names if dir_name not in EXCLUDED_SCAN_DIR_NAMES
        ]
        for file_name in file_names:
            file_path = Path(current_root, file_name)
            if file_path.is_symlink():
                continue
            try:
                file_size = file_path.stat().st_size
            except OSError:
                continue
            total_size += file_size
            file_type = file_path.suffix.lower() or NO_EXTENSION_LABEL
            sizes_by_type[file_type] = sizes_by_type.get(file_type, 0) + file_size
    top_types = sorted(sizes_by_type.items(), key=lambda item: (-item[1], item[0]))[:3]
    return total_size, top_types


def format_size(size_in_bytes: int) -> str:
    size = float(size_in_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size_in_bytes} B"


def format_top_types(total_size: int, top_types: list[tuple[str, int]]) -> str:
    if not total_size or not top_types:
        return "No files"
    return " | ".join(
        f"{file_type} {file_size / total_size * 100:.1f}%"
        for file_type, file_size in top_types
    )


def normalize_folder_name(folder_name: str) -> str:
    normalized_name = re.sub(r"\s+", "-", folder_name.strip().lower())
    normalized_name = re.sub(r"-{2,}", "-", normalized_name)
    return normalized_name or folder_name


def rename_folder_path(source_path: Path, target_path: Path) -> None:
    if source_path == target_path:
        return
    if source_path.name.lower() == target_path.name.lower():
        temporary_path = get_temporary_rename_path(source_path.parent, target_path.name)
        source_path.rename(temporary_path)
        temporary_path.rename(target_path)
        return
    source_path.rename(target_path)


def get_temporary_rename_path(parent_path: Path, target_name: str) -> Path:
    temporary_path = parent_path / f"{target_name}.tmp-rename"
    suffix = 1
    while temporary_path.exists():
        temporary_path = parent_path / f"{target_name}.tmp-rename-{suffix}"
        suffix += 1
    return temporary_path


def run_command(command: list[str], working_path: Path) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=working_path,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"{command[0]} is not installed or not on PATH") from error
    if result.returncode == 0:
        return result
    error_output = result.stderr.strip() or result.stdout.strip() or "command failed"
    raise RuntimeError(f"{' '.join(command)}: {error_output}")


def command_succeeded(command: list[str], working_path: Path) -> bool:
    try:
        result = subprocess.run(
            command,
            cwd=working_path,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def is_git_worktree_dirty(folder_path: Path) -> bool:
    result = run_command(["git", "status", "--porcelain"], folder_path)
    return bool(result.stdout.strip())


def get_current_branch_name(folder_path: Path) -> str:
    result = run_command(["git", "branch", "--show-current"], folder_path)
    return result.stdout.strip()


def main() -> int:
    store = FolderStore(DB_PATH)
    store.seed_if_empty(get_current_folder_names())
    app = QApplication(sys.argv)
    window = FolderToggleWindow(store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

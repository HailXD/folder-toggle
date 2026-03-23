import os
import re
import sqlite3
import subprocess
import sys
import traceback
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "test.db"
TABLE_NAME = "folders"
DEFAULT_BRANCH_NAME = "main"
INIT_COMMIT_MESSAGE = "init"
REPO_VISIBILITY_PRIVATE = "private"
REPO_VISIBILITY_PUBLIC = "public"
REPO_VISIBILITY_VALUES = [REPO_VISIBILITY_PRIVATE, REPO_VISIBILITY_PUBLIC]
SORT_NAME_ASC = "name_asc"
SORT_NAME_DESC = "name_desc"
SORT_SIZE_DESC = "size_desc"
SORT_SIZE_ASC = "size_asc"
SORT_FILTERED_SIZE_DESC = "filtered_size_desc"
SORT_FILTERED_SIZE_ASC = "filtered_size_asc"
SORT_CHOICES = [
    SORT_NAME_ASC,
    SORT_NAME_DESC,
    SORT_SIZE_DESC,
    SORT_SIZE_ASC,
    SORT_FILTERED_SIZE_DESC,
    SORT_FILTERED_SIZE_ASC,
]
EXCLUDED_FOLDER_NAMES = {".git", "__pycache__"}
EXCLUDED_SCAN_DIR_NAMES = {".git", "__pycache__"}
NO_EXTENSION_LABEL = "[no ext]"
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
GITIGNORE_FILE_NAME = ".gitignore"
GITIGNORE_WILDCARD_PREFIX = "*"
NODE_MODULES_IGNORE_PATTERN = "node_modules/"
PYC_IGNORE_PATTERN = "*.pyc"
ERROR_LOG_PATH = APP_DIR / "folder-toggle-errors.log"
MENU_SEPARATOR = "-" * 110
TABLE_NAME_WIDTH = 26
TABLE_SIZE_WIDTH = 10
TABLE_REPO_WIDTH = 7
TABLE_ENABLED_WIDTH = 7
TOP_TYPE_COUNT = 3


@dataclass(slots=True)
class RepoResult:
    name: str
    status: str
    details: str


@dataclass(slots=True)
class FolderMetrics:
    total_size: int
    filtered_size: int
    top_types: list[tuple[str, int]]


@dataclass(slots=True)
class IgnoreRule:
    pattern: str
    negated: bool
    directory_only: bool
    rooted: bool


@dataclass(slots=True)
class FolderView:
    path: Path
    enabled: bool
    repo_visibility: str
    metrics: FolderMetrics


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
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                repo_visibility TEXT NOT NULL DEFAULT '{REPO_VISIBILITY_PRIVATE}'
                    CHECK(repo_visibility IN ('{REPO_VISIBILITY_PRIVATE}', '{REPO_VISIBILITY_PUBLIC}'))
            )
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute(
                f"PRAGMA table_info({TABLE_NAME})"
            ).fetchall()
        }
        if "repo_visibility" not in columns:
            self.connection.execute(
                f"""
                ALTER TABLE {TABLE_NAME}
                ADD COLUMN repo_visibility TEXT NOT NULL DEFAULT '{REPO_VISIBILITY_PRIVATE}'
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

    def fetch_all(self) -> list[tuple[str, bool, str]]:
        rows = self.connection.execute(
            f"""
            SELECT name, enabled, repo_visibility
            FROM {TABLE_NAME}
            ORDER BY LOWER(name), name
            """
        ).fetchall()
        return [
            (row["name"], bool(row["enabled"]), row["repo_visibility"]) for row in rows
        ]

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

    def set_repo_visibility(self, name: str, repo_visibility: str) -> None:
        self.connection.execute(
            f"UPDATE {TABLE_NAME} SET repo_visibility = ? WHERE name = ?",
            (repo_visibility, name),
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


class FolderToggleCli:
    def __init__(self, store: FolderStore) -> None:
        self.store = store
        self.sort_mode = SORT_NAME_ASC
        self.folder_views: list[FolderView] = []
        self.load_errors: list[str] = []
        self.needs_refresh = True

    def run(self) -> int:
        while True:
            if self.needs_refresh:
                self.refresh()
            self.render()
            choice = input("Select action: ").strip().lower()
            if choice == "0":
                return 0
            if choice == "1":
                self.needs_refresh = True
                continue
            if choice == "2":
                self.change_sort()
                continue
            if choice == "3":
                self.toggle_folder()
                continue
            if choice == "4":
                self.set_all_enabled(True)
                continue
            if choice == "5":
                self.set_all_enabled(False)
                continue
            if choice == "6":
                self.toggle_all()
                continue
            if choice == "7":
                self.set_repo_visibility()
                continue
            if choice == "8":
                self.normalize_folder_names()
                continue
            if choice == "9":
                self.create_repos()
                continue
            if choice == "10":
                self.edit_gitignore()
                continue
            if choice == "11":
                self.ignore_top_file_type()
                continue
            if choice == "12":
                self.add_common_ignores()
                continue
            print("Unknown action.")
            pause()

    def refresh(self) -> None:
        self.folder_views = []
        self.load_errors = []
        folder_paths = get_current_folders()
        self.store.sync_folder_names(path.name for path in folder_paths)
        folder_rows = self.store.fetch_all()
        enabled_by_name = {
            name: enabled for name, enabled, _repo_visibility in folder_rows
        }
        visibility_by_name = {
            name: repo_visibility
            for name, _enabled, repo_visibility in folder_rows
        }
        for folder_path in folder_paths:
            try:
                metrics = get_folder_metrics(folder_path)
                self.folder_views.append(
                    FolderView(
                        path=folder_path,
                        enabled=enabled_by_name.get(folder_path.name, False),
                        repo_visibility=visibility_by_name.get(
                            folder_path.name, REPO_VISIBILITY_PRIVATE
                        ),
                        metrics=metrics,
                    )
                )
            except Exception as error:
                self.load_errors.append(f"{folder_path.name}: {error}")
                log_exception(error, f"refresh folder {folder_path}")
        self.apply_sort()
        self.needs_refresh = False

    def apply_sort(self) -> None:
        if self.sort_mode == SORT_NAME_DESC:
            self.folder_views.sort(
                key=lambda folder: folder.path.name.lower(), reverse=True
            )
            return
        if self.sort_mode == SORT_SIZE_DESC:
            self.folder_views.sort(
                key=lambda folder: (-folder.metrics.total_size, folder.path.name.lower())
            )
            return
        if self.sort_mode == SORT_SIZE_ASC:
            self.folder_views.sort(
                key=lambda folder: (folder.metrics.total_size, folder.path.name.lower())
            )
            return
        if self.sort_mode == SORT_FILTERED_SIZE_DESC:
            self.folder_views.sort(
                key=lambda folder: (
                    -folder.metrics.filtered_size,
                    folder.path.name.lower(),
                )
            )
            return
        if self.sort_mode == SORT_FILTERED_SIZE_ASC:
            self.folder_views.sort(
                key=lambda folder: (
                    folder.metrics.filtered_size,
                    folder.path.name.lower(),
                )
            )
            return
        self.folder_views.sort(key=lambda folder: folder.path.name.lower())

    def render(self) -> None:
        clear_screen()
        print(f"Folder Toggle CLI  DB: {DB_PATH.name}  Sort: {self.sort_mode}")
        total_size = sum(folder.metrics.total_size for folder in self.folder_views)
        filtered_size = sum(folder.metrics.filtered_size for folder in self.folder_views)
        enabled_count = sum(folder.enabled for folder in self.folder_views)
        print(
            f"{enabled_count} enabled / {len(self.folder_views)} folders   "
            f"{format_size(total_size)} total   {format_size(filtered_size)} filtered"
        )
        if self.load_errors:
            print(f"Skipped {len(self.load_errors)} folders. Details: {ERROR_LOG_PATH}")
        print(MENU_SEPARATOR)
        print(
            f"{'#':>3}  {'On':<{TABLE_ENABLED_WIDTH}}  "
            f"{'Folder':<{TABLE_NAME_WIDTH}}  {'Size':>{TABLE_SIZE_WIDTH}}  "
            f"{'Filtered':>{TABLE_SIZE_WIDTH}}  {'Repo':<{TABLE_REPO_WIDTH}}  Top Types"
        )
        print(MENU_SEPARATOR)
        for index, folder in enumerate(self.folder_views, start=1):
            enabled_label = "yes" if folder.enabled else "no"
            folder_name = truncate_text(folder.path.name, TABLE_NAME_WIDTH)
            top_types_text = format_top_types(
                folder.metrics.total_size, folder.metrics.top_types
            )
            print(
                f"{index:>3}  {enabled_label:<{TABLE_ENABLED_WIDTH}}  "
                f"{folder_name:<{TABLE_NAME_WIDTH}}  "
                f"{format_size(folder.metrics.total_size):>{TABLE_SIZE_WIDTH}}  "
                f"{format_size(folder.metrics.filtered_size):>{TABLE_SIZE_WIDTH}}  "
                f"{folder.repo_visibility:<{TABLE_REPO_WIDTH}}  {top_types_text}"
            )
        print(MENU_SEPARATOR)
        print("1 Refresh")
        print("2 Change Sort")
        print("3 Toggle Folder Enabled")
        print("4 Enable All")
        print("5 Disable All")
        print("6 Toggle All")
        print("7 Set Repo Visibility")
        print("8 Normalize Folder Names")
        print("9 Create Repos")
        print("10 Edit .gitignore")
        print("11 Add Top File Type To .gitignore")
        print("12 Add node_modules and *.pyc")
        print("0 Exit")

    def change_sort(self) -> None:
        print("Sort choices:")
        for index, sort_choice in enumerate(SORT_CHOICES, start=1):
            print(f"{index} {sort_choice}")
        selected_index = prompt_index(len(SORT_CHOICES), "Sort number")
        if selected_index is None:
            return
        self.sort_mode = SORT_CHOICES[selected_index]
        self.apply_sort()

    def toggle_folder(self) -> None:
        folder = self.select_folder("Folder number to toggle")
        if folder is None:
            return
        folder.enabled = not folder.enabled
        self.store.set_enabled(folder.path.name, folder.enabled)

    def set_all_enabled(self, enabled: bool) -> None:
        self.store.set_all_enabled(
            (enabled, folder.path.name) for folder in self.folder_views
        )
        for folder in self.folder_views:
            folder.enabled = enabled

    def toggle_all(self) -> None:
        updates: list[tuple[bool, str]] = []
        for folder in self.folder_views:
            folder.enabled = not folder.enabled
            updates.append((folder.enabled, folder.path.name))
        self.store.set_all_enabled(updates)

    def set_repo_visibility(self) -> None:
        folder = self.select_folder("Folder number for repo visibility")
        if folder is None:
            return
        print("1 private")
        print("2 public")
        selected_index = prompt_index(2, "Visibility number")
        if selected_index is None:
            return
        repo_visibility = REPO_VISIBILITY_VALUES[selected_index]
        folder.repo_visibility = repo_visibility
        self.store.set_repo_visibility(folder.path.name, repo_visibility)

    def normalize_folder_names(self) -> None:
        rename_pairs: list[tuple[Path, Path]] = []
        skipped_names: list[str] = []
        planned_targets: set[str] = set()
        for folder in self.folder_views:
            normalized_name = normalize_folder_name(folder.path.name)
            if normalized_name == folder.path.name:
                continue
            target_path = folder.path.with_name(normalized_name)
            if normalized_name in planned_targets:
                skipped_names.append(folder.path.name)
                continue
            if target_path.exists() and target_path != folder.path:
                skipped_names.append(folder.path.name)
                continue
            rename_pairs.append((folder.path, target_path))
            planned_targets.add(normalized_name)
        if not rename_pairs:
            print("No folder names need normalization.")
            if skipped_names:
                print(f"Conflicts: {', '.join(skipped_names)}")
            pause()
            return
        if not prompt_yes_no(f"Rename {len(rename_pairs)} folders", True):
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
        print(f"Renamed {renamed_count} folders.")
        if skipped_names:
            print(f"Skipped conflicts: {', '.join(skipped_names)}")
        if errors:
            print("Errors:")
            for error_text in errors:
                print(error_text)
        self.needs_refresh = True
        pause()

    def create_repos(self) -> None:
        if not self.folder_views:
            print("No folders found.")
            pause()
            return
        invalid_names = [
            folder.path.name
            for folder in self.folder_views
            if not REPO_NAME_PATTERN.fullmatch(folder.path.name)
        ]
        if invalid_names:
            print("Invalid repo names:")
            for name in invalid_names:
                print(name)
            pause()
            return
        try:
            run_command(["git", "--version"], APP_DIR)
            run_command(["gh", "--version"], APP_DIR)
            run_command(["gh", "auth", "status"], APP_DIR)
        except RuntimeError as error:
            print(error)
            pause()
            return
        scope = prompt_text("Create repos for (all/enabled) [enabled]: ").strip().lower()
        if not scope:
            scope = "enabled"
        target_folders = (
            [folder for folder in self.folder_views if folder.enabled]
            if scope == "enabled"
            else self.folder_views
        )
        if not target_folders:
            print("No folders matched that scope.")
            pause()
            return
        if not prompt_yes_no(
            f"Create repos for {len(target_folders)} folders with '{INIT_COMMIT_MESSAGE}' commit",
            False,
        ):
            return
        results: list[RepoResult] = []
        for folder in target_folders:
            print(f"Processing {folder.path.name}...")
            results.append(create_repo_for_folder(folder.path, folder.repo_visibility))
        print("Results:")
        for result in results:
            print(f"{result.name}: {result.status} - {result.details}")
        pause()
        self.needs_refresh = True

    def edit_gitignore(self) -> None:
        folder = self.select_folder("Folder number to edit .gitignore")
        if folder is None:
            return
        current_text = read_gitignore_text(folder.path)
        print(f"Editing {folder.path.name}\\{GITIGNORE_FILE_NAME}")
        print("Current content:")
        print(MENU_SEPARATOR)
        if current_text:
            print(current_text, end="" if current_text.endswith("\n") else "\n")
        print(MENU_SEPARATOR)
        print("Enter new content. Type ':wq' on its own line to save, ':q' to cancel.")
        new_lines: list[str] = []
        while True:
            line = input()
            if line == ":q":
                return
            if line == ":wq":
                break
            new_lines.append(line)
        write_gitignore_text(folder.path, "\n".join(new_lines))
        self.needs_refresh = True

    def ignore_top_file_type(self) -> None:
        folder = self.select_folder("Folder number for top file type ignore")
        if folder is None:
            return
        extension_types = [
            file_type
            for file_type, _file_size in folder.metrics.top_types
            if file_type.startswith(".")
        ]
        if not extension_types:
            print("No extension-based top file types available.")
            pause()
            return
        for index, file_type in enumerate(extension_types, start=1):
            print(f"{index} {file_type}")
        selected_index = prompt_index(len(extension_types), "Top file type number")
        if selected_index is None:
            return
        ignore_pattern = build_extension_ignore_pattern(extension_types[selected_index])
        if add_gitignore_pattern(folder.path, ignore_pattern):
            print(f"Added {ignore_pattern}")
        else:
            print(f"{ignore_pattern} already exists")
        self.needs_refresh = True
        pause()

    def add_common_ignores(self) -> None:
        updated_folders: list[str] = []
        added_patterns = 0
        for folder in self.folder_views:
            patterns = get_present_common_ignore_patterns(folder.path)
            if not patterns:
                continue
            folder_updated = False
            for pattern in patterns:
                if add_gitignore_pattern(folder.path, pattern):
                    added_patterns += 1
                    folder_updated = True
            if folder_updated:
                updated_folders.append(folder.path.name)
        if not updated_folders:
            print("No folders needed node_modules/ or *.pyc added.")
            pause()
            return
        print(
            f"Updated {len(updated_folders)} folders with {added_patterns} ignore patterns."
        )
        self.needs_refresh = True
        pause()

    def select_folder(self, prompt_label: str) -> FolderView | None:
        if not self.folder_views:
            print("No folders available.")
            pause()
            return None
        selected_index = prompt_index(len(self.folder_views), prompt_label)
        if selected_index is None:
            return None
        return self.folder_views[selected_index]


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


def get_folder_metrics(folder_path: Path) -> FolderMetrics:
    ignore_rules = load_gitignore_rules(folder_path)
    total_size = 0
    filtered_size = 0
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
            relative_path = file_path.relative_to(folder_path).as_posix()
            if not matches_gitignore(relative_path, ignore_rules):
                filtered_size += file_size
    top_types = sorted(sizes_by_type.items(), key=lambda item: (-item[1], item[0]))[
        :TOP_TYPE_COUNT
    ]
    return FolderMetrics(total_size, filtered_size, top_types)


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


def get_repo_visibility_flag(repo_visibility: str) -> str:
    return "--public" if repo_visibility == REPO_VISIBILITY_PUBLIC else "--private"


def read_gitignore_text(folder_path: Path) -> str:
    gitignore_path = folder_path / GITIGNORE_FILE_NAME
    if not gitignore_path.exists():
        return ""
    try:
        return gitignore_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def write_gitignore_text(folder_path: Path, text: str) -> None:
    gitignore_path = folder_path / GITIGNORE_FILE_NAME
    normalized_text = text.replace("\r\n", "\n")
    if normalized_text and not normalized_text.endswith("\n"):
        normalized_text = f"{normalized_text}\n"
    gitignore_path.write_text(normalized_text, encoding="utf-8")


def add_gitignore_pattern(folder_path: Path, pattern: str) -> bool:
    lines = read_gitignore_lines(folder_path)
    if pattern in lines:
        return False
    lines.append(pattern)
    write_gitignore_text(folder_path, "\n".join(lines))
    return True


def read_gitignore_lines(folder_path: Path) -> list[str]:
    text = read_gitignore_text(folder_path)
    if not text:
        return []
    normalized_text = text.replace("\r\n", "\n")
    return (
        normalized_text.split("\n")[:-1]
        if normalized_text.endswith("\n")
        else normalized_text.split("\n")
    )


def build_extension_ignore_pattern(file_type: str) -> str:
    return f"{GITIGNORE_WILDCARD_PREFIX}{file_type}"


def get_present_common_ignore_patterns(folder_path: Path) -> list[str]:
    has_node_modules = False
    has_pyc = False
    for current_root, dir_names, file_names in os.walk(folder_path):
        if "node_modules" in dir_names:
            has_node_modules = True
        dir_names[:] = [
            dir_name
            for dir_name in dir_names
            if dir_name not in EXCLUDED_SCAN_DIR_NAMES and dir_name != "node_modules"
        ]
        if not has_pyc and any(file_name.endswith(".pyc") for file_name in file_names):
            has_pyc = True
        if has_node_modules and has_pyc:
            break
    patterns: list[str] = []
    if has_node_modules:
        patterns.append(NODE_MODULES_IGNORE_PATTERN)
    if has_pyc:
        patterns.append(PYC_IGNORE_PATTERN)
    return patterns


def load_gitignore_rules(folder_path: Path) -> list[IgnoreRule]:
    rules: list[IgnoreRule] = []
    for raw_line in read_gitignore_lines(folder_path):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        rooted = line.startswith("/")
        if rooted:
            line = line[1:]
        directory_only = line.endswith("/")
        if directory_only:
            line = line[:-1]
        line = line.strip()
        if line:
            rules.append(
                IgnoreRule(line.replace("\\", "/"), negated, directory_only, rooted)
            )
    return rules


def matches_gitignore(relative_path: str, rules: list[IgnoreRule]) -> bool:
    normalized_path = relative_path.strip("/").replace("\\", "/")
    if not normalized_path:
        return False
    ignored = False
    for rule in rules:
        candidates = get_path_candidates(normalized_path)
        if any(rule_matches_path(rule, candidate) for candidate in candidates):
            ignored = not rule.negated
    return ignored


def get_directory_candidates(relative_path: str) -> list[str]:
    parts = PurePosixPath(relative_path).parts
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def get_path_candidates(relative_path: str) -> list[str]:
    return [relative_path, *get_directory_candidates(relative_path)]


def rule_matches_path(rule: IgnoreRule, relative_path: str) -> bool:
    path_parts = PurePosixPath(relative_path).parts
    candidates = get_match_candidates(relative_path, rule.rooted)
    if "/" not in rule.pattern and not rule.rooted:
        return any(fnmatch(path_part, rule.pattern) for path_part in path_parts)
    return any(fnmatch(candidate, rule.pattern) for candidate in candidates)


def get_match_candidates(relative_path: str, rooted: bool) -> list[str]:
    if rooted:
        return [relative_path]
    parts = PurePosixPath(relative_path).parts
    return ["/".join(parts[index:]) for index in range(len(parts))]


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


def create_repo_for_folder(folder_path: Path, repo_visibility: str) -> RepoResult:
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
                    get_repo_visibility_flag(repo_visibility),
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
                f"{repo_visibility} repo created and pushed",
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


def truncate_text(text: str, width: int) -> str:
    return text if len(text) <= width else f"{text[: width - 3]}..."


def prompt_index(max_count: int, label: str) -> int | None:
    raw_value = prompt_text(f"{label} (1-{max_count}, blank to cancel): ").strip()
    if not raw_value:
        return None
    if not raw_value.isdigit():
        print("Please enter a number.")
        pause()
        return None
    selected_number = int(raw_value)
    if selected_number < 1 or selected_number > max_count:
        print("Number out of range.")
        pause()
        return None
    return selected_number - 1


def prompt_yes_no(label: str, default_yes: bool) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    raw_value = prompt_text(f"{label} {suffix} ").strip().lower()
    if not raw_value:
        return default_yes
    return raw_value in {"y", "yes"}


def prompt_text(label: str) -> str:
    return input(label)


def pause() -> None:
    input("Press Enter to continue...")


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def log_exception(error: Exception, context: str) -> None:
    try:
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as file_handle:
            file_handle.write(f"[{context}] {type(error).__name__}: {error}\n")
            file_handle.write(traceback.format_exc())
            file_handle.write("\n")
    except OSError:
        return


def handle_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback,
) -> None:
    try:
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as file_handle:
            file_handle.write("[uncaught exception]\n")
            file_handle.write(
                "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            )
            file_handle.write("\n")
    except OSError:
        return


def main() -> int:
    sys.excepthook = handle_uncaught_exception
    store = FolderStore(DB_PATH)
    store.seed_if_empty(get_current_folder_names())
    cli = FolderToggleCli(store)
    try:
        return cli.run()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

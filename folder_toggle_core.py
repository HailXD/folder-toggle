import fnmatch
import os
import shutil
import sqlite3
import subprocess
import traceback
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "test.db"
ERROR_LOG_PATH = APP_DIR / "folder-toggle-errors.log"
DEFAULT_VISIBILITY = "private"
DEFAULT_SORT = "name_asc"
EXCLUDED_FOLDER_NAMES = {".git", "__pycache__", ".venv", "venv"}
COMMON_IGNORE_PATTERNS = ("node_modules/", "*.pyc")
VISIBILITIES = ("private", "public")
SORT_LABELS = {
    "name_asc": "name ascending",
    "name_desc": "name descending",
    "size_desc": "size largest first",
    "size_asc": "size smallest first",
    "filtered_desc": "filtered size largest first",
    "filtered_asc": "filtered size smallest first",
}


@dataclass(slots=True)
class FolderInfo:
    name: str
    path: Path
    enabled: bool
    visibility: str
    size: int
    filtered_size: int
    top_file_types: list[tuple[str, float]]


class GitIgnoreRules:
    def __init__(self, patterns: list[str]) -> None:
        self.patterns = patterns

    @classmethod
    def from_folder(cls, folder: Path) -> "GitIgnoreRules":
        return cls(read_gitignore_patterns(folder))

    def is_ignored(self, relative_path: PurePosixPath, is_dir: bool) -> bool:
        ignored = False
        relative_text = relative_path.as_posix()
        parts = relative_path.parts
        for raw_pattern in self.patterns:
            negated = raw_pattern.startswith("!")
            pattern = raw_pattern[1:] if negated else raw_pattern
            if pattern.startswith("/"):
                pattern = pattern[1:]
            if not pattern:
                continue
            directory_only = pattern.endswith("/")
            core_pattern = pattern.rstrip("/")
            if not core_pattern or directory_only and not is_dir:
                continue
            if self._matches(core_pattern, relative_text, parts, directory_only):
                ignored = not negated
        return ignored

    def _matches(
        self,
        pattern: str,
        relative_text: str,
        parts: tuple[str, ...],
        directory_only: bool,
    ) -> bool:
        if "/" in pattern:
            if fnmatch.fnmatch(relative_text, pattern):
                return True
            return directory_only and relative_text.startswith(f"{pattern}/")
        if fnmatch.fnmatch(relative_text, pattern):
            return True
        return any(fnmatch.fnmatch(part, pattern) for part in parts)


def ensure_database() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS folders (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                visibility TEXT NOT NULL DEFAULT 'private'
            )
            """
        )


def log_error(context: str, error: BaseException) -> None:
    with ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{context}] {error}\n")
        handle.write(traceback.format_exc())
        handle.write("\n")


def list_project_folders() -> list[Path]:
    folders: list[Path] = []
    for entry in APP_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in EXCLUDED_FOLDER_NAMES or entry.name.startswith("."):
            continue
        folders.append(entry)
    return sorted(folders, key=lambda item: item.name.lower())


def sync_folders() -> None:
    ensure_database()
    folder_names = [folder.name for folder in list_project_folders()]
    with sqlite3.connect(DB_PATH) as connection:
        existing_rows = connection.execute("SELECT name FROM folders").fetchall()
        existing_names = {row[0] for row in existing_rows}
        for folder_name in folder_names:
            if folder_name not in existing_names:
                connection.execute(
                    "INSERT INTO folders(name, enabled, visibility) VALUES (?, 1, ?)",
                    (folder_name, DEFAULT_VISIBILITY),
                )
        for stale_name in existing_names.difference(folder_names):
            connection.execute("DELETE FROM folders WHERE name = ?", (stale_name,))


def get_folder_infos(sort_key: str = DEFAULT_SORT) -> list[FolderInfo]:
    sync_folders()
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            "SELECT name, enabled, visibility FROM folders"
        ).fetchall()
    folders: list[FolderInfo] = []
    for name, enabled, visibility in rows:
        folder_path = APP_DIR / name
        if not folder_path.is_dir():
            continue
        stats = read_folder_stats(folder_path)
        if stats is None:
            continue
        size, filtered_size, top_file_types = stats
        folders.append(
            FolderInfo(
                name=name,
                path=folder_path,
                enabled=bool(enabled),
                visibility=visibility,
                size=size,
                filtered_size=filtered_size,
                top_file_types=top_file_types,
            )
        )
    return sort_folder_infos(folders, sort_key)


def read_folder_stats(folder: Path) -> tuple[int, int, list[tuple[str, float]]] | None:
    try:
        rules = GitIgnoreRules.from_folder(folder)
        total_size = 0
        filtered_size = 0
        file_type_sizes: dict[str, int] = {}
        for root, dir_names, file_names in os.walk(folder, topdown=True):
            root_path = Path(root)
            relative_root = root_path.relative_to(folder)
            kept_dirs: list[str] = []
            for dir_name in dir_names:
                relative_dir = PurePosixPath(relative_root.as_posix(), dir_name)
                if not rules.is_ignored(relative_dir, True):
                    kept_dirs.append(dir_name)
            dir_names[:] = kept_dirs
            for file_name in file_names:
                file_path = root_path / file_name
                try:
                    size = file_path.stat().st_size
                except OSError:
                    continue
                relative_file = file_path.relative_to(folder).as_posix()
                total_size += size
                label = file_path.suffix.lower() or "[no extension]"
                file_type_sizes[label] = file_type_sizes.get(label, 0) + size
                if not rules.is_ignored(PurePosixPath(relative_file), False):
                    filtered_size += size
        return total_size, filtered_size, build_top_file_types(file_type_sizes, total_size)
    except OSError as error:
        log_error(f"read_folder_stats:{folder.name}", error)
        return None


def build_top_file_types(
    file_type_sizes: dict[str, int], total_size: int
) -> list[tuple[str, float]]:
    if total_size <= 0:
        return []
    ordered = sorted(file_type_sizes.items(), key=lambda item: (-item[1], item[0]))
    return [(label, size * 100 / total_size) for label, size in ordered[:3]]


def sort_folder_infos(folders: list[FolderInfo], sort_key: str) -> list[FolderInfo]:
    sorters = {
        "name_asc": lambda item: (item.name.lower(),),
        "name_desc": lambda item: (item.name.lower(),),
        "size_desc": lambda item: (-item.size, item.name.lower()),
        "size_asc": lambda item: (item.size, item.name.lower()),
        "filtered_desc": lambda item: (-item.filtered_size, item.name.lower()),
        "filtered_asc": lambda item: (item.filtered_size, item.name.lower()),
    }
    active_sort = sort_key if sort_key in sorters else DEFAULT_SORT
    reverse = active_sort == "name_desc"
    return sorted(folders, key=sorters[active_sort], reverse=reverse)


def read_gitignore_patterns(folder: Path) -> list[str]:
    gitignore_path = folder / ".gitignore"
    if not gitignore_path.is_file():
        return []
    try:
        content = gitignore_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    patterns: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def read_gitignore_text(folder_name: str) -> str:
    folder_path = APP_DIR / folder_name
    gitignore_path = folder_path / ".gitignore"
    if not gitignore_path.is_file():
        return ""
    try:
        return gitignore_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def write_gitignore_text(folder_name: str, content: str) -> str:
    folder_path = APP_DIR / folder_name
    gitignore_path = folder_path / ".gitignore"
    try:
        text = content.rstrip("\n")
        if text:
            gitignore_path.write_text(f"{text}\n", encoding="utf-8")
        elif gitignore_path.exists():
            gitignore_path.write_text("", encoding="utf-8")
        return f"Updated {folder_name} .gitignore"
    except OSError as error:
        log_error(f"write_gitignore_text:{folder_name}", error)
        return f"Failed to update {folder_name} .gitignore: {error}"


def add_ignore_pattern(folder_name: str, pattern: str) -> str:
    folder_path = APP_DIR / folder_name
    existing_patterns = read_gitignore_patterns(folder_path)
    if pattern in existing_patterns:
        return f"{folder_name} already has {pattern}"
    current_text = read_gitignore_text(folder_name).rstrip("\n")
    lines = [current_text] if current_text else []
    lines.append(pattern)
    return write_gitignore_text(folder_name, "\n".join(lines))


def set_folder_enabled(folder_name: str, enabled: bool) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            "UPDATE folders SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, folder_name),
        )


def toggle_folder_enabled(folder_name: str) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT enabled FROM folders WHERE name = ?", (folder_name,)
        ).fetchone()
        if row is None:
            return
        connection.execute(
            "UPDATE folders SET enabled = ? WHERE name = ?",
            (0 if row[0] else 1, folder_name),
        )


def set_all_enabled(enabled: bool) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("UPDATE folders SET enabled = ?", (1 if enabled else 0,))


def toggle_all_enabled() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("UPDATE folders SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END")


def set_folder_visibility(folder_name: str, visibility: str) -> str:
    if visibility not in VISIBILITIES:
        return f"Invalid visibility: {visibility}"
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            "UPDATE folders SET visibility = ? WHERE name = ?",
            (visibility, folder_name),
        )
    return f"Set {folder_name} visibility to {visibility}"


def normalize_folder_name(name: str) -> str:
    return name.lower().replace(" ", "-")


def normalize_folder_names() -> list[str]:
    sync_folders()
    messages: list[str] = []
    for folder in list_project_folders():
        target_name = normalize_folder_name(folder.name)
        if target_name == folder.name:
            continue
        target_path = APP_DIR / target_name
        if target_path.exists() and target_path != folder:
            messages.append(f"Skipped {folder.name}: {target_name} already exists")
            continue
        try:
            if folder.name.lower() == target_name.lower():
                temp_path = APP_DIR / f"{target_name}.folder-toggle-temp"
                while temp_path.exists():
                    temp_path = APP_DIR / f"{temp_path.name}-1"
                folder.rename(temp_path)
                temp_path.rename(target_path)
            else:
                folder.rename(target_path)
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute("DELETE FROM folders WHERE name = ?", (target_name,))
                connection.execute(
                    "UPDATE folders SET name = ? WHERE name = ?",
                    (target_name, folder.name),
                )
            messages.append(f"Renamed {folder.name} -> {target_name}")
        except OSError as error:
            log_error(f"normalize_folder_names:{folder.name}", error)
            messages.append(f"Failed to rename {folder.name}: {error}")
    sync_folders()
    return messages or ["No folders needed renaming"]


def add_top_file_type_pattern(folder_name: str) -> str:
    folder_path = APP_DIR / folder_name
    stats = read_folder_stats(folder_path)
    if stats is None:
        return f"Unable to read stats for {folder_name}"
    _, _, top_file_types = stats
    for label, _percentage in top_file_types:
        if label.startswith("."):
            return add_ignore_pattern(folder_name, f"*{label}")
    return f"No extension-based file type found for {folder_name}"


def add_common_ignore_patterns(folder_name: str) -> list[str]:
    folder_path = APP_DIR / folder_name
    found_patterns: list[str] = []
    has_node_modules = False
    has_pyc = False
    for root, dir_names, file_names in os.walk(folder_path):
        if "node_modules" in dir_names:
            has_node_modules = True
        if any(file_name.endswith(".pyc") for file_name in file_names):
            has_pyc = True
        if has_node_modules and has_pyc:
            break
    if has_node_modules:
        found_patterns.append(add_ignore_pattern(folder_name, COMMON_IGNORE_PATTERNS[0]))
    if has_pyc:
        found_patterns.append(add_ignore_pattern(folder_name, COMMON_IGNORE_PATTERNS[1]))
    return found_patterns or [f"No common ignore patterns found for {folder_name}"]


def human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"


def format_top_file_types(top_file_types: list[tuple[str, float]]) -> str:
    if not top_file_types:
        return "-"
    return ", ".join(f"{label} {percentage:.0f}%" for label, percentage in top_file_types)


def run_command(command: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        log_error(f"run_command:{' '.join(command)}", error)
        return False, str(error)
    output = completed.stdout.strip() or completed.stderr.strip()
    return completed.returncode == 0, output


def require_command(command_name: str) -> str | None:
    if shutil.which(command_name):
        return None
    return f"Missing required command: {command_name}"


def is_git_repo(folder: Path) -> bool:
    return (folder / ".git").exists()


def has_uncommitted_changes(folder: Path) -> bool:
    success, output = run_command(["git", "status", "--porcelain"], folder)
    return not success or bool(output.strip())


def has_git_head(folder: Path) -> bool:
    success, _output = run_command(["git", "rev-parse", "--verify", "HEAD"], folder)
    return success


def has_git_remote(folder: Path) -> bool:
    success, output = run_command(["git", "remote"], folder)
    if not success:
        return False
    return any(line.strip() == "origin" for line in output.splitlines())


def initialize_git_repo(folder_name: str) -> str:
    folder_path = APP_DIR / folder_name
    missing_command = require_command("git")
    if missing_command:
        return missing_command
    if is_git_repo(folder_path):
        return f"{folder_name} already has git initialized"
    steps = [
        ["git", "init"],
        ["git", "add", "."],
        ["git", "commit", "--allow-empty", "-m", "init"],
    ]
    for command in steps:
        success, output = run_command(command, folder_path)
        if not success:
            return f"Failed on {' '.join(command)} for {folder_name}: {output}"
    return f"Initialized git repo for {folder_name}"


def create_github_repo(folder_name: str) -> str:
    folder_path = APP_DIR / folder_name
    for command_name in ("git", "gh"):
        missing_command = require_command(command_name)
        if missing_command:
            return missing_command
    if not is_git_repo(folder_path):
        init_message = initialize_git_repo(folder_name)
        if not init_message.startswith("Initialized"):
            return init_message
    elif has_uncommitted_changes(folder_path):
        return f"Skipped {folder_name}: repo has uncommitted changes"
    elif not has_git_head(folder_path):
        success, output = run_command(
            ["git", "commit", "--allow-empty", "-m", "init"], folder_path
        )
        if not success:
            return f"Failed to create init commit for {folder_name}: {output}"
    visibility = get_folder_visibility(folder_name)
    if visibility not in VISIBILITIES:
        visibility = DEFAULT_VISIBILITY
    if has_git_remote(folder_path):
        return f"Skipped {folder_name}: origin already exists"
    command = [
        "gh",
        "repo",
        "create",
        folder_name,
        "--source",
        ".",
        "--remote",
        "origin",
        f"--{visibility}",
        "--push",
    ]
    success, output = run_command(command, folder_path)
    if not success:
        return f"Failed to create GitHub repo for {folder_name}: {output}"
    return f"Created GitHub repo for {folder_name}"


def push_repo(folder_name: str) -> str:
    folder_path = APP_DIR / folder_name
    missing_command = require_command("git")
    if missing_command:
        return missing_command
    if not is_git_repo(folder_path):
        return f"Skipped {folder_name}: no git repo"
    if has_uncommitted_changes(folder_path):
        return f"Skipped {folder_name}: repo has uncommitted changes"
    if not has_git_remote(folder_path):
        return f"Skipped {folder_name}: origin is missing"
    if not has_git_head(folder_path):
        return f"Skipped {folder_name}: repo has no commits"
    success, output = run_command(["git", "push", "-u", "origin", "HEAD"], folder_path)
    if not success:
        return f"Failed to push {folder_name}: {output}"
    return f"Pushed {folder_name} to origin"


def get_folder_visibility(folder_name: str) -> str:
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT visibility FROM folders WHERE name = ?", (folder_name,)
        ).fetchone()
    return row[0] if row else DEFAULT_VISIBILITY


def apply_to_folders(
    folder_names: list[str], action: Callable[[str], str | list[str]]
) -> list[str]:
    messages: list[str] = []
    for folder_name in folder_names:
        result = action(folder_name)
        if isinstance(result, list):
            messages.extend(result)
        else:
            messages.append(result)
    return messages

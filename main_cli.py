from typing import Callable

from folder_toggle_core import (
    DEFAULT_SORT,
    FolderInfo,
    SORT_LABELS,
    VISIBILITIES,
    add_common_ignore_patterns,
    add_top_file_type_pattern,
    apply_to_folders,
    create_github_repo,
    ensure_database,
    format_top_file_types,
    get_folder_infos,
    human_size,
    log_error,
    normalize_folder_names,
    push_repo,
    read_gitignore_text,
    set_all_enabled,
    set_folder_enabled,
    set_folder_visibility,
    toggle_all_enabled,
    write_gitignore_text,
)

MENU_OPTIONS = [
    ("1", "Refresh folder list"),
    ("2", "Change sort"),
    ("3", "Change one folder state"),
    ("4", "Enable all folders"),
    ("5", "Disable all folders"),
    ("6", "Toggle all folders"),
    ("7", "Change repo visibility"),
    ("8", "Normalize folder names"),
    ("9", "Create GitHub repo"),
    ("10", "Push repo to origin"),
    ("11", "Edit .gitignore"),
    ("12", "Add top file type ignore"),
    ("13", "Add common ignores"),
    ("0", "Quit"),
]


def main() -> None:
    ensure_database()
    sort_key = DEFAULT_SORT
    messages: list[str] = []
    try:
        while True:
            folders = get_folder_infos(sort_key)
            print()
            print_table(folders, sort_key)
            print_menu()
            if messages:
                print_messages(messages)
                messages = []
            choice = input("Select an option: ").strip()
            if choice == "0":
                print("Goodbye.")
                return
            if choice == "1":
                messages = ["Refreshed folder list"]
            elif choice == "2":
                sort_key, message = prompt_sort(sort_key)
                messages = [message]
            elif choice == "3":
                messages = handle_folder_state_change(folders)
            elif choice == "4":
                set_all_enabled(True)
                messages = ["Enabled all folders"]
            elif choice == "5":
                set_all_enabled(False)
                messages = ["Disabled all folders"]
            elif choice == "6":
                toggle_all_enabled()
                messages = ["Toggled all folders"]
            elif choice == "7":
                messages = handle_visibility_change(folders)
            elif choice == "8":
                messages = normalize_folder_names()
            elif choice == "9":
                messages = handle_folder_batch(folders, create_github_repo)
            elif choice == "10":
                messages = handle_folder_batch(folders, push_repo)
            elif choice == "11":
                messages = handle_gitignore_edit(folders)
            elif choice == "12":
                messages = handle_folder_batch(folders, add_top_file_type_pattern)
            elif choice == "13":
                messages = handle_folder_batch(folders, add_common_ignore_patterns)
            else:
                messages = [f"Unknown option: {choice}"]
    except Exception as error:
        log_error("main_cli", error)
        print("Unexpected error. Details were logged to folder-toggle-errors.log.")


def print_table(folders: list[FolderInfo], sort_key: str) -> None:
    print(f"Folder Toggle CLI ({SORT_LABELS.get(sort_key, SORT_LABELS[DEFAULT_SORT])})")
    if not folders:
        print("No tracked folders found.")
        return
    headers = ["#", "Folder", "Enabled", "Visibility", "Size", "Filtered", "Top File Types"]
    rows: list[list[str]] = []
    for index, folder in enumerate(folders, start=1):
        rows.append(
            [
                str(index),
                folder.name,
                "yes" if folder.enabled else "no",
                folder.visibility,
                human_size(folder.size),
                human_size(folder.filtered_size),
                format_top_file_types(folder.top_file_types),
            ]
        )
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print(format_row(headers, widths))
    print(format_row(["-" * width for width in widths], widths))
    for row in rows:
        print(format_row(row, widths))


def format_row(row: list[str], widths: list[int]) -> str:
    return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))


def print_menu() -> None:
    print()
    for code, label in MENU_OPTIONS:
        print(f"{code}. {label}")


def print_messages(messages: list[str]) -> None:
    print()
    for message in messages:
        print(f"- {message}")


def prompt_sort(current_sort: str) -> tuple[str, str]:
    sort_keys = list(SORT_LABELS.keys())
    for index, sort_key in enumerate(sort_keys, start=1):
        marker = "*" if sort_key == current_sort else " "
        print(f"{marker} {index}. {SORT_LABELS[sort_key]}")
    raw_value = input("Select sort: ").strip()
    if not raw_value.isdigit():
        return current_sort, "Sort unchanged"
    index = int(raw_value) - 1
    if index < 0 or index >= len(sort_keys):
        return current_sort, "Sort unchanged"
    next_sort = sort_keys[index]
    return next_sort, f"Sort set to {SORT_LABELS[next_sort]}"


def select_single_folder(folders: list[FolderInfo]) -> str:
    if not folders:
        return ""
    raw_value = input("Select folder number: ").strip()
    if not raw_value.isdigit():
        return ""
    index = int(raw_value) - 1
    if index < 0 or index >= len(folders):
        return ""
    return folders[index].name


def select_folder_names(folders: list[FolderInfo], allow_all: bool) -> list[str]:
    if not folders:
        return []
    prompt = "Select folder number"
    if allow_all:
        prompt += " or type all"
    prompt += ": "
    raw_value = input(prompt).strip().lower()
    if allow_all and raw_value == "all":
        return [folder.name for folder in folders]
    if not raw_value.isdigit():
        return []
    index = int(raw_value) - 1
    if index < 0 or index >= len(folders):
        return []
    return [folders[index].name]


def handle_visibility_change(folders: list[FolderInfo]) -> list[str]:
    folder_name = select_single_folder(folders)
    if not folder_name:
        return ["No folder selected"]
    for index, visibility in enumerate(VISIBILITIES, start=1):
        print(f"{index}. {visibility}")
    raw_value = input("Select visibility: ").strip()
    if not raw_value.isdigit():
        return ["Visibility unchanged"]
    index = int(raw_value) - 1
    if index < 0 or index >= len(VISIBILITIES):
        return ["Visibility unchanged"]
    return [set_folder_visibility(folder_name, VISIBILITIES[index])]


def handle_folder_state_change(folders: list[FolderInfo]) -> list[str]:
    folder_name = select_single_folder(folders)
    if not folder_name:
        return ["No folder selected"]
    print("1. Toggle")
    print("2. Enable")
    print("3. Disable")
    raw_value = input("Select state action: ").strip()
    if raw_value == "1":
        current_folder = next((folder for folder in folders if folder.name == folder_name), None)
        if current_folder is None:
            return ["Folder not found"]
        set_folder_enabled(folder_name, not current_folder.enabled)
        return [f"Toggled {folder_name}"]
    if raw_value == "2":
        set_folder_enabled(folder_name, True)
        return [f"Enabled {folder_name}"]
    if raw_value == "3":
        set_folder_enabled(folder_name, False)
        return [f"Disabled {folder_name}"]
    return ["Folder state unchanged"]


def handle_folder_batch(
    folders: list[FolderInfo], action: Callable[[str], str | list[str]]
) -> list[str]:
    folder_names = select_folder_names(folders, allow_all=True)
    if not folder_names:
        return ["No folders selected"]
    return apply_to_folders(folder_names, action)


def handle_gitignore_edit(folders: list[FolderInfo]) -> list[str]:
    folder_name = select_single_folder(folders)
    if not folder_name:
        return ["No folder selected"]
    current_text = read_gitignore_text(folder_name)
    print()
    print(f"Editing {folder_name}/.gitignore")
    print("Enter content below. Finish with a single END line.")
    if current_text:
        print()
        print(current_text.rstrip("\n"))
        print()
    lines: list[str] = []
    while True:
        line = input()
        if line == "END":
            break
        lines.append(line)
    return [write_gitignore_text(folder_name, "\n".join(lines))]


if __name__ == "__main__":
    main()

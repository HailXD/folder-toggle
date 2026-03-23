# Folder Toggle Features

This app tracks folders in the project directory, stores per-folder state in SQLite, and provides both GUI and CLI ways to manage them.

## Core Folder Management

- Detects folders in the app directory automatically and syncs them with `test.db`
- Stores per-folder enabled state
- Stores per-folder GitHub repo visibility as `private` or `public`
- Supports refreshing folder data when the filesystem changes
- Supports enabling one folder, enabling all folders, disabling all folders, and toggling all folders

## Folder Analytics

- Shows total folder size
- Shows filtered folder size based on that folder's `.gitignore`
- Shows the top 3 file types in each folder by size percentage
- Supports sorting by:
  - name ascending
  - name descending
  - size largest first
  - size smallest first
  - filtered size largest first
  - filtered size smallest first

## Folder Rename Tools

- Renames folders to lowercase
- Replaces spaces with `-`
- Handles case-only renames safely
- Skips rename conflicts instead of overwriting existing folders
- Syncs renamed folders back into the database

## GitHub Repo Creation

- Uses `git` and `gh` CLI
- Can initialize git repos for folders that do not already have `.git`
- Creates an `init` commit for new repos
- Can create GitHub repos using each folder's selected visibility
- Can push existing repos to `origin`
- Skips folders with existing git history and uncommitted changes

## .gitignore Management

- Reads each folder's own `.gitignore`
- Uses `.gitignore` rules to calculate filtered size
- Allows manual `.gitignore` editing
- Can add a top file type to `.gitignore` as `*.ext`
- Can bulk-add `node_modules/` and `*.pyc` if those are present in a folder
- Avoids duplicating ignore patterns already present

## CLI App

- `main_cli.py` provides a menu-driven version of the app without PyQt6
- Lets you list folders and their state in a terminal table
- Supports all major management actions from the terminal:
  - refresh
  - sorting
  - toggling enabled state
  - repo visibility changes
  - rename normalization
  - repo creation and push
  - `.gitignore` editing
  - top file type ignore insertion
  - bulk common ignore insertion

## GUI App

- `main.py` provides a PyQt6 version of the app
- Shows folder state in a multi-column table
- Supports repo visibility dropdowns per folder
- Supports clickable top file types for ignore insertion
- Supports progress-aware folder loading

## Error Handling

- Skips unreadable folders instead of stopping the whole load
- Treats unreadable or non-UTF-8 `.gitignore` files as empty
- Logs unexpected errors to `folder-toggle-errors.log`

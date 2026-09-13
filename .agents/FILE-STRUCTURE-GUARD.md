# 🧠 Agent: `FILE-STRUCTURE-GUARD`

## Purpose

This agent is responsible for maintaining the project's structural documentation.

It ensures that every file and folder change is automatically reflected inside `FILE_STRUCTURE.md`, making it the project's single source of truth for the current directory structure and structural change history.

Manual maintenance of `FILE_STRUCTURE.md` should never be required.

---

# 🎯 Core Responsibilities

Whenever the project structure changes, automatically synchronize `FILE_STRUCTURE.md`.

Structural changes include:

- Creating files
- Creating folders
- Renaming files
- Renaming folders
- Moving files
- Moving folders
- Deleting files
- Deleting folders

Do **not** update the History Log for:

- Editing code
- Changing CSS
- Refactoring logic
- Fixing bugs
- Updating documentation content
- Formatting code

Every structural change must be reflected immediately.

---

# FILE_STRUCTURE.md Responsibilities

The agent is solely responsible for maintaining `FILE_STRUCTURE.md`.

The document must always contain:

- Last Updated timestamp
- Complete project directory tree
- Complete folder hierarchy
- Complete file hierarchy
- History Log

The project tree must always represent the latest state of the project.

Whenever the project structure changes, update the project tree before updating the History Log.

---

# Last Updated

Every structural modification must update the following field:

```md
Last Updated: YYYY-MM-DD HH:MM AM/PM
```

This timestamp represents the last successful synchronization of `FILE_STRUCTURE.md`.

---

# Project Tree

The Project Tree must always represent the complete and current directory structure.

Whenever a structural change occurs:

- Create
- Rename
- Move
- Delete

Immediately regenerate the affected portion of the project tree.

Never leave the project tree outdated.

---

# History Log

The History Log records every structural modification.

Newest entries always appear first.

Group entries by date.

Example:

```md
## 2026-07-12

🕑 02:15 PM | 📄✏️ Renamed File | client/src/components/MinisterialWorkersTable.jsx → client/src/components/MWTable.jsx

🕑 01:42 PM | 📁✨ Created Folder | client/src/hooks

🕑 01:39 PM | 📄✨ Created File | client/src/hooks/useMembers.js
```

---

# Standard Action Emojis

Always use these emojis consistently.

| Action         | Emoji |
| -------------- | ----- |
| Created File   | 📄✨  |
| Created Folder | 📁✨  |
| Renamed File   | 📄✏️  |
| Renamed Folder | 📁✏️  |
| Moved File     | 📄🚚  |
| Moved Folder   | 📁🚚  |
| Deleted File   | 📄🗑️  |
| Deleted Folder | 📁🗑️  |

Never replace or change these emojis.

---

# Source File Metadata

Every newly created source file must begin with a metadata header placed at the top of the file.

Use the following format:

```js
/*
 * File: MinisterialWorkers.jsx
 * Created: YYYY-MM-DD HH:MM AM/PM
 * Last Modified: YYYY-MM-DD HH:MM AM/PM
 */
```

The metadata applies to:

- .js
- .jsx
- .css
- .md
- .sql

---

# Modification Rules

When modifying an existing source file:

- Never remove the original **Created** timestamp.
- Always update the **Last Modified** timestamp.
- If the file is renamed, update the **File** field to match the new filename.
- Preserve the existing metadata format.

---

# Exceptions

Do not add metadata headers to:

- package.json
- package-lock.json
- .env
- .env.\*
- .gitignore
- Binary assets
- Files that do not support comments

These files must still appear in the Project Tree and History Log.

---

# Folder Rules

Folders must never contain metadata files.

Do not create:

- README.md
- .gitkeep
- .keep
- metadata.json

Folder creation is documented exclusively within `FILE_STRUCTURE.md`.

---

# Synchronization Rules

Whenever the assistant performs any structural operation, it must also:

1. Update the Last Updated timestamp.
2. Synchronize the Project Tree.
3. Synchronize the History Log.
4. Update source file metadata when applicable.

These updates must occur in the same response.

The assistant must never leave `FILE_STRUCTURE.md` outdated.

---

# Enforcement Rules

It is forbidden to:

- Forget updating `FILE_STRUCTURE.md`.
- Leave the Project Tree outdated.
- Remove original Created timestamps.
- Replace Created timestamps with Last Modified timestamps.
- Use inconsistent action emojis.
- Create placeholder metadata files.
- Maintain multiple project structure documents.

`FILE_STRUCTURE.md` is the project's authoritative structural document.

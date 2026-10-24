# Search plugins

A plugin brings items from another program into the search bar: chats, contacts,
records. WinLauncher searches them with the same matching as files, shows them
with the plugin's label and an icon per item, and lets the plugin decide what
opening one does, usually a deep link into its program.

```text
Telegram: Anna Schmidt        @anna
Telegram: Team Annahme        Group  ·  3 unread
```

## How plugins run

- **Refresh.** Each enabled plugin lists all of its items on a worker thread at its
  interval (Settings → Plugins → Refresh every). Neither indexing nor searching
  waits for it: searches use the last list until the new one is complete, then
  the shown results update.
- **Cache.** The last list is kept in `%USERPROFILE%\.WinLauncher\plugin-data\<id>\items.json`,
  so the items appear right after a restart and survive a failed refresh (offline,
  rate limit). If the plugin says it needs setup, such as after signing out, its
  items are dropped.
- **Search.** Items match by their name and keywords, like file names do:
  exact names, prefixes, word starts, initials and fuzzy matches. The plugin lists
  its items most relevant first, such as the chats written to last; earlier
  items get a small bonus (12 points, halving every 10 items). The scores share
  the scale of file results, so plugin items join them by score. The plugin's
  **Boost** setting lifts or lowers them, between -100 and 100.
- **Only plain words.** A search with a filter such as `type:`, `ext:`, `in:` or a
  date asks for files, so plugins stay out of it. **In every search** off keeps a
  plugin out of ordinary searches altogether.
- **Keyword.** Typed first, the plugin's keyword searches only that plugin:
  `tg anna`. The keyword alone lists its items in the plugin's order, such as
  your most recent chats.
- **Open.** Enter, a click or Ctrl+1…9 calls the plugin's `open` on the action
  worker. Ctrl+C copies the item's link.

## Where plugins come from

WinLauncher ships no plugin itself. A plugin is either

- a Python file in `%USERPROFILE%\.WinLauncher\plugins\`, or
- an installed package that declares an entry point of the group
  `winlauncher.plugins`, such as
  [winlauncher-plugin-telegram](https://github.com/bjspi/WinLauncher-Plugins):
  your recent Telegram chats, opened in Telegram Desktop.

Both are loaded at start and then appear in **Settings → Plugins**, off until you
turn them on.

## Writing a plugin

Put a Python file in `%USERPROFILE%\.WinLauncher\plugins\`. At start, WinLauncher
loads every `Plugin` subclass defined in it; Settings → Plugins lists files that
failed to load. A package can declare plugins instead as entry points of the
group `winlauncher.plugins`:

```toml
[project.entry-points."winlauncher.plugins"]
crm = "my_package.crm:CrmPlugin"
```

The interface lives in `winlauncher/plugins/api.py`:

```python
from winlauncher.plugins.api import Plugin, PluginItem, PluginOption, PluginError


class Notes(Plugin):
    id = "notes"            # Settings key and data folder: a-z, 0-9, _ and -.
    label = "Note"          # Rows read "Note: <name>".
    description = "Text files from my notes folder."
    keyword = "n"           # "n shopping" searches only this plugin.
    refresh_minutes = 5
    icon = ""               # Image file for items without their own icon.
    round_icons = False     # True for avatars: round crops, initials without a picture.
    options = (PluginOption("folder", "Folder", "C:\\Notes", "Where the notes are."),)

    def refresh(self, context):
        """Every item, most relevant first. Runs on a worker; may take seconds."""
        from pathlib import Path

        folder = Path(context.options["folder"])
        if not folder.is_dir():
            raise PluginError(f"{folder} does not exist.")
        files = sorted(folder.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [PluginItem(str(f), f.stem, target=str(f), subtitle=str(f.parent)) for f in files]
```

- `PluginItem(key, name, target, subtitle, keywords, icon, title, data)`:
  - `key` identifies the item across refreshes.
  - `name` is matched and shown after the label; `title` replaces the whole
    "Label: name" text.
  - `keywords` also find the item (a username, an email address), a little below
    name matches.
  - `icon` is an image file, such as a cached avatar.
  - `data` holds JSON values for `open`; it is never matched.
- `refresh(context)` returns the items. Raise `PluginError` with a message for the
  user, or `SetupRequired` when the user must set something up first.
- `open(item, context)` opens the item; by default `os.startfile(item.target)`, so a
  URI such as `tg://…`, `msteams:/…`, `mailto:` or `https://…`, or a file path,
  opens in its registered program.
- `commands()` returns `PluginCommand(label, run)` buttons for the settings card,
  such as *Sign in…*. `run(context)` may call
  `context.ask(title, text, secret=False)` to ask for a line of text and returns a
  message to show. With `forgets_items=True`, the list is dropped afterwards.
- `context.data_dir` is the plugin's own folder for caches, sessions and pictures.
  `context.options` holds the validated values of `options`. A long refresh
  should stop when `context.cancel` is set. `context.log` writes to
  WinLauncher's log.

Refresh, commands and `open` run on different worker threads. A plugin that
uses COM calls `pythoncom.CoInitialize()` at the start of each of them and
`pythoncom.CoUninitialize()` when done.

### Example: Outlook contacts through COM

```python
import pythoncom
import win32com.client

from winlauncher.plugins.api import Plugin, PluginItem

CONTACTS_FOLDER = 10  # olFolderContacts


class OutlookContacts(Plugin):
    id = "outlook"
    label = "Outlook"
    keyword = "ol"
    refresh_minutes = 30
    round_icons = True

    def refresh(self, context):
        pythoncom.CoInitialize()
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            folder = outlook.GetNamespace("MAPI").GetDefaultFolder(CONTACTS_FOLDER)
            items = []
            for contact in folder.Items:
                if getattr(contact, "Class", None) != 40:  # olContact
                    continue
                email = contact.Email1Address or ""
                items.append(
                    PluginItem(
                        contact.EntryID,
                        contact.FullName or email,
                        target=f"mailto:{email}" if email else "",
                        subtitle=email,
                        keywords=(email, contact.CompanyName or ""),
                        data={"entry_id": contact.EntryID},
                    )
                )
            return items
        finally:
            pythoncom.CoUninitialize()

    def open(self, item, context):
        pythoncom.CoInitialize()
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            outlook.GetNamespace("MAPI").GetItemFromID(item.data["entry_id"]).Display()
        finally:
            pythoncom.CoUninitialize()
```

### Example: Microsoft Teams chats

Teams opens a chat with one or more people from a deep link. As `target`, it needs
no `open` of its own:

```python
PluginItem(
    email,
    display_name,
    target=f"msteams:/l/chat/0/0?users={email}",  # Or https://teams.microsoft.com/l/chat/0/0?users=…
    subtitle=email,
    keywords=(email,),
)
```

Where the names come from is up to the plugin: Outlook's address book, an
organization's directory library, or an export.

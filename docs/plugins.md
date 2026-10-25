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
- **Numbers are taken literally.** A term with a digit in it names something — a
  ticket, a version, a phone number — and is matched in one piece: `12345` finds
  the ticket 12345, never 52314. Words in the same query still match loosely, so
  `12345 login` and `uC halted` both work.
- **Only plain words.** A search with a filter such as `type:`, `ext:`, `in:` or a
  date asks for files, so plugins stay out of it. **In every search** off keeps a
  plugin out of ordinary searches altogether.
- **How many items.** Searching happens in Python over the plugin's list. A few
  thousand items stay inside a keystroke (measured: 5,000 tickets answer a typical
  query in 13–45 ms, a number in 2 ms); tens of thousands do not, so a plugin
  should list the part of its world you actually search and offer the rest through
  a direct lookup, such as opening a ticket by its key.
- **Keyword.** Typed first, the plugin's keyword searches only that plugin:
  `tg anna`. The keyword alone lists its items in the plugin's order, such as
  your most recent chats.
- **Open.** Enter, a click or Ctrl+1…9 calls the plugin's `open` on the action
  worker. Ctrl+C copies the item's link.

## Where plugins come from

WinLauncher ships no plugin itself. A plugin is a folder holding its `plugin.py`
and, next to it, a `plugin.toml` naming its version:

```text
telegram\
    plugin.toml      version = "1.0.0"
    plugin.py        one or more Plugin subclasses
    assets\          icons and whatever else it needs
```

Such folders are searched in

1. `%USERPROFILE%\.WinLauncher\plugins\`, always — the place for private plugins
   and for single plugins you copied in, and
2. every folder listed under **Settings → Plugins → Plugin folders**, in order —
   point one at a clone of a plugin repository, such as
   [WinLauncher-Plugins](https://github.com/bjspi/WinLauncher-Plugins) with the
   Telegram plugin.

A loose `<name>.py` file in those folders is a plugin too, for a quick one of your
own without any folder. Installed packages may declare plugins as entry points of
the group `winlauncher.plugins` instead:

```toml
[project.entry-points."winlauncher.plugins"]
crm = "my_package.crm:CrmPlugin"
```

The first plugin claiming an id wins, so a private plugin in the first folder
takes precedence over one of the same name in a cloned repository; the others are
listed as problems on the Plugins page, together with folders that failed to load.
Everything is loaded once at start: after adding a folder, save and restart
WinLauncher from the tray. Plugins stay off until you turn them on.

## A plugin cannot break WinLauncher

The loader is the one place that decides whether a plugin is usable, so the rest
of the program may trust what it declares:

- A module that fails to import — a syntax error, a missing package, an exception
  at import time — is listed as a problem; everything else starts normally.
- What a plugin declares (`label`, `description`, `keyword`, `refresh_minutes`,
  `icon`, `round_icons`, `options`) is checked once at load. A plugin that declares
  something the launcher could not paint is rejected with a message naming the
  mistake, instead of failing later inside a result row or the settings page. A
  plugin that does not implement `refresh` is rejected too.
- Every call into a plugin is wrapped. `refresh` failures are shown on its settings
  card and keep the items listed before; `open` and command failures are reported
  like any other failed action; `commands()` entries that are not usable buttons
  are dropped with a warning in the log.
- `PluginItem` refuses a field the launcher could not show, so a bad item fails the
  refresh instead of a later search.
- No plugin runs on the GUI thread, and worker threads are daemons: a plugin stuck
  in a network call delays only other plugin work and never keeps WinLauncher from
  quitting.

What WinLauncher cannot do is interrupt a running plugin, because Python cannot
kill a thread. Two plugins stuck in `refresh` occupy both plugin workers, and no
plugin refreshes again until they return. Network calls and subprocesses therefore
need explicit timeouts, and a long refresh must honor `context.cancel`.

## Writing a plugin

The interface lives in `winlauncher/plugins/api.py`; the plugin repository has a
[step-by-step guide](https://github.com/bjspi/WinLauncher-Plugins/blob/main/docs/writing-a-plugin.md)
and a template folder to copy.

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

Files that belong to the plugin itself, such as its icon, are addressed relative to
its module: `Path(__file__).with_name("assets") / "telegram.svg"`. Everything the
plugin writes belongs under `context.data_dir`, never next to its code, so the
plugin folder stays a clean checkout.

Refresh, commands and `open` run on different worker threads. A plugin that
uses COM calls `pythoncom.CoInitialize()` at the start of each of them and
`pythoncom.CoUninitialize()` when done.

A plugin may import only the standard library, WinLauncher's own API and packages
that are installed in the environment running WinLauncher; folder plugins pull in
no dependencies of their own. When a package is missing, raise `SetupRequired`
with the command that installs it, as the Telegram plugin does for Telethon.

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

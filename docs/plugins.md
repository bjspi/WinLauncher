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
- **Identifiers are taken literally.** A term with a digit in it names something — a
  ticket, a version, a phone number — and is matched in one piece: `12345` finds
  the ticket 12345, never 52314. A plugin adds its own shapes with
  `literal_terms = (r"HCP-?\w*",)`, for identifiers that have no digit of their
  own. Words in the same query still match loosely, so `12345 login` and
  `uC halted` both work.
- **Only plain words.** A search with a filter such as `type:`, `ext:`, `in:` or a
  date asks for files, so plugins stay out of it. **In every search** off keeps a
  plugin out of ordinary searches altogether.
- **How many items.** Searching happens in Python over the plugin's list. A few
  thousand items stay inside a keystroke (measured: 5,000 tickets answer a typical
  query in 13–45 ms, a number in 2 ms); tens of thousands do not, so a plugin
  should list the part of its world you actually search and offer the rest through
  a direct lookup, such as opening a ticket by its key.
- **Keywords.** Typed first, a plugin's keyword puts its items on top:
  `tg anna` searches your chats for Anna. The keyword alone lists them in the
  plugin's order, such as your most recent ones. The same text still searches your
  files, so `tg` also finds a file named `tg`. A plugin may have several keywords
  that work alike, such as `jira` and `ji`; change them in its settings, separated
  by commas.
- **Routes.** A plugin may offer keywords that search only part of its items. A
  ticket plugin could list every ticket under `jira` and offer a route *Bugs only*
  with the keyword `bug`: `bug login` then finds only bugs about logins. Route
  keywords are changed in the plugin's settings, too.
- **`@` for that plugin alone.** `@tg anna` answers from Telegram only, without any
  file results. Useful for a plugin whose keyword is a common word.
- **Open.** Enter, a click or Ctrl+1…9 calls the plugin's `open` on the action
  worker. Ctrl+C copies the item's link.

## Where plugins come from

WinLauncher ships no plugin itself. A plugin is a folder holding its `plugin.py`
and, next to it, a `plugin.toml`:

```text
telegram\
    plugin.toml      version, name, description, icon, required packages
    plugin.py        one or more Plugin subclasses
    assets\          icons and whatever else it needs
```

### Plugin sources

**Settings → Plugins → Plugin sources** lists where plugins come from, one per
line; [WinLauncher-Plugins](https://github.com/bjspi/WinLauncher-Plugins) is there
by default. **Available plugins** above shows what they offer.

| Source | Example | What you do |
| --- | --- | --- |
| A git repository | `https://github.com/bjspi/WinLauncher-Plugins.git`, `git@git.example.com:team/plugins.git` | **Install and activate**, **Update**, **Uninstall** — the plugin is copied into `%USERPROFILE%\.WinLauncher\plugins\` |
| A folder | `D:\Work\plugins`, `\server\share\plugins` | **Activate**, **Deactivate** — the plugin loads from where it lies, so your edits apply at the next start |

Nothing from a source loads until you install or activate it. Both first install
the Python packages the plugin needs (`requires`, see below) into WinLauncher's
environment, and the plugin is on from the next start; the page offers to restart
right away. Its keywords and options are then under **Settings → Plugin
settings**, one card per plugin with its icon; unfold a card to see them. A card
that is off shows only its header. WinLauncher checks its sources every 6 hours and announces a new
version of an installed plugin in a tray message.

Repositories are fetched with git, so git must be installed. A private one works
once git can reach it with your saved credentials — clone it once in a terminal if
git has to ask for a password; WinLauncher itself never prompts.

### Laying out a source

A folder or repository offers plugins in one of three ways:

- a `catalog.toml` at its root names it and the folder holding its plugins:

  ```toml
  name = "Team plugins"     # Shown in Settings; the address when missing.
  plugins = "plugins"       # One subfolder per plugin.
  ```

- it is a single plugin folder, or
- it is a folder of plugin folders.

Each plugin folder describes itself in its `plugin.toml`, which WinLauncher reads
without running the plugin:

```toml
version = "1.2.0"         # Raise it with every change: installed copies get the update.
name = "Jira"
description = "Your open tickets; choosing one opens it in the browser."
icon = "assets/jira.svg"  # Shown under Available plugins; must lie in the plugin folder.
requires = ["jira>=3.8"]  # Python packages beside WinLauncher, as pip requirements.
```

Folders starting with `_`, such as a template, are not offered. Updates and
uninstalling only touch plugin folders installed from that source, so a private
plugin of the same name is left alone.

### The plugins folder

`%USERPROFILE%\.WinLauncher\plugins\` always loads: plugins installed from a
repository land there, and so can private plugins or single ones you copy in.

A loose `<name>.py` file in that folder is a plugin too, for a quick one of your
own without any folder. Installed packages may declare plugins as entry points of
the group `winlauncher.plugins` instead:

```toml
[project.entry-points."winlauncher.plugins"]
crm = "my_package.crm:CrmPlugin"
```

The first plugin claiming an id wins, so a plugin in the plugins folder takes
precedence over an activated one of the same id; the others are listed as problems
on the Plugins page, together with folders that failed to load. Everything is
loaded once at start, so installing, activating or removing a plugin takes effect
after a restart.

## A plugin cannot break WinLauncher

The loader is the one place that decides whether a plugin is usable, so the rest
of the program may trust what it declares:

- A module that fails to import — a syntax error, a missing package, an exception
  at import time — is listed as a problem; everything else starts normally.
- What a plugin declares (`label`, `description`, `keywords`, `routes`, `refresh_minutes`,
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

- A plugin whose items take too long to rank — several thousand, say — is left out
  of ordinary searches after 3 slow searches in a row, until it lists new items;
  its keywords still search it. **Settings → Plugin settings → Time limit per
  search** sets the limit (default 30 ms, 0 turns it off). Turning **In every
  search** off keeps a big plugin to its keywords for good.

What WinLauncher cannot do is interrupt a running plugin, because Python cannot
kill a thread. Two plugins stuck in `refresh` occupy both plugin workers, and no
plugin refreshes again until they return. Network calls and subprocesses therefore
need explicit timeouts, and a long refresh must honor `context.cancel`.

## Internet status and offline pauses

WinLauncher checks Windows' Network List Manager and requests
`https://www.gstatic.com/generate_204` at startup and every 15 seconds. The HTTPS
GET expects HTTP 204, has a two-second request timeout and runs on a separate
network worker. No plugin data is sent. Either successful check means **online**;
only when both report no connectivity is the status **offline**. Missing or inconclusive checks
mean **unknown** unless the other check succeeds. A blocked HTTPS probe alone therefore
does not mark an otherwise connected computer offline.

**Settings → Plugin settings → All plugins → Pause plugins without internet**
pauses automatic refreshes while the status is offline. It defaults to off:
local and VPN-only services may work without public internet. Cached items stay
searchable, and **Refresh now**, **Refresh catalog**, opening items and plugin
commands remain available. Already running calls are not interrupted. Queued
automatic refreshes check the status again before calling the plugin. When
connectivity returns to online, enabled plugins are refreshed without waiting for
their normal interval. Unknown status allows automatic refreshes.

Plugins may read the shared status whether or not the pause setting is enabled:

```python
from winlauncher.plugins.api import InternetStatus, PluginError


def refresh(self, context):
    internet = context.internet()
    if internet.status == InternetStatus.OFFLINE:
        # Raise rather than return [], so the launcher's cached items survive.
        raise PluginError("Offline; keeping the last items.")
    return self.fetch_items()  # Keep explicit timeouts and normal error handling.
```

`context.internet()` reads the latest immutable observation, even from an existing
context during a long refresh. It exposes `status` (`InternetStatus.ONLINE`,
`OFFLINE`, `UNKNOWN`), `windows_online` and `http_online` (`bool | None`), and
`checked_at` (Unix seconds, or `None` before the first check). Reading it never
starts network I/O. Before the first observation its status is unknown.
The old `ping_online` attribute remains a read-only alias for `http_online`.

This is an optional hint: online does not guarantee that a particular host, VPN,
proxy or sign-in works. Plugins own their retry policy. They can pause retries
without increasing their backoff while offline and allow a prompt retry after
reconnection. WinLauncher cannot reset a plugin's internal backoff; server rate
limits and authentication errors still need their own handling.

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
    keywords = ("n", "note")  # "n shopping" puts this plugin first; synonyms work alike.
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

- `PluginItem(key, name, target, subtitle, keywords, icon, title, routes, data)`:
  - `key` identifies the item across refreshes.
  - `name` is matched and shown after the label; `title` replaces the whole
    "Label: name" text.
  - `keywords` also find the item (a username, an email address), a little below
    name matches.
  - `icon` is an image file, such as a cached avatar.
  - `routes` names the plugin's routes that list the item (see below).
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

### Multiline text options

Text options normally use a single-line field. Set `multiline=True` for a text
box without line wrapping, for example a list of source URLs:

```python
options = (PluginOption("folders", "Source folders", "", multiline=True),)
```

The value in `context.options["folders"]` is still a string, including its line
breaks. WinLauncher does not split entries or convert existing semicolon-separated
values; the plugin decides which separators to accept. The default is `False`,
and `multiline=True` requires a string default.

### Keywords and routes

`keywords` are the plugin's suggestions; the user's own replace them in Settings.
Older plugins declare a single `keyword = "n"`, which still works. A route searches
only the items that name it in their `routes`:

```python
from winlauncher.plugins.api import Plugin, PluginItem, PluginRoute


class Tickets(Plugin):
    id = "tickets"
    label = "Ticket"
    keywords = ("jira", "ji")
    routes = (PluginRoute("bugs", "Bugs only", ("bug",), "Typed first, it searches bugs."),)

    def refresh(self, context):
        return [
            PluginItem("T-1", "Login fails after update", routes=("bugs",)),
            PluginItem("T-3", "Login with single sign-on"),
        ]
```

`jira login` finds both tickets, `bug login` only the first, and `@bug login` only
the first without any file results. A route is a filter on the items the plugin
listed, so it costs no extra refresh; its `id` must stay stable, because the user's
keywords for it are stored under it.

### Encodings

`plugin.py`, `plugin.toml` and `catalog.toml` may be saved as UTF-8, with or
without a byte order mark, or as Windows-1252, as older Windows editors do; a
`# coding:` declaration in `plugin.py` is honored. UTF-8 is still the
recommendation.

### Files of a plugin

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
    keywords = ("ol", "outlook")
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

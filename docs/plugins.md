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
  the shown results update. Saving changed options of a plugin, such as an
  exclusion list, refreshes it at once.
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
  `literal_terms = (r"TKT-?\w*",)`, for identifiers that have no digit of their
  own. Words in the same query still match loosely, so `12345 login` and
  `uC halted` both work.
- **Words and dates.** A search with a filter such as `type:`, `ext:`, `in:`,
  `size:` or `rg:` asks for files, so plugins stay out of it. Date filters and
  `sort:` work for plugin items, too, when the plugin says when each item last
  changed: `anna date:week` finds this week's chats with Anna, `date:today` alone
  lists everything that changed today, files and items alike, and
  `sort:modified` puts the newest first. Items without a date stay out while a
  date filter is typed. **In every search** off keeps a plugin out of ordinary
  searches altogether.
- **Preview.** With the preview open (Alt+P, see Settings → General), the pane
  shows what the plugin offers for the selected item, such as a ticket's
  description, or the file behind an item that is a local path.
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
- **Live search.** A plugin with a full-text index or a server of its own may
  search by itself instead of listing items. Only its keyword asks it, and the
  results then hold only what it found (see *Live search* below).
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
settings**: each plugin has its own page there, listed below it with its icon. A
plugin that is off shows only its header. WinLauncher checks its sources every 6 hours and announces a new
version of an installed plugin in a tray message.

Repositories are fetched with git, so git must be installed (restart WinLauncher
after installing it). **Check now** shows a spinner while it runs and a warning
when a source cannot be reached, with a hint at the likely cause. A private one works
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
on the Plugins page, together with folders that failed to load, and the loaded
plugin's card names each skipped copy with its place and version. Everything is
loaded once at start, so installing, activating or removing a plugin takes effect
after a restart.

### Several instances of one plugin

**Add copy** on a plugin's page (Settings → Plugin settings → the plugin) adds another instance
of it, such as for a second Jira server or account. It loads from the next start,
which WinLauncher offers right away, and gets a card of its own: on, off,
keywords, routes, boost, interval and the plugin's options are its own, and so are
its items and its data folder (`plugin-data\<id>~<number>`), so it signs in
separately. Its name, at first “Jira 2”, comes before each of its items and can be
changed on its card. Its suggested keywords carry its number, `jira2` beside
`jira`, so both work at once. **Remove copy** deletes it with its settings once
saved; it stays loaded until the next start, which deletes its data folder. A copy
runs the code of the plugin it copies, so it follows that plugin's updates, and
it does not load when that plugin is gone.

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
- Refresh, commands and `open` run on worker threads, and those are daemons: a plugin
  stuck in a network call delays only other plugin work and never keeps WinLauncher
  from quitting. A stuck `open` delays only opening other plugin items.
- Importing your plugin and creating its class run on the GUI thread at every start,
  before the shortcut works. An exception or `sys.exit()` there skips the plugin, but
  a slow import delays WinLauncher, so import heavy packages inside `refresh`.
- All plugins share WinLauncher's process. A crash in native code, such as a C
  extension or a DLL, ends all of WinLauncher; the next start writes the stacks of
  its threads to `winlauncher.log`.

- A plugin whose items take too long to rank — several thousand, say — is left out
  of ordinary searches after 3 slow searches in a row, until it lists new items;
  its keywords still search it. **Settings → Plugin settings → Time limit per
  search** sets the limit (default 30 ms, 0 turns it off). Turning **In every
  search** off keeps a big plugin to its keywords for good.

What WinLauncher cannot do is interrupt a running plugin, because Python cannot
kill a thread. Two plugins stuck in `refresh` occupy both plugin workers, and no
plugin refreshes again until they return. Network calls and subprocesses therefore
need explicit timeouts, and a long refresh must honor `context.cancel`.

## Older and newer WinLauncher versions

A plugin may meet an older WinLauncher than the one it was written for, and an old
plugin a newer one. Newer versions keep what plugins use: new fields and arguments
are optional, so an old plugin needs no change. A new plugin asks what the running
WinLauncher offers before using something recent:

```python
from winlauncher.plugins import api

FEATURES = frozenset(getattr(api, "FEATURES", ()))  # Empty before WinLauncher 0.22.2.

def menu_entry(label, run, icon):
    if "action_icons" in FEATURES:
        return api.PluginAction(label, run, icon=icon)
    return api.PluginAction(label, run)
```

`api.FEATURES` names everything below. A name is never removed or renamed. It
exists since 0.22.2; before, `winlauncher.__version__` tells the version, and the
sections on each feature show the usual guard.

| Feature | Since | What it offers |
| --- | --- | --- |
| `literal_terms` | 0.12 | `Plugin.literal_terms` |
| `routes` | 0.14 | `PluginRoute`, `PluginItem.routes` |
| `multiline_options` | 0.15.2 | `PluginOption(multiline=True)` |
| `internet` | 0.15.3 | `context.internet` |
| `breadcrumbs` | 0.15.8 | `PluginItem.breadcrumbs` |
| `chips` | 0.15.10 | `Chip`, `PluginItem.chips` |
| `dialogs` | 0.17 | `context.ask`, `context.choose`, `context.form`, `Choice` |
| `item_actions` | 0.17 | `Plugin.actions`, `PluginAction` |
| `exclude` | 0.17 | `Plugin.exclude` |
| `answers` | 0.17 | `Plugin.answer`, `Answer` |
| `tips` | 0.18.1 | `Plugin.tips`, `PluginTip` |
| `previews` | 0.20 | `Plugin.preview`, `PluginPreview`, `PluginItem.modified` |
| `action_icons` | 0.22.2 | `PluginAction.icon` |
| `live_search` | 0.23.6 | `Plugin.live_search`, `Plugin.search`, `SearchRequest` |
| `command_states` | 0.23.6 | `PluginCommand.shown` |

What an older WinLauncher does not know fails gracefully: an import of a missing
name lists the plugin as a problem at start, and an exception in `actions` loses
only that menu's entries. So import recent names guardedly and check `FEATURES`
before passing a recent argument.

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

- `PluginItem(key, name, target, subtitle, keywords, icon, title, routes, data, breadcrumbs,
  chips, modified)`:
  - `key` identifies the item across refreshes.
  - `name` is matched and shown after the label; `title` replaces the whole
    "Label: name" text.
  - `subtitle` is muted text beside the name. `breadcrumbs` replaces it with the
    item's location as parts, outermost first (see below).
  - `keywords` also find the item (a username, an email address), a little below
    name matches.
  - `icon` is an image file, such as a cached avatar.
  - `routes` names the plugin's routes that list the item (see below).
  - `data` holds JSON values for `open`, `preview` and `actions`; it is never matched.
  - `chips` are short colored labels, such as a ticket's status (see below).
  - `modified` says when the item last changed, as a `datetime` or a POSIX
    timestamp, such as a chat's last message (see below).
- `refresh(context)` returns the items. Raise `PluginError` with a message for the
  user, or `SetupRequired` when the user must set something up first. A plugin
  with `live_search` may leave it out (see *Live search* below).
- `search(request, context)` returns the items for the text typed behind the
  keyword, best first, when `live_search` is set (see *Live search* below).
- `open(item, context)` opens the item; by default `os.startfile(item.target)`, so a
  URI such as `tg://…`, `msteams:/…`, `mailto:` or `https://…`, or a file path,
  opens in its registered program.
- `preview(item, context)` returns a `PluginPreview` for the preview pane, or `None`
  (see below). By default an item whose target is a local path previews that file.
- `commands()` returns `PluginCommand(label, run)` buttons for the settings card,
  such as *Sign in…*. `run(context)` returns a message to show and may open
  dialogs, each returning `None` when cancelled:
  - `context.ask(title, text, secret=False)`: a line of text.
  - `context.choose(title, text, choices)`: a checklist with a filter. `choices`
    are `Choice(key, label, detail="", checked=False)` rows; it returns the
    `frozenset` of the ticked keys.
  - `context.form(title, text, fields)`: `PluginOption` fields with the editors of
    the settings card — switches, numbers, a line or a multiline text box — each
    starting at its `default`; it returns the values by key.

  With `forgets_items=True`, the list is dropped afterwards. With `shown=`, the
  button shows only while it fits, such as *Sign in…* while signed out (see
  *Buttons that follow the plugin's state* below).
- `tips(context)` returns `PluginTip(example, meaning, insert="")` rows for the `?`
  help, such as the questions `answer` understands; choosing one types `insert`, or
  the example itself. Their meaning is shown after the plugin's label.
- `answer(text, context)` may answer the typed text itself, as `Answer(result,
  detail="")`, shown above the results like a calculation: Enter copies `result`,
  `detail` is muted beside it. It is asked shortly after typing pauses, on a worker
  of its own, possibly while a refresh runs: keep it quick, thread-safe and off
  the network, and answer only text whose whole shape you recognize. Return `None`
  otherwise. Arithmetic answers first, without plugins.
- `actions(item)` returns `PluginAction(label, run, refreshes=False, icon="")` entries for
  the item's right-click menu, or `()` when none fits the item. It is called while
  the menu opens, so it must be quick and only look at `item`. `run(item, context)`
  runs on a worker: it may read files and the plugin's caches, look things up, show
  the same dialogs as a command, and returns a message or `None`. So nothing an
  action needs has to be prepared at refresh: `item.data` only holds what is cheap
  anyway or needed while the menu opens, such as whether an entry appears at all.
  `icon` shows beside the label: one of WinLauncher's line icons by name, tinted
  for the theme like its own entries (`activity`, `alert`, `ban`, `chart`, `check`,
  `code`, `download`, `eye-off`, `file`, `folder`, `gear`, `globe`, `help`, `info`,
  `keyboard`, `pin`, `plug`, `power`, `refresh`, `search`, `sliders`, `terminal`,
  `trash`, `window`), or the full path of an image file. Anything else shows no
  icon. Icons exist since 0.22.2 (feature `"action_icons"`, see below).

  ```python
  def actions(self, item):
      return (PluginAction("Open parent folder", self.open_parent, icon="folder"),)

  def open_parent(self, item, context):
      os.startfile(self.parent_url(item.target, context.data_dir))  # Looked up on the worker.
      return None
  ```
- `exclude(item, context)`, when a plugin defines it, puts **Exclude…** into every
  item's menu. The plugin decides what to leave out — the item, its folder, a
  pattern — usually after a short `context.choose` or `context.form`, keeps that in
  its data folder and returns `True`, or `False` when cancelled. WinLauncher drops
  the item at once and refreshes the plugin. The dialogs, actions and Exclude… exist
  since WinLauncher 0.17. A plugin that also runs on older versions imports
  `PluginAction` guardedly, or it does not load there at all, and checks
  `getattr(context, "choose", None)` before a dialog:

  ```python
  try:
      from winlauncher.plugins.api import PluginAction
  except ImportError:  # WinLauncher before 0.17: no item actions.
      PluginAction = None
  ```

  `actions` then returns `()` while `PluginAction` is `None`.
- `context.data_dir` is the plugin's own folder for caches, sessions and pictures.
  `context.options` holds the validated values of `options`. A long refresh
  should stop when `context.cancel` is set. `context.log` writes to
  WinLauncher's log.

### Long locations: breadcrumbs

A URL such as
`https://contoso.sharepoint.com/sites/Finance/Shared%20Documents/Forms/AllItems.aspx?id=…`
is a poor subtitle: shortened in the middle by characters, it keeps the host
and the query string, which nobody reads. Give the meaningful parts instead and
leave out technical ones (`sites`, `Shared Documents`, `Forms`, view pages, ids):

```python
PluginItem(
    key=drive_item_id,
    name="Q3 report.xlsx",
    target=web_url,  # Opening and copying still use the full URL.
    breadcrumbs=("Contoso Finance", "Documents", "Controlling", "2026", "Q3"),
)
```

The row shows `Contoso Finance › Documents › Controlling › 2026 › Q3`. When that
does not fit, whole middle parts give way first, so the outermost part (where)
and the innermost ones (how deep) stay readable:
`Contoso Finance › … › 2026 › Q3`, then `… › Q3`. Only then are characters
elided. Each part must be non-empty text; `breadcrumbs` takes precedence over
`subtitle`, and the full joined text is read to screen readers. Breadcrumbs are
only shown, never matched: put words users type into `name` or `keywords`.

### Status at a glance: chips

A chip is a short rounded label on the row's second line, before the location,
such as a ticket's status or priority:

```python
from winlauncher.plugins.api import Chip, ChipTone, PluginItem

PluginItem(
    key=issue["key"],
    name=f"{issue['key']} {fields['summary']}",
    target=f"https://contoso.atlassian.net/browse/{issue['key']}",
    breadcrumbs=("Contoso", fields["project"]["name"]),
    chips=(Chip(fields["status"]["name"], ChipTone.INFO), Chip("Bug")),
)
```

A chip names a tone, not a color, so it stays readable in the light and the dark
theme and looks like the chips of every other plugin:

| Tone | Color | For |
| --- | --- | --- |
| `NEUTRAL` (default) | gray | types, labels, "To Do" |
| `INFO` | blue | work going on, "In Progress" |
| `SUCCESS` | green | done, approved, online |
| `WARNING` | orange | waiting, due soon |
| `DANGER` | red | blocked, failed, overdue |

`tone` also takes the value as text, such as `"success"`. Keep chips to a word or
two and to what helps choosing a row: the launcher draws at most three
(`MAX_CHIPS`), elides long texts, and leaves out chips that do not fit whole.
The location gives way before chips do. Screen readers hear the chips' texts.
Chips are only shown, never matched: to let users type `blocked`, also put the
word into `keywords`.

### When items changed: dates

Give items a `modified` time, and date filters and `sort:modified` include them:

```python
from datetime import datetime, timezone

PluginItem(
    key=chat["id"],
    name=chat["title"],
    target=f"tg://resolve?domain={chat['username']}",
    modified=datetime.fromtimestamp(chat["last_message_date"], timezone.utc),
)
```

A `datetime` without a time zone is read as local time, like the dates users
type. Use the time that matters for choosing an item — the last message, the
last update of a ticket — rather than when the plugin fetched it. While a date
filter is typed, the row shows that time like a file's.

### Previews

With the preview pane open, the selected item shows what `preview` returns. It is
asked once the selection rests, on a worker of its own, possibly while a refresh
runs; keep it thread-safe and quick (well within a second), and stop when
`context.cancel` is set, because the user moved on. Unlike `answer`, it may read
files or ask the network; caching what it fetched in `context.data_dir` keeps
going back and forth quick.

```python
from winlauncher.plugins.api import PluginError, PluginPreview


class Tickets(Plugin):
    ...

    def preview(self, item, context):
        issue = self.client(context).issue(item.key)  # Raises PluginError when signed out.
        return PluginPreview(
            markdown=f"## {issue.summary}\n\n{issue.description}",
            facts=f"{issue.status}  ·  {issue.assignee}",
        )
```

A `PluginPreview` holds exactly one content:

| Field | Shows |
| --- | --- |
| `markdown` | Rendered text; images it links to are left out |
| `text` | Plain text in a fixed-width font |
| `file` | A local file or folder, previewed like a search result with its size and date |
| `image` | A picture file, such as one saved in `context.data_dir`, without size and date |

`facts` adds a muted line below, after the item's title and `modified` date.
Text beyond 65,536 characters is cut. Return `None` when there is nothing to
show; raise `PluginError` with a message to show it in the pane instead. Other
exceptions are logged, and the pane says the plugin failed.

The default `preview` returns `PluginPreview(file=item.target)` when the target
is an absolute local path, such as `C:\Notes\shopping.md` or a share, and `None`
for links. `modified`, `preview` and `PluginPreview` exist since WinLauncher 0.20:
a plugin that also runs on older versions sets `modified` only when
`"modified" in PluginItem.__dataclass_fields__`, and imports `PluginPreview`
inside `preview`.

### Buttons that follow the plugin's state

A plugin's buttons appear first on its card, in the order `commands()` returns
them, followed by WinLauncher's *Refresh now* and *Add copy*. By default every
button always shows. A button can instead show only while it makes sense, such
as *Sign in…* while signed out and *Sign out* while signed in: give it
`shown=`, a function of the context that returns whether to show it.

```python
from winlauncher.plugins import api
from winlauncher.plugins.api import PluginCommand

FEATURES = frozenset(getattr(api, "FEATURES", ()))


def signed_in(context):
    return (context.data_dir / "session.json").exists()


class Tickets(Plugin):
    ...

    def commands(self):
        if "command_states" not in FEATURES:  # Older WinLauncher: every button, always.
            return (
                PluginCommand("Sign in…", self.sign_in),
                PluginCommand("Sign out", self.sign_out, forgets_items=True),
            )
        return (
            PluginCommand("Sign in…", self.sign_in, shown=lambda c: not signed_in(c)),
            PluginCommand("Sign out", self.sign_out, forgets_items=True, shown=signed_in),
        )
```

- `shown` is asked on the GUI thread when the card shows, after each of the
  plugin's commands and after each refresh. Keep it to quick checks, such as
  whether a file exists in `context.data_dir`; never the network.
- A `shown` that raises is logged, and the button shows: a broken check never
  hides how to set a plugin up.
- While the last refresh raised `SetupRequired`, *Refresh now* is disabled; a
  command such as *Sign in…* refreshes the plugin when it succeeds.

Both ways stay supported. A plugin without `shown` keeps working unchanged on
every WinLauncher, and a new plugin may still return a fixed list. `shown` exists
since WinLauncher 0.23.6 (feature `"command_states"`): an older WinLauncher fails
to create a `PluginCommand` with it, so pass it only after checking `FEATURES`,
as above.

### Live search: the plugin searches by itself

Listing every item suits a few thousand chats or tickets. A full-text index, a
document library or a ticket server holds far more, and already searches better
than matching names could. Such a plugin sets `live_search = True` and answers
`search(request, context)` instead of listing items:

- Only its keyword asks it, never an ordinary search: `ft brake pressure` asks
  the plugin, `brake pressure` alone does not. A route's keyword asks it, too,
  with the route's id in `request.route`.
- The results then hold only what it found, in its order, without files or other
  plugins; `@` changes nothing.
- It is asked 300 ms after typing pauses, on workers of its own, so a slow server
  never holds up a file search. Meanwhile the results say *Searching <label>…*.
- Typing on sets `context.cancel`. After `search_timeout_ms` (default 5000) it is
  set as well and the results say the plugin did not answer. Stop when it is set,
  for example by passing short timeouts to each request.
- Raise `PluginError` with a message, such as *Sign in first*, to show it instead
  of results. Other exceptions are logged, and the results say the plugin failed.
- `request.text` is everything typed behind the keyword, trimmed, and empty for
  the keyword alone; `request.limit` is how many items are shown. Filters such as
  `date:` are not parsed for the plugin: it reads them from the text if it wants.
- A plugin that searches only leaves out `refresh`: it is never refreshed, and its
  settings card has no *Refresh every*, *Refresh now* or *In every search*.
  `open`, `preview`, `actions`, chips, breadcrumbs and `modified` work as for
  listed items. Put the found passage in `subtitle` or `breadcrumbs`, and the full
  context in `preview`.

A minimal example: a full-text search over a local SQLite FTS5 index that a
separate indexing service keeps up to date. The plugin only reads it.

```python
import sqlite3
from pathlib import Path

from winlauncher.plugins.api import Plugin, PluginError, PluginItem, PluginOption


class FullText(Plugin):
    id = "fulltext"
    label = "Full text"
    description = "Documents from the local full-text index."
    keywords = ("ft",)
    live_search = True
    search_timeout_ms = 2000
    options = (PluginOption("index", "Index file", "C:\\Index\\fulltext.db"),)

    def search(self, request, context):
        if not request.text:
            return []  # The keyword alone: nothing to look for yet.
        index = Path(context.options["index"])
        if not index.is_file():
            raise PluginError(f"No index at {index}. Start the indexing service first.")
        # Read only: the indexing service writes. One connection per search, because
        # searches run on several worker threads.
        with sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True, timeout=1) as db:
            rows = db.execute(
                "SELECT path, title, snippet(chunks, 2, '', '', ' … ', 12) "
                "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                (request.text, request.limit),
            ).fetchall()
        return [
            PluginItem(path, title, target=path, subtitle=passage)
            for path, title, passage in rows
        ]
```

An index or server only answers quickly when the plugin keeps the work small per
request: never build an index inside `search`, and keep long indexing in a
service of its own, so a crash there cannot end WinLauncher. A server's own
search, such as Jira's JQL or Microsoft Graph's search, fits as well; use the
network timeout of each request to stay within `search_timeout_ms`.

`live_search`, `search` and `SearchRequest` exist since WinLauncher 0.23.6 (feature
`"live_search"`). An older WinLauncher ignores them: there the plugin needs a
`refresh`, or it lists nothing.

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

The user may run several instances of a plugin (see “Several instances of one
plugin”): each is a subclass WinLauncher creates with its own `id` and `label`,
and each gets its own options and `context.data_dir`. Keep what belongs to one
instance, such as a session or a client, on `self` or in that folder, not in
module globals or class attributes, which the instances share, and read
`self.id` and `self.label` instead of repeating your own, such as in a credential
name.

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

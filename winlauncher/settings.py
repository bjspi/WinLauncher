"""User settings: validated value object and its JSON file."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path

from winlauncher.catalog.exclusions import DEFAULT_MARKER_FILES, ExclusionPolicy
from winlauncher.catalog.model import normalize
from winlauncher.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    PriorityPolicy,
    PriorityRule,
    SearchPriority,
)
from winlauncher.commands import DEFAULT_MENU_COMMANDS, MenuCommand
from winlauncher.plugins.config import PluginConfig
from winlauncher.websites import DEFAULT_WEBSITES, WebsiteShortcut

DEFAULT_DOUBLE_CTRL_MS = 350
DOUBLE_CTRL_RANGE_MS = (150, 800)
DEFAULT_AUTO_HIDE_SECONDS = 3
AUTO_HIDE_RANGE_SECONDS = (1, 60)
RESCAN_RANGE_SECONDS = (30, 86400)
RESULT_LIMIT_RANGE = (10, 200)
DEFAULT_CONTENT_SEARCH_THREADS = 4
CONTENT_SEARCH_THREADS_RANGE = (1, 16)
BOOST_RANGE = (-100, 100)
# Excluded names; matching directories also exclude their whole subtree.
DEFAULT_EXCLUDED_PATTERNS = (
    # Version control, including copies such as .git_old (and .github)
    ".git*",
    ".svn",
    ".hg",
    # Python
    "__pycache__",
    ".venv",
    "venv",
    ".ruff_cache",
    ".mypy_cache",
    ".dmypy_cache",
    ".pytype",
    ".pyre",
    ".pyright",
    ".basedpyright",
    ".pylint.d",
    ".pylint_cache",
    ".lint_cache",
    ".pytest_cache",
    "pytest-cache-files-*",
    ".tox",
    ".nox",
    ".hypothesis",
    ".ipynb_checkpoints",
    ".eggs",
    "*.egg-info",
    # JavaScript
    "node_modules",
    ".next",
    ".parcel-cache",
    # Build output and build-tool caches (Gradle, Android, CMake)
    "build",
    "dist",
    ".gradle",
    ".cxx",
    ".externalNativeBuild",
    # IDE state
    ".vs",
    ".idea",
    # Windows
    "*.lnk",
    "~$*",  # Office lock/owner files.
    "$Recycle.Bin",
    "System Volume Information",
)
_USER_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
# Desktop, Documents and Downloads; values may point to redirected or OneDrive folders.
_DEFAULT_SHELL_FOLDERS = ("Desktop", "Personal", "{374DE290-123F-4565-9164-39C4925E467B}")


def default_roots() -> tuple[str, ...]:
    home = Path.home()
    folders = [home / name for name in ("Desktop", "Documents", "Downloads")]
    folders.extend(_redirected_shell_folders())
    return tuple(dict.fromkeys(str(folder) for folder in folders if folder.is_dir()))


def _redirected_shell_folders() -> list[Path]:
    if os.name != "nt":
        return []
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_SHELL_FOLDERS)
    except OSError:
        return []
    folders = []
    with key:
        for name in _DEFAULT_SHELL_FOLDERS:
            try:
                value = winreg.QueryValueEx(key, name)[0]
            except OSError:
                continue
            folders.append(Path(os.path.expandvars(value)))
    return folders


def _require_range(value: int, bounds: tuple[int, int], message: str) -> None:
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(message.format(low=low, high=high))


class Appearance(StrEnum):
    """Light or dark colors; `system` follows the Windows setting."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class WindowsSettingsDetail(StrEnum):
    """Which Windows settings are indexed: whole pages, or also each single setting."""

    PAGES = "pages"
    EVERY_SETTING = "every-setting"


@dataclass(frozen=True, slots=True)
class Settings:
    roots: tuple[str, ...] = field(default_factory=default_roots)
    excluded_paths: tuple[str, ...] = ()
    excluded_patterns: tuple[str, ...] = DEFAULT_EXCLUDED_PATTERNS
    marker_files: tuple[str, ...] = DEFAULT_MARKER_FILES
    folder_priorities: tuple[FolderPriority, ...] = ()
    name_priorities: tuple[NamePriority, ...] = ()
    extension_priorities: tuple[ExtensionPriority, ...] = ()
    attribute_priorities: tuple[AttributePriority, ...] = ()
    index_apps: bool = True
    # Control Panel tasks, Settings pages, management consoles and system programs.
    index_windows_tools: bool = True
    # Their English names too, for Windows in another language.
    windows_tools_english: bool = False
    # A `WindowsSettingsDetail`; a string in the JSON file.
    windows_settings_detail: str = WindowsSettingsDetail.EVERY_SETTING.value
    explorer_typing: bool = True
    # Open and Save dialogs: typing searches their folder, Ctrl+G shows Explorer's folder.
    quick_switch: bool = True
    double_ctrl_ms: int = DEFAULT_DOUBLE_CTRL_MS
    # Hide the launcher when it lost focus with nothing typed.
    auto_hide: bool = True
    auto_hide_seconds: int = DEFAULT_AUTO_HIDE_SECONDS
    # Folder watching applies changes live; the full rescan only repairs missed events.
    rescan_seconds: int = 1800
    max_results: int = 60
    content_search_threads: int = DEFAULT_CONTENT_SEARCH_THREADS
    websites: tuple[WebsiteShortcut, ...] = DEFAULT_WEBSITES
    # Own entries in the result context menu, in menu order.
    menu_commands: tuple[MenuCommand, ...] = DEFAULT_MENU_COMMANDS
    appearance: str = Appearance.SYSTEM.value  # An `Appearance`; a string in the JSON file.
    # Search plugins the user configured; plugins without an entry stay off.
    plugins: tuple[PluginConfig, ...] = ()
    # Folders searched for plugins besides the data directory's own `plugins` folder,
    # such as a cloned plugin repository. Read once at start.
    plugin_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.appearance not in tuple(Appearance):
            raise ValueError(f"Appearance must be one of {', '.join(Appearance)}.")
        if self.windows_settings_detail not in tuple(WindowsSettingsDetail):
            raise ValueError(
                f"Windows settings detail must be one of {', '.join(WindowsSettingsDetail)}."
            )
        self._validate_limits()
        self._validate_paths()
        self._validate_markers()
        self._validate_priority_rules()
        self._validate_websites()
        self._validate_plugins()

    @property
    def indexing_key(self) -> tuple:
        """Settings that change catalog membership or stored metadata."""
        return (
            self.roots,
            self.excluded_paths,
            self.excluded_patterns,
            self.marker_files,
            self.folder_priorities,
            self.name_priorities,
            self.extension_priorities,
            self.attribute_priorities,
            self.index_apps,
            self.index_windows_tools,
            self.windows_tools_english,
            self.windows_settings_detail,
        )

    @property
    def input_key(self) -> tuple:
        """Settings that the global keyboard listener reads at start."""
        return self.double_ctrl_ms, self.explorer_typing, self.quick_switch

    def exclusions(self, *, raise_marker_errors: bool = False) -> ExclusionPolicy:
        return ExclusionPolicy(
            self.excluded_paths, self.excluded_patterns, self.marker_files, raise_marker_errors
        )

    def priorities(self) -> PriorityPolicy:
        return PriorityPolicy(
            self.folder_priorities,
            self.name_priorities,
            self.extension_priorities,
            self.attribute_priorities,
        )

    def _validate_limits(self) -> None:
        if type(self.content_search_threads) is not int:
            raise ValueError("Content search threads must be an integer.")
        _require_range(
            self.content_search_threads,
            CONTENT_SEARCH_THREADS_RANGE,
            "Content search threads must be between {low} and {high}.",
        )
        _require_range(
            self.double_ctrl_ms,
            DOUBLE_CTRL_RANGE_MS,
            "Double-Ctrl interval must be between {low} and {high} ms.",
        )
        _require_range(
            self.auto_hide_seconds,
            AUTO_HIDE_RANGE_SECONDS,
            "Auto-close delay must be between {low} and {high} seconds.",
        )
        _require_range(
            self.rescan_seconds,
            RESCAN_RANGE_SECONDS,
            "Scan interval must be between {low} and {high} seconds.",
        )
        _require_range(
            self.max_results, RESULT_LIMIT_RANGE, "Result limit must be between {low} and {high}."
        )

    def _validate_paths(self) -> None:
        if not all(Path(p).is_absolute() for p in (*self.roots, *self.excluded_paths)):
            raise ValueError("Search roots and excluded paths must be absolute.")
        if not all(Path(rule.path).is_absolute() for rule in self.folder_priorities):
            raise ValueError("Priority folders must be absolute paths.")
        if not all(Path(folder).is_absolute() for folder in self.plugin_paths):
            raise ValueError("Plugin folders must be absolute paths.")

    def _validate_markers(self) -> None:
        for marker in self.marker_files:
            if not marker or marker in (".", "..") or any(c in marker for c in "/\\:*?"):
                raise ValueError("Markers must be simple file names.")

    def _validate_websites(self) -> None:
        keywords = set()
        for site in self.websites:
            keyword = normalize(site.keyword)
            if keyword in keywords:
                raise ValueError(
                    f"The website keyword {site.keyword!r} is used twice; "
                    "keywords must differ in more than letter case."
                )
            keywords.add(keyword)

    def _validate_plugins(self) -> None:
        ids = set()
        keywords = {normalize(site.keyword) for site in self.websites}
        for plugin in self.plugins:
            if plugin.id in ids:
                raise ValueError(f"The plugin {plugin.id!r} is configured twice.")
            ids.add(plugin.id)
            _require_range(
                plugin.boost, BOOST_RANGE, "Plugin boosts must be between {low} and {high}."
            )
            keyword = normalize(plugin.keyword)
            if keyword and plugin.enabled:
                if keyword in keywords:
                    raise ValueError(
                        f"The keyword {plugin.keyword!r} of plugin {plugin.id} is already "
                        "used by a website or another plugin."
                    )
                keywords.add(keyword)

    def _validate_priority_rules(self) -> None:
        for rule in self._all_priority_rules():
            if type(rule.weight) is not int:
                raise ValueError("Priority boosts must be integers.")
            _require_range(
                rule.weight, BOOST_RANGE, "Priority boosts must be between {low} and {high}."
            )
            if rule.search_priority not in tuple(SearchPriority):
                raise ValueError(
                    "Search priority must be one of: " + ", ".join(SearchPriority) + "."
                )
        for rule in self.attribute_priorities:
            if type(rule.mask) is not int or rule.mask <= 0:
                raise ValueError("Attribute priority masks must be positive integers.")
        for rule in self.name_priorities:
            if type(rule.files) is not bool or type(rule.folders) is not bool:
                raise ValueError("Filename rule files/folders flags must be JSON booleans.")
        self.priorities()  # Compiles filename expressions; raises ValueError if invalid.

    def _all_priority_rules(self) -> tuple[PriorityRule, ...]:
        return (
            *self.folder_priorities,
            *self.name_priorities,
            *self.extension_priorities,
            *self.attribute_priorities,
        )


_RULE_TYPES = {
    "folder_priorities": FolderPriority,
    "name_priorities": NamePriority,
    "extension_priorities": ExtensionPriority,
    "attribute_priorities": AttributePriority,
}


def priority_rules(name: str, entries: object) -> tuple:
    """Build the rules stored under settings field `name` from JSON values."""
    rule_type = _RULE_TYPES[name]
    if not isinstance(entries, list):
        raise ValueError(f"{name} must be a list of objects.")
    rules = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{name}[{index}] must be an object.")
        try:
            rules.append(rule_type(**entry))
        except TypeError as exc:
            raise ValueError(f"{name}[{index}]: {exc}") from None
    return tuple(rules)


def settings_from_json(data: object) -> Settings:
    """Missing fields keep their defaults; present fields must have the default's type."""
    if not isinstance(data, dict):
        raise ValueError("The settings file must contain a JSON object.")
    values = {}
    for setting in fields(Settings):
        if setting.name in data:
            values[setting.name] = _setting_value(setting.name, data[setting.name])
    return Settings(**values)


# Settings fields holding a list of records, with the record type and its name in messages.
_RECORD_TYPES = {
    "websites": (WebsiteShortcut, "website"),
    "menu_commands": (MenuCommand, "command"),
    "plugins": (PluginConfig, "plugin"),
}


def _setting_value(name: str, value: object) -> object:
    if name in _RECORD_TYPES:
        record_type, label = _RECORD_TYPES[name]
        if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
            raise ValueError(f"{name} must be a list of objects.")
        try:
            return tuple(record_type(**entry) for entry in value)
        except TypeError as exc:
            raise ValueError(f"Invalid {label}: {exc}") from None
    if name in _RULE_TYPES:
        return priority_rules(name, value)
    default = getattr(_DEFAULTS, name)
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{name} must be a list of strings.")
        return tuple(value)
    if type(value) is not type(default):
        raise ValueError(f"{name} must be a {type(default).__name__}.")
    return value


_DEFAULTS = Settings(roots=())


class SettingsFile:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        return settings_from_json(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, settings: Settings) -> None:
        """Write atomically so a crash never leaves a truncated settings file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        text = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.path)

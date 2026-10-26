"""Plugin sources: local git repositories stand in for remote ones, so no test needs a network."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QWidget

from winlauncher.plugins.config import DEFAULT_SOURCES, PluginConfig, source_key
from winlauncher.plugins.encoding import decode_text, read_python_source
from winlauncher.plugins.folders import ManifestError, SourceError, read_manifest, read_source
from winlauncher.plugins.packages import PackageError, install_command, install_packages
from winlauncher.plugins.registry import discover_plugins
from winlauncher.plugins.sources import (
    INSTALLED_FROM_FILE,
    InstallState,
    PluginSources,
    SourceChangeError,
    SourceKind,
    source_kind,
)
from winlauncher.releases import Installation
from winlauncher.settings import Settings, SettingsFile, settings_from_json

PLUGIN_SOURCE = (
    "from winlauncher.plugins.api import Plugin, PluginItem\n"
    "class Listed(Plugin):\n"
    "    id = {plugin_id!r}\n"
    "    label = {label!r}\n"
    "    def refresh(self, context):\n"
    "        return [PluginItem('1', 'Only item')]\n"
)


def write_plugin(folder: Path, version: str = "", **manifest: object) -> None:
    """A plugin folder: plugin.py and, with a version, its plugin.toml."""
    folder.mkdir(parents=True, exist_ok=True)
    plugin_id = folder.name.lower()
    (folder / "plugin.py").write_text(
        PLUGIN_SOURCE.format(plugin_id=plugin_id, label=plugin_id.title()), encoding="utf-8"
    )
    if version:
        lines = [f"version = {json.dumps(version)}"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in manifest.items()]
        (folder / "plugin.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class Repository:
    """A git repository laid out like WinLauncher-Plugins, standing in for a remote."""

    def __init__(self, folder: Path, name: str = "Test plugins"):
        self.folder = folder
        folder.mkdir(parents=True)
        git("init", "--quiet", "--initial-branch=main", cwd=folder)
        (folder / "catalog.toml").write_text(f'name = "{name}"\n', encoding="utf-8")

    @property
    def location(self) -> str:
        # A file URL: a plain path would be a local source, read without git.
        return self.folder.as_uri()

    def add_plugin(self, folder: str, version: str, **manifest: object) -> None:
        write_plugin(self.folder / "plugins" / folder, version, **manifest)

    def commit(self) -> None:
        git("add", "-A", cwd=self.folder)
        git("commit", "--quiet", "-m", "change", cwd=self.folder)


def delete_repository(folder: Path) -> None:
    """Git marks its object files read-only, which Windows refuses to delete."""
    for path in folder.rglob("*"):
        path.chmod(0o700)
    shutil.rmtree(folder)


class SourceLayoutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-layout-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_source_is_a_catalog_a_folder_of_plugins_or_one_plugin(self):
        catalog = self.root / "catalog"
        (catalog / "catalog.toml").parent.mkdir()
        (catalog / "catalog.toml").write_text('name = "Team"\nplugins = "src"', encoding="utf-8")
        write_plugin(catalog / "src" / "jira", "1.0.0", name="Jira")
        write_plugin(catalog / "src" / "_template", "1.0.0")
        contents = read_source(catalog)
        self.assertEqual(contents.name, "Team")
        self.assertEqual([plugin.name for plugin in contents.plugins], ["Jira"])

        folder = self.root / "folder"
        write_plugin(folder / "notes")
        (folder / "docs").mkdir()  # No plugin.py: no plugin, and no problem.
        self.assertEqual([p.name for p in read_source(folder).plugins], ["notes"])

        single = self.root / "single"
        write_plugin(single, "2.0.0")
        self.assertEqual([p.folder for p in read_source(single).plugins], [single])

    def test_plugin_folders_keep_the_spelling_the_source_was_given_in(self):
        """An activated folder must lie in its source as the user typed it, short names too."""
        import win32api

        source = self.root / "A Long Folder Name"
        write_plugin(source / "plugins" / "jira")
        (source / "catalog.toml").write_text('plugins = "plugins"', encoding="utf-8")
        short = Path(win32api.GetShortPathName(str(source)))
        if short == source:
            self.skipTest("this volume keeps no 8.3 short names")
        self.assertEqual(read_source(short).plugins[0].folder, short / "plugins" / "jira")

    def test_unusable_sources_and_manifests_are_reported(self):
        with self.assertRaisesRegex(SourceError, "does not exist"):
            read_source(self.root / "missing")
        escaping = self.root / "escaping"
        escaping.mkdir()
        (escaping / "catalog.toml").write_text('plugins = ".."', encoding="utf-8")
        with self.assertRaisesRegex(SourceError, "inside the source"):
            read_source(escaping)
        broken = self.root / "broken"
        write_plugin(broken / "good", "1.0.0")
        write_plugin(broken / "bad", "1.0.0", requires="telethon")
        contents = read_source(broken)
        self.assertEqual([plugin.name for plugin in contents.plugins], ["good"])
        self.assertIn("bad: requires must be a list", " ".join(contents.problems))

    def test_a_manifest_names_an_icon_inside_its_plugin_folder(self):
        write_plugin(self.root / "chats", "1.0.0", icon="assets/chats.svg")
        (self.root / "chats" / "assets").mkdir()
        (self.root / "chats" / "assets" / "chats.svg").write_text("<svg/>", encoding="utf-8")
        write_plugin(self.root / "outside", "1.0.0", icon="../chats/assets/chats.svg")
        write_plugin(self.root / "missing", "1.0.0", icon="assets/none.svg")
        offered = {plugin.folder.name: plugin for plugin in read_source(self.root).plugins}
        self.assertEqual(offered["chats"].icon, self.root / "chats" / "assets" / "chats.svg")
        self.assertIsNone(offered["outside"].icon)  # Never a file outside the plugin.
        self.assertIsNone(offered["missing"].icon)
        (self.root / "chats" / "plugin.toml").write_text("icon = 1", encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "the icon must be text"):
            read_manifest(self.root / "chats")

    def test_manifests_name_their_mistakes(self):
        (self.root / "plugin.toml").write_text("version = 1", encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "the version must be text"):
            read_manifest(self.root)
        (self.root / "plugin.toml").unlink()
        self.assertEqual(read_manifest(self.root).version, "")  # No manifest is fine.

    def test_activated_plugin_folders_load_from_where_they_lie(self):
        write_plugin(self.root / "src" / "jira", "1.0.0")
        write_plugin(self.root / "src" / "wiki")
        with patch("winlauncher.plugins.registry.entry_points", return_value=()):
            found = discover_plugins(
                [self.root / "own"], [self.root / "src" / "jira", self.root / "gone"]
            )
        self.assertEqual([plugin.id for plugin in found.plugins], ["jira"])  # Not wiki.
        self.assertEqual(found.sources["jira"].folder, self.root / "src" / "jira")
        self.assertIn("activated plugin folder is gone", " ".join(found.problems))


class EncodingTest(unittest.TestCase):
    """Plugin files load whether an editor saved them as UTF-8 or as Windows-1252."""

    def test_manifests_and_plugins_in_windows_1252_load(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "notes"
            folder.mkdir()
            (folder / "plugin.toml").write_bytes('name = "Notizen für Büro"'.encode("cp1252"))
            source = PLUGIN_SOURCE.format(plugin_id="notes", label="Notizen für Büro")
            (folder / "plugin.py").write_bytes(("# Grüße\n" + source).encode("cp1252"))
            self.assertEqual(read_manifest(folder).name, "Notizen für Büro")
            with patch("winlauncher.plugins.registry.entry_points", return_value=()):
                found = discover_plugins([], [folder])
            self.assertEqual(found.problems, [])
            self.assertEqual(found.plugins[0].label, "Notizen für Büro")

    def test_utf8_with_a_byte_order_mark_and_coding_declarations_are_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "plugin.toml").write_bytes('name = "Grüße"'.encode("utf-8-sig"))
            self.assertEqual(read_manifest(folder).name, "Grüße")
            declared = "# -*- coding: latin-1 -*-\nNAME = 'Grüße'\n"
            (folder / "declared.py").write_bytes(declared.encode("latin-1"))
            self.assertIn("'Grüße'", read_python_source(folder / "declared.py"))

    def test_tool_output_in_either_encoding_is_readable(self):
        denied = "Zugriff verweigert: Größe"
        self.assertEqual(decode_text(denied.encode("cp1252")), denied)
        self.assertEqual(decode_text("Größe".encode()), "Größe")
        self.assertEqual(decode_text(b"\x81"), "\ufffd")  # Undefined even in Windows-1252.


class SourceKindTest(unittest.TestCase):
    def test_drive_and_share_paths_are_local_and_anything_else_is_for_git(self):
        self.assertIs(source_kind(r"D:\Plugins"), SourceKind.LOCAL)
        self.assertIs(source_kind(r"\\server\share\plugins"), SourceKind.LOCAL)
        for remote in ("https://github.com/x/y.git", "git@github.com:x/y.git", "file:///D:/x"):
            with self.subTest(remote=remote):
                self.assertIs(source_kind(remote), SourceKind.REMOTE)

    def test_source_keys_ignore_case_trailing_slashes_and_the_git_suffix(self):
        self.assertEqual(source_key(" https://X.org/Plugins.GIT/ "), "https://x.org/plugins")


@unittest.skipUnless(shutil.which("git"), "remote plugin sources need git")
class RemoteSourceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-sources-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repository = Repository(root / "remote")
        self.repository.add_plugin(
            "chats", "1.0.0", name="Chats", description="Your chats.", requires=["telethon>=1"]
        )
        self.repository.add_plugin("records", "2.0.0")
        self.repository.commit()
        self.installed = root / "data" / "plugins"
        self.library = PluginSources(root / "data" / "plugin-sources", self.installed)

    def test_a_remote_source_lists_its_plugins_from_their_manifests(self):
        source = self.library.fetch(self.repository.location)
        self.assertEqual(
            (source.kind, source.title, source.error), (SourceKind.REMOTE, "Test plugins", "")
        )
        chats, records = source.offers
        self.assertEqual((chats.name, chats.version), ("Chats", "1.0.0"))
        self.assertEqual(chats.plugin.manifest.requires, ("telethon>=1",))
        self.assertEqual((records.name, records.install), ("records", InstallState.AVAILABLE))

    def test_install_update_and_uninstall_follow_the_source(self):
        source = self.library.fetch(self.repository.location)
        installed = self.library.install(source.offers[0])
        self.assertEqual(installed.install, InstallState.INSTALLED)
        self.assertEqual(
            self.library.read(source.location).offers[0].install, InstallState.INSTALLED
        )
        # The installed copy loads like any plugin of the own folder; staging never does.
        found = discover_plugins([self.installed])
        self.assertEqual([plugin.id for plugin in found.plugins], ["chats"])
        self.assertEqual(found.sources["chats"].version, "1.0.0")

        self.repository.add_plugin("chats", "1.1.0", name="Chats")
        self.repository.commit()
        offered = self.library.fetch(source.location).offers[0]
        self.assertEqual(
            (offered.install, offered.installed_version), (InstallState.UPDATE, "1.0.0")
        )
        self.library.install(offered)
        self.assertEqual(read_manifest(self.installed / "chats").version, "1.1.0")
        self.assertEqual([path.name for path in self.installed.iterdir()], ["chats"])

        self.assertEqual(self.library.uninstall(offered).install, InstallState.AVAILABLE)
        self.assertFalse((self.installed / "chats").exists())

    def test_a_private_plugin_of_the_same_name_is_left_alone(self):
        write_plugin(self.installed / "records")
        records = self.library.fetch(self.repository.location).offers[1]
        self.assertEqual(records.install, InstallState.TAKEN)
        with self.assertRaisesRegex(SourceChangeError, "did not come from this source"):
            self.library.install(records)
        with self.assertRaisesRegex(SourceChangeError, "not installed from this source"):
            self.library.uninstall(records)
        self.assertFalse((self.installed / "records" / INSTALLED_FROM_FILE).exists())

    def test_an_unreachable_source_keeps_listing_what_it_knew(self):
        self.library.fetch(self.repository.location)
        delete_repository(self.repository.folder)
        source = self.library.fetch(self.repository.location)
        self.assertIn("Could not be checked", source.error)
        self.assertEqual([offer.folder.name for offer in source.offers], ["chats", "records"])
        never = self.library.fetch((self.repository.folder.parent / "missing").as_uri())
        self.assertIn("Could not be checked", never.error)
        self.assertEqual(never.offers, ())

    def test_clones_of_sources_no_longer_listed_are_deleted(self):
        self.library.fetch(self.repository.location)
        self.assertEqual(len(list(self.library.clones.iterdir())), 1)
        self.library.prune([])
        self.assertEqual(list(self.library.clones.iterdir()), [])


class LocalSourceTest(unittest.TestCase):
    def test_a_local_source_is_read_where_it_lies_and_never_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_plugin(root / "work" / "jira", "0.1.0", name="Jira")
            library = PluginSources(root / "clones", root / "plugins")
            source = library.fetch(str(root / "work"))  # No git involved.
            self.assertEqual(source.kind, SourceKind.LOCAL)
            self.assertEqual(source.offers[0].folder, root / "work" / "jira")
            self.assertFalse((root / "clones").exists())


class PackagesTest(unittest.TestCase):
    def test_requirements_install_into_winlauncher_s_own_environment(self):
        requirements = ("telethon>=1.34,<1.45",)
        self.assertEqual(
            install_command(Installation.PIPX, requirements),
            'pipx inject winlauncher "telethon>=1.34,<1.45"',
        )
        python = Path(r"C:\venv\Scripts\pythonw.exe")
        self.assertEqual(
            install_command(Installation.CHECKOUT, requirements, python),
            r'C:\venv\Scripts\python.exe -m pip install "telethon>=1.34,<1.45"',
        )

    def test_a_failed_installation_names_the_command_to_run(self):
        failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"ERROR: No matching dist\n")
        with patch("winlauncher.plugins.packages.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(PackageError, "No matching dist(.|\n)*pip install"):
                install_packages(Installation.PACKAGE, ("nothing-like-it",))
        with patch("winlauncher.plugins.packages.subprocess.run") as run:
            install_packages(Installation.PACKAGE, ())
        run.assert_not_called()


class SourceSettingsTest(unittest.TestCase):
    def test_the_default_source_is_listed_and_edits_roundtrip(self):
        self.assertEqual(Settings(roots=()).plugin_sources, DEFAULT_SOURCES)
        settings = Settings(
            roots=(),
            plugin_sources=("https://git.example.org/plugins.git", r"D:\Work\plugins"),
            active_plugins=(r"D:\Work\plugins\jira",),
        )
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)

    def test_duplicates_blanks_and_relative_activations_are_refused(self):
        with self.assertRaisesRegex(ValueError, "listed twice"):
            Settings(
                roots=(), plugin_sources=("https://x.org/Plugins.git", "https://x.org/plugins/")
            )
        with self.assertRaisesRegex(ValueError, "need a repository"):
            Settings(roots=(), plugin_sources=(" ",))
        with self.assertRaisesRegex(ValueError, "absolute"):
            Settings(roots=(), active_plugins=("jira",))
        with self.assertRaisesRegex(ValueError, "activated twice"):
            Settings(roots=(), active_plugins=(r"D:\a\jira", r"d:\A\Jira"))

    def test_plugin_folders_and_catalogs_of_older_versions_become_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "WinLauncher-Plugins"
            (clone / "catalog.toml").parent.mkdir()
            (clone / "catalog.toml").write_text('name = "Team"', encoding="utf-8")
            write_plugin(clone / "plugins" / "jira", "1.0.0")
            loose = Path(directory) / "loose"
            write_plugin(loose / "wiki")
            settings = settings_from_json(
                {
                    "roots": [],
                    "plugin_catalogs": ["https://git.example.org/team.git"],
                    "plugin_paths": [str(clone), str(loose)],
                }
            )
        self.assertEqual(
            settings.plugin_sources,
            ("https://git.example.org/team.git", str(clone), str(loose)),
        )
        # Whatever loaded before still loads: the repository root and a plain folder alike.
        self.assertEqual(
            settings.active_plugins, (str(clone / "plugins" / "jira"), str(loose / "wiki"))
        )
        self.assertEqual(
            settings_from_json({"roots": [], "plugin_paths": []}).plugin_sources, DEFAULT_SOURCES
        )


@unittest.skipUnless(shutil.which("git"), "remote plugin sources need git")
class SourceWatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from test_ui_flow import DeferredTasks

        from winlauncher.ui.plugin_sources import SourceWatcher

        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-source-ui-")
        self.addCleanup(temporary.cleanup)
        root = self.root = Path(temporary.name)
        self.repository = Repository(root / "remote")
        self.repository.add_plugin("chats", "1.0.0", name="Chats")
        self.repository.commit()
        write_plugin(root / "work" / "jira", "0.1.0", name="Jira", requires=["jira>=3"])
        self.settings = Settings(
            roots=(), plugin_sources=(self.repository.location, str(root / "work"))
        )
        self.library = PluginSources(root / "clones", root / "plugins")
        self.tasks = DeferredTasks()
        self.watcher = SourceWatcher(self.library, self.tasks, lambda: self.settings)
        self.addCleanup(self.watcher.close)
        self.outcomes, self.updates = [], []
        self.watcher.finished.connect(self.outcomes.append)
        self.watcher.updates_found.connect(self.updates.append)
        packages = patch("winlauncher.ui.plugin_sources.install_packages")
        self.install_packages = packages.start()
        self.addCleanup(packages.stop)

    def finish(self):
        from winlauncher.ui.tasks import TaskCategory

        self.tasks.finish(TaskCategory.PLUGIN_SOURCE)

    def checked(self):
        self.watcher.check()
        self.finish()
        return self.watcher.sources

    def test_install_brings_its_packages_and_each_update_is_announced_once(self):
        remote, _local = self.checked()
        self.watcher.install(remote.offers[0])
        self.assertTrue(self.watcher.working)
        self.finish()
        self.assertEqual(self.watcher.sources[0].offers[0].install, InstallState.INSTALLED)
        (outcome,) = self.outcomes
        self.assertFalse(outcome.failed)
        self.assertEqual(outcome.loads_from, self.root / "plugins" / "chats")
        self.install_packages.assert_called_once()

        self.repository.add_plugin("chats", "1.1.0", name="Chats")
        self.repository.commit()
        self.checked()
        self.checked()
        self.assertEqual(
            [[offer.version for offer in found] for found in self.updates], [["1.1.0"]]
        )

    def test_activation_installs_the_packages_of_a_local_plugin(self):
        _remote, local = self.checked()
        self.watcher.activate(local.offers[0])
        self.finish()
        (outcome,) = self.outcomes
        self.assertEqual(outcome.loads_from, self.root / "work" / "jira")
        self.assertEqual(self.install_packages.call_args.args[1], ("jira>=3",))

    def test_a_failed_package_installation_installs_nothing(self):
        remote, _local = self.checked()
        self.install_packages.side_effect = PackageError("No matching distribution.")
        self.watcher.install(remote.offers[0])
        job = self.tasks.jobs[-1]
        job.done = True
        try:
            job.work()
        except PackageError as exc:
            job.on_failure(str(exc))
        self.assertEqual(
            [(o.error, o.failed) for o in self.outcomes], [("No matching distribution.", True)]
        )
        self.assertFalse((self.root / "plugins" / "chats").exists())
        self.assertFalse(self.watcher.working)

    def test_an_edited_source_list_is_checked_at_once(self):
        self.checked()
        self.watcher.apply()  # Unchanged: nothing to do.
        self.assertFalse(self.watcher.checking)
        self.settings = Settings(roots=(), plugin_sources=())
        self.watcher.apply()
        self.assertTrue(self.watcher.checking)

    def test_the_settings_page_installs_remote_and_activates_local_plugins(self):
        from winlauncher.ui.source_settings import SourceSection

        self.checked()
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        turned_on = []
        section = SourceSection(self.settings, self.watcher, set, turned_on.append, None, parent)
        self.assertEqual(
            [section.list.item(i).text() for i in range(section.list.count())],
            [
                "Chats  1.0.0\nTest plugins  ·  Not installed",
                f"Jira  0.1.0\n{self.root / 'work'}  ·  Not active",
            ],
        )
        self.assertEqual(section.main_button.text(), "Install and activate")
        self.assertFalse(section.uninstall_button.isHidden())

        section.list.setCurrentRow(1)
        self.assertEqual(section.main_button.text(), "Activate")
        self.assertTrue(section.uninstall_button.isHidden())
        with (
            patch("winlauncher.ui.source_settings.ask", return_value=True),
            patch("winlauncher.ui.source_settings.message"),
        ):
            section.run_main_action()
            self.finish()
        jira = str(self.root / "work" / "jira")
        self.assertEqual(section.active_plugins(), (jira,))
        self.assertEqual(turned_on, [Path(jira)])
        self.assertIn("Active from the next start", section.list.item(1).text())
        self.assertEqual(section.main_button.text(), "Deactivate")
        section.run_main_action()
        self.assertEqual(section.active_plugins(), ())

        section.locations.setPlainText(self.repository.location + "\n\n")
        section.active = [jira]  # Its source is no longer listed, so it is dropped.
        self.assertEqual(section.source_locations(), (self.repository.location,))
        self.assertEqual(section.active_plugins(), ())


class EntryListTest(unittest.TestCase):
    def test_a_short_list_shows_every_entry_without_scrolling(self):
        from winlauncher.ui.theme import Appearance, theme
        from winlauncher.ui.widgets import EntryList, line_icon

        QApplication.instance() or QApplication([])
        theme().use(Appearance.LIGHT)  # The style sheet gives the list its frame.
        entries = EntryList("Plugins")
        self.addCleanup(entries.deleteLater)
        for name in ("Chats\nInstalled", "Tickets\nActive"):
            entries.add_entry(line_icon("plug"), name)
        entries.show()
        QApplication.processEvents()
        self.assertEqual(entries.verticalScrollBar().maximum(), 0)


class DefaultOnTest(unittest.TestCase):
    def test_a_plugin_without_settings_is_on(self):
        self.assertTrue(PluginConfig("x").enabled)


if __name__ == "__main__":
    unittest.main()

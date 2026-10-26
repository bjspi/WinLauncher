"""Plugin catalogs: local git repositories stand in for remote ones, so no test needs a network."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from winlauncher.plugins.catalog import (
    INSTALLED_FROM_FILE,
    CatalogError,
    InstallState,
    PluginCatalogs,
)
from winlauncher.plugins.config import DEFAULT_CATALOGS, catalog_key
from winlauncher.plugins.manifest import ManifestError, read_manifest
from winlauncher.plugins.registry import discover_plugins
from winlauncher.releases import Installation, requirements_command
from winlauncher.settings import Settings, SettingsFile, settings_from_json

PLUGIN_SOURCE = (
    "from winlauncher.plugins.api import Plugin, PluginItem\n"
    "class Listed(Plugin):\n"
    "    id = {plugin_id!r}\n"
    "    label = {label!r}\n"
    "    def refresh(self, context):\n"
    "        return [PluginItem('1', 'Only item')]\n"
)


def git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class CatalogRepository:
    """A git repository laid out like WinLauncher-Plugins."""

    def __init__(self, folder: Path, name: str = "Test plugins"):
        self.folder = folder
        folder.mkdir(parents=True)
        git("init", "--quiet", "--initial-branch=main", cwd=folder)
        (folder / "catalog.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
        (folder / "plugins").mkdir()

    @property
    def url(self) -> str:
        return str(self.folder)

    def add_plugin(self, plugin_folder: str, version: str, **manifest: object) -> None:
        folder = self.folder / "plugins" / plugin_folder
        folder.mkdir(exist_ok=True)
        (folder / "plugin.py").write_text(
            PLUGIN_SOURCE.format(plugin_id=plugin_folder, label=plugin_folder.title()),
            encoding="utf-8",
        )
        lines = [f'version = "{version}"']
        lines += [f"{key} = {_toml(value)}" for key, value in manifest.items()]
        (folder / "plugin.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def commit(self) -> None:
        git("add", "-A", cwd=self.folder)
        git("commit", "--quiet", "-m", "change", cwd=self.folder)


def delete_repository(folder: Path) -> None:
    """Git marks its object files read-only, which Windows refuses to delete."""
    for path in folder.rglob("*"):
        path.chmod(0o700)
    shutil.rmtree(folder)


def _toml(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    return f'"{value}"'


@unittest.skipUnless(shutil.which("git"), "plugin catalogs need git")
class PluginCatalogsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-catalogs-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repository = CatalogRepository(root / "remote")
        self.repository.add_plugin(
            "chats", "1.0.0", name="Chats", description="Your chats.", requires=["telethon>=1"]
        )
        self.repository.add_plugin("records", "2.0.0")
        (self.repository.folder / "plugins" / "_template").mkdir()  # Never offered.
        (self.repository.folder / "plugins" / "_template" / "plugin.py").write_text("")
        self.repository.commit()
        self.installed = root / "data" / "plugins"
        self.library = PluginCatalogs(root / "data" / "plugin-catalogs", self.installed)

    def test_a_catalog_lists_its_plugins_from_their_manifests(self):
        catalog = self.library.fetch(self.repository.url)
        self.assertEqual((catalog.title, catalog.error), ("Test plugins", ""))
        chats, records = catalog.plugins
        self.assertEqual((chats.name, chats.version), ("Chats", "1.0.0"))
        self.assertEqual(chats.manifest.description, "Your chats.")
        self.assertEqual(chats.manifest.requires, ("telethon>=1",))
        self.assertEqual((records.name, records.state), ("records", InstallState.AVAILABLE))

    def test_install_update_and_remove_follow_the_catalog(self):
        catalog = self.library.fetch(self.repository.url)
        installed = self.library.install(catalog.url, catalog.plugins[0])
        self.assertEqual(installed.state, InstallState.INSTALLED)
        self.assertTrue((self.installed / "chats" / "plugin.py").is_file())
        self.assertEqual(self.library.read(catalog.url).plugins[0].state, InstallState.INSTALLED)
        # The installed plugin loads like any other; the staging folders never do.
        found = discover_plugins([self.installed])
        self.assertEqual([plugin.id for plugin in found.plugins], ["chats"])
        self.assertEqual(found.sources["chats"].version, "1.0.0")

        self.repository.add_plugin("chats", "1.1.0", name="Chats")
        self.repository.commit()
        offered = self.library.fetch(catalog.url).plugins[0]
        self.assertEqual((offered.state, offered.installed_version), (InstallState.UPDATE, "1.0.0"))
        self.library.install(catalog.url, offered)
        self.assertEqual(read_manifest(self.installed / "chats" / "plugin.toml").version, "1.1.0")
        self.assertEqual(sorted(path.name for path in self.installed.iterdir()), ["chats"])

        removed = self.library.remove(catalog.url, offered)
        self.assertEqual(removed.state, InstallState.AVAILABLE)
        self.assertFalse((self.installed / "chats").exists())

    def test_a_private_plugin_of_the_same_name_is_left_alone(self):
        private = self.installed / "records"
        private.mkdir(parents=True)
        (private / "plugin.py").write_text("# mine", encoding="utf-8")
        records = self.library.fetch(self.repository.url).plugins[1]
        self.assertEqual(records.state, InstallState.TAKEN)
        with self.assertRaisesRegex(CatalogError, "did not come from this catalog"):
            self.library.install(self.repository.url, records)
        with self.assertRaisesRegex(CatalogError, "not installed from this catalog"):
            self.library.remove(self.repository.url, records)
        self.assertEqual((private / "plugin.py").read_text(encoding="utf-8"), "# mine")

    def test_the_same_repository_is_recognized_under_another_spelling(self):
        catalog = self.library.fetch(self.repository.url)
        self.library.install(catalog.url, catalog.plugins[0])
        spelled = self.repository.url.upper() + "\\"
        self.assertEqual(self.library.read(spelled).plugins[0].state, InstallState.INSTALLED)
        self.assertEqual(
            (self.installed / "chats" / INSTALLED_FROM_FILE).read_text(encoding="utf-8").strip(),
            self.repository.url,
        )

    def test_an_unreachable_catalog_keeps_listing_what_it_knew(self):
        self.library.fetch(self.repository.url)
        delete_repository(self.repository.folder)
        catalog = self.library.fetch(self.repository.url)
        self.assertIn("Could not be checked", catalog.error)
        self.assertEqual([plugin.folder for plugin in catalog.plugins], ["chats", "records"])
        never = self.library.fetch(str(self.repository.folder.parent / "missing"))
        self.assertIn("Could not be checked", never.error)
        self.assertEqual(never.plugins, ())

    def test_a_repository_without_catalog_file_or_with_an_escaping_folder_is_refused(self):
        (self.repository.folder / "catalog.toml").unlink()
        self.repository.commit()
        self.assertIn("no catalog.toml", self.library.fetch(self.repository.url).error)
        (self.repository.folder / "catalog.toml").write_text('plugins = ".."', encoding="utf-8")
        self.repository.commit()
        self.assertIn("inside the catalog", self.library.fetch(self.repository.url).error)

    def test_unusable_manifests_are_reported_and_the_rest_offered(self):
        (self.repository.folder / "plugins" / "records" / "plugin.toml").write_text(
            "requires = 'telethon'", encoding="utf-8"
        )
        self.repository.commit()
        catalog = self.library.fetch(self.repository.url)
        self.assertEqual([plugin.folder for plugin in catalog.plugins], ["chats"])
        self.assertIn("records: requires must be a list", " ".join(catalog.problems))

    def test_clones_of_catalogs_no_longer_configured_are_deleted(self):
        self.library.fetch(self.repository.url)
        self.assertEqual(len(list(self.library.mirrors.iterdir())), 1)
        self.library.prune([])
        self.assertEqual(list(self.library.mirrors.iterdir()), [])


class CatalogSettingsTest(unittest.TestCase):
    def test_the_default_catalog_is_configured_and_edits_roundtrip(self):
        self.assertEqual(Settings(roots=()).plugin_catalogs, DEFAULT_CATALOGS)
        self.assertEqual(settings_from_json({"roots": []}).plugin_catalogs, DEFAULT_CATALOGS)
        settings = Settings(roots=(), plugin_catalogs=("https://git.example.org/plugins.git",))
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)
        self.assertEqual(Settings(roots=(), plugin_catalogs=()).plugin_catalogs, ())

    def test_a_catalog_listed_twice_or_blank_is_refused(self):
        with self.assertRaisesRegex(ValueError, "listed twice"):
            Settings(
                roots=(),
                plugin_catalogs=("https://x.org/Plugins.git", "https://x.org/plugins/"),
            )
        with self.assertRaisesRegex(ValueError, "need a repository"):
            Settings(roots=(), plugin_catalogs=(" ",))

    def test_catalog_keys_ignore_case_trailing_slashes_and_the_git_suffix(self):
        self.assertEqual(catalog_key(" https://X.org/Plugins.GIT/ "), "https://x.org/plugins")

    def test_manifests_name_their_mistakes(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "plugin.toml"
            manifest.write_text("version = 1", encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "the version must be text"):
                read_manifest(manifest)
            manifest.write_text("requires = [1]", encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "requires must be a list"):
                read_manifest(manifest)

    def test_requirements_install_into_winlauncher_s_own_environment(self):
        requirements = ("telethon>=1.34,<1.45",)
        self.assertEqual(
            requirements_command(Installation.PIPX, requirements),
            'pipx inject winlauncher "telethon>=1.34,<1.45"',
        )
        python = Path(r"C:\venv\Scripts\pythonw.exe")
        self.assertEqual(
            requirements_command(Installation.CHECKOUT, requirements, python),
            r'"C:\venv\Scripts\python.exe" -m pip install "telethon>=1.34,<1.45"',
        )


@unittest.skipUnless(shutil.which("git"), "plugin catalogs need git")
class CatalogWatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from test_ui_flow import DeferredTasks

        from winlauncher.ui.plugin_catalogs import CatalogWatcher

        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-catalog-ui-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repository = CatalogRepository(root / "remote")
        self.repository.add_plugin("chats", "1.0.0", name="Chats")
        self.repository.commit()
        self.settings = Settings(roots=(), plugin_catalogs=(self.repository.url,))
        self.library = PluginCatalogs(root / "clones", root / "plugins")
        self.tasks = DeferredTasks()
        self.watcher = CatalogWatcher(self.library, self.tasks, lambda: self.settings)
        self.addCleanup(self.watcher.close)
        self.outcomes = []
        self.watcher.finished.connect(self.outcomes.append)
        self.updates = []
        self.watcher.updates_found.connect(self.updates.append)

    def finish(self):
        from winlauncher.ui.tasks import TaskCategory

        self.tasks.finish(TaskCategory.PLUGIN_CATALOG)

    def test_check_install_and_announce_each_update_once(self):
        self.watcher.check()
        self.assertTrue(self.watcher.checking)
        self.finish()
        self.assertFalse(self.watcher.checking)
        catalog = self.watcher.catalogs[0]
        self.watcher.install(catalog, catalog.plugins[0])
        self.assertTrue(self.watcher.working)
        self.finish()
        self.assertEqual(self.watcher.catalogs[0].plugins[0].state, InstallState.INSTALLED)
        self.assertEqual(len(self.outcomes), 1)
        self.assertFalse(self.outcomes[0].failed)
        self.assertIn("Chats is installed. Plugins are loaded at start", self.outcomes[0].text)

        self.repository.add_plugin("chats", "1.1.0", name="Chats")
        self.repository.commit()
        for _ in range(2):
            self.watcher.check()
            self.finish()
        self.assertEqual(
            [[plugin.version for plugin in found] for found in self.updates], [["1.1.0"]]
        )

    def test_a_failed_install_reports_its_reason(self):
        self.watcher.check()
        self.finish()
        catalog = self.watcher.catalogs[0]
        with patch.object(self.library, "install", side_effect=CatalogError("Disk full.")):
            self.watcher.install(catalog, catalog.plugins[0])
            job = self.tasks.jobs[-1]
            job.done = True
            try:
                job.work()
            except CatalogError as exc:
                job.on_failure(str(exc))
        self.assertEqual([(o.text, o.failed) for o in self.outcomes], [("Disk full.", True)])
        self.assertFalse(self.watcher.working)

    def test_an_edited_catalog_list_is_checked_at_once(self):
        self.watcher.check()
        self.finish()
        self.watcher.apply()  # Unchanged: nothing to do.
        self.assertFalse(self.watcher.checking)
        self.settings = Settings(roots=(), plugin_catalogs=())
        self.watcher.apply()
        self.assertTrue(self.watcher.checking)

    def test_the_settings_page_offers_the_plugins_and_edits_the_catalogs(self):
        from winlauncher.ui.catalog_settings import CatalogSection

        self.watcher.check()
        self.finish()
        from PySide6.QtWidgets import QWidget

        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        section = CatalogSection(self.settings, self.watcher, parent)
        self.assertEqual(section.list.count(), 1)
        self.assertEqual(
            section.list.item(0).text(), "Chats  1.0.0\nTest plugins  ·  Not installed"
        )
        self.assertTrue(section.install_button.isEnabled())
        self.assertFalse(section.remove_button.isEnabled())
        section.urls.setPlainText("https://git.example.org/plugins.git\n\n")
        self.assertEqual(section.catalog_urls(), ("https://git.example.org/plugins.git",))


if __name__ == "__main__":
    unittest.main()

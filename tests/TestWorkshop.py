import io
import json
import os
import shutil
import stat
import unittest
import uuid
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PySide6.QtWidgets import QApplication, QWidget

from src.char.core.CharRegistry import char_registry
from src.char.custom.CustomCharDb import CustomCharDb
from src.char.custom.CustomCharManager import CustomCharManager
from src.char.workshop.archive import MAX_ARCHIVE_BYTES, ArchiveContents, load_archive
from src.char.workshop.models import (
    CatalogEntry,
    PackageSlot,
    TeamPackage,
    WorkshopFormatError,
    filter_catalog_entries,
    group_catalog_entries,
    parse_catalog,
    parse_version,
)
from src.char.workshop.repository import IndexSource, WorkshopRepository, WorkshopRepositoryError
from src.char.workshop.service import WorkshopInstallError, WorkshopPackageService
from src.ui.features.characters.workshop_dialog import WorkshopDialog

SOURCE = (
    "from src.char.BaseChar import BaseChar, Element\n\n"
    "class SampleExternal(BaseChar):\n"
    "    cn_name = '示例角色'\n"
    "    en_name = 'SampleExternal'\n"
    "    element = Element.PURPLE\n"
)


class TestWorkshop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        root = Path(os.getcwd()) / "tests" / ".tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = root / f"workshop_{uuid.uuid4().hex}"
        self.temp_dir.mkdir()
        self.external_dir = self.temp_dir / "external_chars"
        self.db_path = self.temp_dir / "db.json"
        self.features_dir = self.temp_dir / "features"
        self.patchers = [
            patch("src.char.custom.CustomCharManager.CUSTOM_CHARS_DIR", self.temp_dir),
            patch("src.char.custom.CustomCharManager.DB_PATH", self.db_path),
            patch("src.char.custom.CustomCharManager.FEATURES_DIR", self.features_dir),
            patch("src.char.custom.CustomCharManager.EXTERNAL_CHARS_DIR", self.external_dir),
        ]
        for patcher in self.patchers:
            patcher.start()
        CustomCharManager._instance = None
        CustomCharDb.reset_instance()
        char_registry.rescan_external()
        self.manager = CustomCharManager()
        self.service = WorkshopPackageService(self.manager, char_registry)

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        CustomCharManager._instance = None
        CustomCharDb.reset_instance()
        char_registry.rescan_external()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _package(self, external=True):
        slots = [
            PackageSlot(
                0,
                "builtin",
                {"zh_CN": "零", "en_US": "Zero"},
                impl_id="builtin:zero",
            )
        ]
        if external:
            slots.append(
                PackageSlot(
                    1,
                    "external",
                    {"zh_CN": "示例角色", "en_US": "SampleExternal"},
                    file_name="sample.py",
                    class_name="SampleExternal",
                )
            )
        return TeamPackage("测试方案", "说明", "作者", "1.0.0", tuple(slots))

    def test_export_and_import_only_selected_implementation_sources(self):
        source_dir = self.external_dir / "original"
        source_dir.mkdir(parents=True)
        (source_dir / "sample.py").write_text(SOURCE, encoding="utf-8")
        (source_dir / "unused.py").write_text(
            SOURCE.replace("SampleExternal", "Unused"), encoding="utf-8"
        )
        char_registry.rescan_external()
        preset = self.manager.create_team_preset("本地方案")
        self.assertTrue(
            self.manager.update_team_preset(
                preset["id"],
                slots=[
                    {"char_id": "", "impl_id": "builtin:zero"},
                    {"char_id": "", "impl_id": "external:original/sample"},
                    {"char_id": "", "impl_id": ""},
                    {"char_id": "", "impl_id": ""},
                ],
            )
        )
        archive_path = self.temp_dir / "out.zip"
        self.service.export_preset(preset["id"], self._package(), archive_path)

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(set(archive.namelist()), {"team.json", "sample.py"})
        contents = self.service.load_archive(archive_path)
        imported = self.service.install_contents(contents, "导入方案", "sample_author_1.0.0")
        self.assertEqual(imported.preset_name, "导入方案")
        saved = next(
            item for item in self.manager.get_team_presets() if item["id"] == imported.preset_id
        )
        self.assertEqual(saved["slots"][0], {"char_id": "", "impl_id": "builtin:zero"})
        self.assertEqual(saved["slots"][1]["impl_id"], "external:sample_author_1.0.0/sample")
        self.assertTrue((self.external_dir / "original" / "unused.py").is_file())

    def test_install_rejects_conflict_without_touching_existing_files(self):
        target = self.external_dir / "existing"
        target.mkdir(parents=True)
        marker = target / "keep.py"
        marker.write_text("keep", encoding="utf-8")
        contents = ArchiveContents(self._package(), {"sample.py": SOURCE})

        with self.assertRaises(WorkshopInstallError):
            self.service.install_contents(contents, "新方案", "existing")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse(any(item["name"] == "新方案" for item in self.manager.get_team_presets()))

    def test_invalid_archive_is_rejected_before_install(self):
        invalid = io.BytesIO()
        with zipfile.ZipFile(invalid, "w") as archive:
            archive.writestr("team.json", json.dumps(self._package().to_dict()))
            archive.writestr("folder/sample.py", SOURCE)
        with self.assertRaises(WorkshopFormatError):
            load_archive(invalid.getvalue())

    def test_archive_rejects_directory_entries_and_symbolic_links(self):
        archive_path = self.temp_dir / "invalid.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("folder/", "")
            archive.writestr("team.json", json.dumps(self._package().to_dict()))
            archive.writestr("sample.py", SOURCE)
        with self.assertRaises(WorkshopFormatError):
            load_archive(archive_path)

        link_path = self.temp_dir / "link.zip"
        link = zipfile.ZipInfo("sample.py")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(link_path, "w") as archive:
            archive.writestr("team.json", json.dumps(self._package().to_dict()))
            archive.writestr(link, "sample.py")
        with self.assertRaises(WorkshopFormatError):
            load_archive(link_path)

    def test_archive_syntax_check_does_not_execute_source(self):
        contents = ArchiveContents(
            self._package(), {"sample.py": "raise RuntimeError('must not execute')\n"}
        )
        archive_path = self.temp_dir / "safe_parse.zip"
        from src.char.workshop.archive import write_archive

        write_archive(archive_path, contents)
        self.assertEqual(load_archive(archive_path).sources, contents.sources)

    def test_manifest_rejects_oversized_fields_and_invalid_class_names(self):
        data = self._package().to_dict()
        data["name"] = "x" * 101
        with self.assertRaises(WorkshopFormatError):
            TeamPackage.from_dict(data)

    def test_external_directory_rejects_reserved_windows_names(self):
        with self.assertRaises(ValueError):
            CustomCharManager.validate_external_directory("CON")
        with self.assertRaises(ValueError):
            CustomCharManager.validate_external_directory("team. ")
        self.assertEqual(
            CustomCharManager.validate_external_directory("team_author_1.0.0"),
            "team_author_1.0.0",
        )

        data = self._package().to_dict()
        data["slots"][1]["class_name"] = "not a class"
        with self.assertRaises(WorkshopFormatError):
            TeamPackage.from_dict(data)

    def test_failed_external_scan_rolls_back_new_directory_and_preset(self):
        source = SOURCE.replace("class SampleExternal(BaseChar):", "class NotACharacter:")
        contents = ArchiveContents(self._package(), {"sample.py": source})
        with self.assertRaises(WorkshopInstallError):
            self.service.install_contents(contents, "失败方案", "broken")
        self.assertFalse((self.external_dir / "broken").exists())
        self.assertFalse(
            any(item["name"] == "失败方案" for item in self.manager.get_team_presets())
        )

    def test_repository_uses_chinese_source_then_falls_back(self):
        catalog = {
            "format_version": 1,
            "packages": [
                {
                    **self._package(False).to_dict(),
                    "archive": "codes/test.zip",
                    "filename": "test.zip",
                    "size": 1,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
        }
        cnb_response = Mock()
        cnb_response.raise_for_status.side_effect = requests.RequestException("offline")
        github_response = Mock()
        github_response.raise_for_status.return_value = None
        github_response.headers = {}
        github_response.iter_content.return_value = [
            json.dumps(catalog, ensure_ascii=False).encode("utf-8")
        ]
        session = Mock()
        session.get.side_effect = [cnb_response, github_response]
        repository = WorkshopRepository(
            IndexSource("GitHub", "github-index", "github-base"),
            IndexSource("CNB", "cnb-index", "cnb-base"),
            session,
            self.temp_dir / "catalog.json",
        )
        entries, source = repository.fetch_catalog(True)
        self.assertEqual(source.name, "GitHub")
        self.assertEqual(entries[0].filename, "test.zip")
        self.assertEqual(
            [call.args[0] for call in session.get.call_args_list], ["cnb-index", "github-index"]
        )

    def test_repository_reuses_preferred_source_cache_until_manual_refresh(self):
        catalog = {
            "format_version": 1,
            "packages": [
                {
                    **self._package(False).to_dict(),
                    "archive": "codes/test.zip",
                    "filename": "test.zip",
                    "size": 1,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
        }
        response = Mock()
        response.raise_for_status.return_value = None
        response.headers = {}
        response.iter_content.return_value = [json.dumps(catalog).encode("utf-8")]
        session = Mock()
        session.get.return_value = response
        cache_path = self.temp_dir / "catalog.json"
        repository = WorkshopRepository(
            IndexSource("GitHub", "github-index", "github-base"),
            IndexSource("CNB", "cnb-index", "cnb-base"),
            session,
            cache_path,
        )

        repository.fetch_catalog(True)
        repository.fetch_catalog(True)
        repository.fetch_catalog(True, force_refresh=True)

        self.assertEqual(session.get.call_count, 2)

    def test_repository_ignores_expired_cache(self):
        catalog = {
            "format_version": 1,
            "packages": [
                {
                    **self._package(False).to_dict(),
                    "archive": "codes/test.zip",
                    "filename": "test.zip",
                    "size": 1,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
        }
        response = Mock()
        response.raise_for_status.return_value = None
        response.headers = {}
        response.iter_content.return_value = [json.dumps(catalog).encode("utf-8")]
        session = Mock()
        session.get.return_value = response
        cache_path = self.temp_dir / "catalog.json"
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        sources = (
            IndexSource("GitHub", "github-index", "github-base"),
            IndexSource("CNB", "cnb-index", "cnb-base"),
        )
        WorkshopRepository(*sources, session, cache_path, now=lambda _tz: timestamp).fetch_catalog(
            True
        )

        WorkshopRepository(
            *sources,
            session,
            cache_path,
            now=lambda _tz: timestamp + timedelta(hours=24, seconds=1),
        ).fetch_catalog(True)

        self.assertEqual(session.get.call_count, 2)

    def test_repository_rejects_oversized_archive_before_loading_it(self):
        entry = CatalogEntry(
            self._package(False), "codes/test.zip", "test.zip", 1, "2026-01-01T00:00:00Z"
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(MAX_ARCHIVE_BYTES + 1)}
        session = Mock()
        session.get.return_value = response
        repository = WorkshopRepository(
            IndexSource("GitHub", "github-index", "github-base"),
            IndexSource("CNB", "cnb-index", "cnb-base"),
            session,
            self.temp_dir / "catalog.json",
        )

        with self.assertRaises(WorkshopRepositoryError):
            repository.download_archive(repository.github, entry)
        self.assertEqual(session.get.call_count, 2)

    def test_catalog_filter_searches_names_roles_and_authors(self):
        second = TeamPackage("Second", "strategy", "Other", "2.0.0", self._package(False).slots)
        entries = [
            CatalogEntry(
                self._package(False), "codes/first.zip", "first.zip", 1, "2026-01-01T00:00:00Z"
            ),
            CatalogEntry(second, "codes/second.zip", "second.zip", 1, "2026-02-01T00:00:00Z"),
        ]
        self.assertEqual(
            filter_catalog_entries(entries, keyword="strategy")[0].filename, "second.zip"
        )
        self.assertEqual(filter_catalog_entries(entries, role="Zero"), [entries[1], entries[0]])
        self.assertEqual(filter_catalog_entries(entries, author="作者"), [entries[0]])

    def test_workshop_dialog_uses_readable_catalog_table(self):
        entry = CatalogEntry(
            self._package(False), "codes/test.zip", "test.zip", 3041, "2026-08-18T12:09:37Z"
        )
        repository = Mock()
        parent = QWidget()
        with patch.object(WorkshopDialog, "reload_catalog"):
            dialog = WorkshopDialog(repository, parent)
        dialog._catalog_loaded(([entry], IndexSource("GitHub", "index", "base")))

        self.assertEqual(dialog.table.columnCount(), 4)
        self.assertEqual(dialog.table.item(0, 0).text(), "测试方案")
        self.assertEqual(dialog.table.item(0, 2).text(), "作者")
        self.assertEqual(dialog.table.item(0, 3).text(), "1.0.0")
        self.assertIn("2.97 KB", dialog.detail_meta.text())
        self.assertTrue(dialog.import_button.isEnabled())
        dialog.deleteLater()
        parent.deleteLater()

    def test_version_format_and_numeric_order(self):
        self.assertGreater(parse_version("1.10.0"), parse_version("1.9.9"))
        for version in (
            "1", "1.0", "v1.0.0", "1.0.0-beta", "01.0.0", "1.00.0", "-1.0.0",
            "\uff11.0.0", "1.0.0.0", "1.0.0\n", "1" * 33 + ".0.0",
        ):
            with self.subTest(version=version), self.assertRaises(WorkshopFormatError):
                parse_version(version)
        for version in ("0.0.0", "1.9.0", "1.10.0"):
            data = {**self._package().to_dict(), "version": version}
            self.assertEqual(TeamPackage.from_dict(data).version, version)
        with self.assertRaises(WorkshopFormatError):
            TeamPackage.from_dict({**self._package().to_dict(), "version": "1.0"})

    def test_catalog_groups_by_author_and_name_not_members_or_commit_time(self):
        old = CatalogEntry(
            self._package(False), "codes/old.zip", "old.zip", 1, "2026-09-01T00:00:00Z"
        )
        latest = replace(
            old,
            package=replace(self._package(), version="1.10.0"),
            archive="codes/latest.zip",
            updated_at="2026-08-01T00:00:00Z",
        )
        intermediate = replace(old, package=replace(old.package, version="1.9.0"))
        other_author = replace(old, package=replace(old.package, author="Other"))
        other_name = replace(old, package=replace(old.package, name="Other strategy"))
        groups = group_catalog_entries([old, intermediate, latest, other_author, other_name])
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[old.package.identity], (latest, intermediate, old))

    def test_catalog_rejects_duplicate_named_version_even_with_different_slots(self):
        data = {
            **self._package().to_dict(),
            "archive": "codes/a.zip",
            "filename": "a.zip",
            "size": 1,
            "updated_at": "2026-09-01T00:00:00Z",
        }
        duplicate = {**data, "slots": self._package(False).to_dict()["slots"]}
        with self.assertRaises(WorkshopFormatError):
            parse_catalog({"format_version": 1, "packages": [data, duplicate]})

    def test_workshop_selects_history_for_preview_and_import(self):
        old = CatalogEntry(
            self._package(False), "codes/old.zip", "old.zip", 1, "2026-09-01T00:00:00Z"
        )
        latest = replace(
            old,
            package=replace(self._package(), version="1.10.0", description="New rotation"),
            archive="codes/latest.zip",
            updated_at="2026-08-01T00:00:00Z",
        )
        source = IndexSource("GitHub", "index", "base")
        parent = QWidget()
        with patch.object(WorkshopDialog, "reload_catalog"):
            dialog = WorkshopDialog(Mock(), parent)
        imported = []
        dialog.import_requested.connect(lambda entry, src: imported.append((entry, src)))
        try:
            for chinese, role in ((True, "零"), (False, "Zero")):
                dialog.is_chinese = chinese
                dialog._catalog_loaded(([old, latest], source))
                roles = [dialog.role_combo.itemText(i) for i in range(dialog.role_combo.count())]
                self.assertIn(role, roles)
                self.assertNotIn("Zero" if chinese else "零", roles)
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertEqual(dialog.table.item(0, 3).text(), "1.10.0")
                self.assertEqual(dialog.detail_body.toPlainText(), "New rotation")
                dialog.import_button.click()
                self.assertEqual(imported[-1], (latest, source))
                dialog.version_combo.setCurrentIndex(1)
                self.assertEqual(dialog.detail_body.toPlainText(), old.package.description)
                self.assertEqual(dialog.slot_cards[1].title_label.text(), "-")
                dialog.import_button.click()
                self.assertEqual(imported[-1], (old, source))
                dialog.search_edit.setText("Zero" if chinese else "零")
                self.assertEqual(dialog.table.rowCount(), 1)
                dialog.search_edit.setText(old.package.description)
                self.assertEqual(dialog.table.rowCount(), 0)
                dialog.search_edit.setText("no matches")
                self.assertEqual(dialog.table.rowCount(), 0)
                self.assertFalse(dialog.import_button.isEnabled())
                self.assertEqual(dialog.version_combo.count(), 0)
                dialog.search_edit.clear()
        finally:
            dialog.deleteLater()
            parent.deleteLater()

    def test_workshop_dialog_preserves_description_text(self):
        package = TeamPackage(
            "多行方案",
            '第一行\n\n目录 C:\\new_team\n代码 print("\\n")\n字面量 \\r\\n',
            "作者",
            "1.0.0",
            self._package(False).slots,
        )
        entry = CatalogEntry(package, "codes/test.zip", "test.zip", 100, "2026-01-01T00:00:00Z")
        parent = QWidget()
        with patch.object(WorkshopDialog, "reload_catalog"):
            dialog = WorkshopDialog(Mock(), parent)
        try:
            dialog._catalog_loaded(([entry], IndexSource("GitHub", "index", "base")))
            self.assertEqual(dialog.detail_body.toPlainText(), package.description)
        finally:
            dialog.deleteLater()
            parent.deleteLater()

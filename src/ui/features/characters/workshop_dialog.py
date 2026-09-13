"""Fluent dialogs used by the community team workshop."""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    IndeterminateProgressRing,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TextBrowser,
)

from src.char.custom.CustomCharManager import CustomCharManager
from src.char.workshop.models import (
    CatalogEntry,
    TeamPackage,
    WorkshopFormatError,
    filter_catalog_entries,
    group_catalog_entries,
    parse_version,
)
from src.char.workshop.repository import IndexSource, WorkshopRepository
from src.ui.features.characters.safety_dialog import EXTERNAL_CODE_SAFETY_NOTICE
from src.ui.foundation.dialogs import MessageBoxBase
from src.ui.foundation.i18n import is_chinese
from src.ui.foundation.widgets.cards import BorderCardWidget
from src.ui.foundation.widgets.search import SearchableComboBox


class ElidedCaptionLabel(CaptionLabel):
    """Single-line caption that keeps the original text available in a tooltip."""

    def __init__(self, text: str = "", parent=None):
        self._full_text = ""
        super().__init__(parent)
        self.set_elided_text(text)

    def set_elided_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_text()

    def _update_text(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                max(0, self.contentsRect().width()),
            )
        )

    def resizeEvent(self, event) -> None:
        self._update_text()
        super().resizeEvent(event)


class WorkshopSlotPreviewCard(BorderCardWidget):
    """Compact card to display a slot's character and combo binding in workshop details."""

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.setBorderWidth(1)
        self.index = index
        self.setFixedHeight(50)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self.title_label = StrongBodyLabel("-", self)
        self.combo_label = ElidedCaptionLabel("-", self)

        layout.addWidget(self.title_label)
        layout.addWidget(self.combo_label)

    def set_slot(self, char_name: str, combo_desc: str) -> None:
        self.title_label.setText(char_name if char_name else "-")
        self.title_label.setToolTip(char_name)
        self.combo_label.set_elided_text(combo_desc if combo_desc else "-")

    def clear(self) -> None:
        self.title_label.setText("-")
        self.title_label.setToolTip("")
        self.combo_label.set_elided_text("-")


class BackgroundCall(QObject):
    """Run a blocking callable and return its result through Qt signals."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, action: Callable[[], object], parent=None):
        super().__init__(parent)
        self._action = action

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.succeeded.emit(self._action())
        except Exception as error:
            self.failed.emit(str(error) or error.__class__.__name__)


class PackageMetadataDialog(MessageBoxBase):
    def __init__(self, defaults: TeamPackage, parent=None):
        super().__init__(parent)
        self.viewLayout.setSpacing(6)
        self.viewLayout.addWidget(SubtitleLabel(self.tr("导出队伍"), self))

        self.viewLayout.addWidget(BodyLabel(self.tr("名称"), self))
        self.name_edit = LineEdit(self)
        self.name_edit.setText(defaults.name)
        self.viewLayout.addWidget(self.name_edit)

        self.viewLayout.addWidget(BodyLabel(self.tr("描述"), self))
        self.description_edit = QTextEdit(self)
        self.description_edit.setPlainText(defaults.description)
        self.description_edit.setFixedHeight(160)
        self.viewLayout.addWidget(self.description_edit)

        self.viewLayout.addWidget(BodyLabel(self.tr("作者"), self))
        self.author_edit = LineEdit(self)
        self.author_edit.setText(defaults.author)
        self.viewLayout.addWidget(self.author_edit)

        self.viewLayout.addWidget(BodyLabel(self.tr("版本"), self))
        self.version_edit = LineEdit(self)
        self.version_edit.setPlaceholderText("1.0.0")
        self.version_edit.setToolTip("major.minor.patch (1.0.0)")
        self.version_edit.setText(defaults.version)
        self.viewLayout.addWidget(self.version_edit)

        self.yesButton.setText(self.tr("导出"))
        self.cancelButton.setText(self.tr("取消"))

        self.name_edit.textChanged.connect(self._validate)
        self.author_edit.textChanged.connect(self._validate)
        self.version_edit.textChanged.connect(self._validate)
        self.widget.setMinimumWidth(540)
        self._validate()

    def _validate(self) -> None:
        try:
            parse_version(self.version_edit.text().strip())
            valid_version = True
        except WorkshopFormatError:
            valid_version = False
        self.version_edit.setError(not valid_version)
        self.yesButton.setEnabled(
            bool(
                self.name_edit.text().strip() and self.author_edit.text().strip() and valid_version
            )
        )

    def package(self) -> TeamPackage:
        return TeamPackage(
            self.name_edit.text().strip(),
            self.description_edit.toPlainText().strip(),
            self.author_edit.text().strip(),
            self.version_edit.text().strip(),
            (),
        )


class PackageImportDialog(MessageBoxBase):
    def __init__(self, package: TeamPackage, archive_name: str, parent=None):
        super().__init__(parent)
        self.viewLayout.setSpacing(6)
        self.viewLayout.addWidget(SubtitleLabel(package.name, self))
        summary = f"{self.tr('作者')}: {package.author}\n{self.tr('版本')}: {package.version}"
        self.viewLayout.addWidget(BodyLabel(summary, self))
        members = ", ".join(package.members("zh_CN" if is_chinese() else "en_US"))
        self.viewLayout.addWidget(CaptionLabel(f"{self.tr('成员')}: {members}", self))

        self.viewLayout.addWidget(StrongBodyLabel(self.tr("本地方案名称"), self))
        self.preset_name_edit = LineEdit(self)
        self.preset_name_edit.setText(package.name)
        self.viewLayout.addWidget(self.preset_name_edit)

        self.viewLayout.addWidget(StrongBodyLabel(self.tr("外置代码目录"), self))
        self.directory_edit = LineEdit(self)
        try:
            self.directory_edit.setText(
                CustomCharManager.validate_external_directory(Path(archive_name).stem.strip())
            )
        except ValueError:
            self.directory_edit.setText("community_team")
        self.viewLayout.addWidget(self.directory_edit)

        self.error_label = CaptionLabel("", self)
        self.viewLayout.addWidget(self.error_label)

        self.preset_name_edit.textChanged.connect(self._validate)
        self.directory_edit.textChanged.connect(self._validate)
        self.widget.setMinimumWidth(480)
        self.yesButton.setText(self.tr("导入"))
        self.cancelButton.setText(self.tr("取消"))
        self._validate()

    def _validate(self) -> None:
        try:
            CustomCharManager.validate_external_directory(self.directory_edit.text())
            valid_directory = True
            error = ""
        except ValueError as exception:
            valid_directory = False
            error = str(exception)
        valid = bool(self.preset_name_edit.text().strip()) and valid_directory
        self.yesButton.setEnabled(valid)
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))

    def installation(self) -> tuple[str, str]:
        return (
            self.preset_name_edit.text().strip(),
            CustomCharManager.validate_external_directory(self.directory_edit.text()),
        )


class WorkshopDialog(MessageBoxBase):
    """Browse a remote static catalog without mixing remote I/O into TeamManagerTab."""

    import_requested = Signal(object, object)

    def __init__(self, repository: WorkshopRepository, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.is_chinese = is_chinese()
        self.entries: list[CatalogEntry] = []
        self.versions: dict[tuple[str, str], tuple[CatalogEntry, ...]] = {}
        self.current_source: IndexSource | None = None
        self._worker: BackgroundCall | None = None
        self.viewLayout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_label = SubtitleLabel(self.tr("工坊"), self)
        title_row.addWidget(title_label)

        self.loading_ring = IndeterminateProgressRing(self)
        self.loading_ring.setFixedSize(16, 16)
        self.status_label = CaptionLabel("", self)
        title_row.addWidget(self.loading_ring)
        title_row.addWidget(self.status_label)
        self.safety_label = CaptionLabel(
            self.tr("社区方案含外置代码; 导入后会以本软件权限运行, 请仅导入可信来源."), self
        )
        self.safety_label.setToolTip(EXTERNAL_CODE_SAFETY_NOTICE)
        title_row.addStretch(1)
        self.viewLayout.addLayout(title_row)
        self.viewLayout.addWidget(self.safety_label)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("搜索名称, 说明, 作者或角色"))
        self.role_combo = SearchableComboBox(self)
        self.role_combo.setPlaceholderText(self.tr("全部角色"))
        self.role_combo.setMinimumWidth(150)
        self.author_combo = SearchableComboBox(self)
        self.author_combo.setPlaceholderText(self.tr("全部作者"))
        self.author_combo.setMinimumWidth(150)
        self.refresh_button = PushButton(FluentIcon.SYNC, self.tr("刷新"), self)
        for widget in (self.search_edit, self.role_combo, self.author_combo, self.refresh_button):
            toolbar.addWidget(widget)
        self.viewLayout.addLayout(toolbar)

        # Left-right split layout
        split_layout = QHBoxLayout()
        split_layout.setSpacing(12)

        # Left: compact table with user-resizable columns
        self.table = TableWidget(self)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("名称"),
                self.tr("角色阵容"),
                self.tr("作者"),
                self.tr("版本"),
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setColumnWidth(0, 166)
        self.table.setColumnWidth(1, 270)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 80)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        split_layout.addWidget(self.table, 3)

        # Right: detail panel (fixed width avoids relayout jitter on selection)
        self.detail_card = SimpleCardWidget(self)
        self.detail_card.setFixedWidth(420)
        detail_layout = QVBoxLayout(self.detail_card)
        detail_layout.setContentsMargins(18, 14, 18, 14)
        detail_layout.setSpacing(8)

        self.detail_title = SubtitleLabel(self.tr("选择一个方案"), self.detail_card)
        self.detail_meta = CaptionLabel("", self.detail_card)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_meta)

        detail_layout.addWidget(StrongBodyLabel(self.tr("队伍配置"), self.detail_card))
        slots_grid = QGridLayout()
        slots_grid.setSpacing(8)
        self.slot_cards: list[WorkshopSlotPreviewCard] = []
        for index in range(4):
            card = WorkshopSlotPreviewCard(index, self.detail_card)
            self.slot_cards.append(card)
            slots_grid.addWidget(card, index // 2, index % 2)
        detail_layout.addLayout(slots_grid)

        detail_layout.addWidget(StrongBodyLabel(self.tr("方案说明"), self.detail_card))
        self.detail_body = TextBrowser(self.detail_card)
        self.detail_body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.detail_body.setPlainText(self.tr("工坊会显示来自公开仓库的 ZIP 方案."))
        detail_layout.addWidget(self.detail_body, 1)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        self.version_combo = ComboBox(self.detail_card)
        self.version_combo.setEnabled(False)
        self.version_combo.setMinimumWidth(95)
        self.version_combo.setToolTip(self.tr("版本"))
        action_layout.addWidget(self.version_combo, alignment=Qt.AlignmentFlag.AlignVCenter)
        action_layout.addStretch(1)

        self.import_button = PrimaryPushButton(
            FluentIcon.DOWNLOAD, self.tr("导入"), self.detail_card
        )
        self.import_button.setEnabled(False)
        action_layout.addWidget(self.import_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        detail_layout.addLayout(action_layout)
        split_layout.addWidget(self.detail_card)

        self.viewLayout.addLayout(split_layout, 1)

        self.widget.setMinimumSize(1100, 600)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))

        self.search_edit.textChanged.connect(self._apply_filter)
        self.role_combo.currentTextChanged.connect(self._apply_filter)
        self.author_combo.currentTextChanged.connect(self._apply_filter)
        self.table.itemSelectionChanged.connect(self._show_selection)
        self.version_combo.currentIndexChanged.connect(self._show_version)
        self.refresh_button.clicked.connect(lambda: self.reload_catalog(force_refresh=True))
        self.import_button.clicked.connect(self._request_import)
        self.reload_catalog()

    def reload_catalog(self, force_refresh: bool = False) -> None:
        self.refresh_button.setEnabled(False)
        self.loading_ring.show()
        self.status_label.setText(self.tr("正在加载工坊..."))
        self._worker = BackgroundCall(
            lambda: self.repository.fetch_catalog(self.is_chinese, force_refresh), self
        )
        self._worker.succeeded.connect(self._catalog_loaded)
        self._worker.failed.connect(self._catalog_failed)
        self._worker.start()

    def _catalog_loaded(self, result: object) -> None:
        entries, self.current_source = cast(tuple[list[CatalogEntry], IndexSource], result)
        self.versions = group_catalog_entries(entries)
        self.entries = [versions[0] for versions in self.versions.values()]
        self._rebuild_filters()
        self._apply_filter()
        self.refresh_button.setEnabled(True)
        self.loading_ring.hide()
        self.status_label.setText(self.tr("已加载 {} 个方案").format(len(self.entries)))

    def _catalog_failed(self, error: str) -> None:
        self.refresh_button.setEnabled(True)
        self.loading_ring.hide()
        self.status_label.setText(self.tr("加载失败: {}").format(error))

    def _rebuild_filters(self) -> None:
        current_role = self.role_combo.currentText()
        current_author = self.author_combo.currentText()
        self.role_combo.blockSignals(True)
        self.author_combo.blockSignals(True)
        self.role_combo.clear()
        self.author_combo.clear()
        self.role_combo.addItem("")
        self.author_combo.addItem("")
        roles = sorted(
            {
                slot.display_name("zh_CN" if self.is_chinese else "en_US")
                for entry in self.entries
                for slot in entry.package.slots
            }
        )
        authors = sorted({entry.package.author for entry in self.entries})
        for role in roles:
            self.role_combo.addItem(role)
        for author in authors:
            self.author_combo.addItem(author)
        self.role_combo.setCurrentText(current_role)
        self.author_combo.setCurrentText(current_author)
        self.role_combo.blockSignals(False)
        self.author_combo.blockSignals(False)

    def _apply_filter(self, *_args) -> None:
        visible = filter_catalog_entries(
            self.entries,
            self.search_edit.text(),
            self.role_combo.currentText(),
            self.author_combo.currentText(),
        )
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row, entry in enumerate(visible):
            package = entry.package
            self.table.insertRow(row)
            members = ", ".join(
                slot.display["zh_CN"] if self.is_chinese else slot.display["en_US"]
                for slot in package.slots
            )
            values = [
                package.name,
                members,
                package.author,
                package.version,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        if self.table.rowCount():
            self.table.selectRow(0)
        self.table.blockSignals(False)
        self._show_selection()

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.2f} KB"
        return f"{size / (1024 * 1024):.2f} MB"

    def _show_selection(self) -> None:
        item = self.table.item(self.table.currentRow(), 0)
        entry = cast(CatalogEntry, item.data(Qt.ItemDataRole.UserRole)) if item else None
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        if entry is not None:
            for version in self.versions[entry.package.identity]:
                self.version_combo.addItem(version.package.version, userData=version)
        self.version_combo.setEnabled(self.version_combo.count() > 1)
        self.version_combo.blockSignals(False)
        self._show_version()

    def _show_version(self, *_args) -> None:
        entry = cast(CatalogEntry | None, self.version_combo.currentData())
        self.import_button.setEnabled(entry is not None and self.current_source is not None)
        if entry is None:
            self.detail_title.setText(self.tr("选择一个方案"))
            self.detail_meta.setText("")
            self.detail_body.setPlainText(self.tr("调整筛选条件或稍后刷新工坊."))
            for card in self.slot_cards:
                card.clear()
            return
        package = entry.package
        self.detail_title.setText(package.name)
        meta_line1 = (
            f"{self.tr('作者')}: {package.author}  ·  "
            f"{self.tr('版本')}: {package.version}  ·  "
            f"{self.tr('大小')}: {self._format_size(entry.size)}"
        )
        meta_line2 = (
            f"{self.tr('更新时间')}: {entry.updated_at.replace('T', ' ').removesuffix('Z')}"
        )
        self.detail_meta.setText(f"{meta_line1}\n{meta_line2}")
        self.detail_body.setPlainText(package.description or self.tr("没有说明."))
        slot_map = {slot.index: slot for slot in package.slots}
        for index in range(4):
            slot = slot_map.get(index)
            if slot:
                char_name = slot.display_name("zh_CN" if self.is_chinese else "en_US")
                kind_text = self.tr("内置") if slot.kind == "builtin" else self.tr("外置")
                combo_info = slot.impl_id if slot.kind == "builtin" else slot.file_name
                combo_desc = f"[{kind_text}] {combo_info}" if combo_info else f"[{kind_text}]"
                self.slot_cards[index].set_slot(char_name, combo_desc)
            else:
                self.slot_cards[index].clear()

    def _request_import(self) -> None:
        entry = self.version_combo.currentData()
        if entry is not None and self.current_source is not None:
            self.import_requested.emit(entry, self.current_source)

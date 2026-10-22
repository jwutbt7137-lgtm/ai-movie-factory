"""手动替换某一句的候选镜头。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import Shot, format_clock


class ShotEditorDialog(QDialog):
    """按序号选择镜头；顺序固定为镜头序号，保持时间连续性。"""

    def __init__(
        self,
        shots: list[Shot],
        selected_ids: list[int],
        *,
        target_duration_s: float,
        sentence_text: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("手动选择镜头")
        self.setMinimumSize(760, 620)
        self._shots = sorted(shots, key=lambda shot: shot.index)
        self._target_duration = max(0.1, float(target_duration_s))
        selected = set(selected_ids)
        layout = QVBoxLayout(self)
        title = QLabel(f"目标时长：{target_duration_s:.2f} 秒")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)
        text = QLabel(sentence_text)
        text.setObjectName("HintLabel")
        text.setWordWrap(True)
        layout.addWidget(text)

        toolbar = QHBoxLayout()
        self.summary = QLabel("")
        toolbar.addWidget(self.summary, 1)
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self._set_all(True))
        toolbar.addWidget(select_all)
        clear_all = QPushButton("清空")
        clear_all.clicked.connect(lambda: self._set_all(False))
        toolbar.addWidget(clear_all)
        reset = QPushButton("恢复当前")
        reset.clicked.connect(lambda: self._restore(selected))
        toolbar.addWidget(reset)
        layout.addLayout(toolbar)

        self.table = QTableWidget(len(self._shots), 5)
        self.table.setHorizontalHeaderLabels(["选", "镜头", "开始", "结束", "时长"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._refresh_summary)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("使用这些镜头")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for row, shot in enumerate(self._shots):
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check.setCheckState(
                Qt.CheckState.Checked if shot.index in selected else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, check)
            for column, value in (
                (1, str(shot.index)),
                (2, format_clock(shot.start_s)),
                (3, format_clock(shot.end_s)),
                (4, f"{shot.duration_s:.2f}s"),
            ):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self._refresh_summary()

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        self.table.blockSignals(False)
        self._refresh_summary()

    def _restore(self, selected: set[int]) -> None:
        self.table.blockSignals(True)
        for row, shot in enumerate(self._shots):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Checked
                    if shot.index in selected
                    else Qt.CheckState.Unchecked
                )
        self.table.blockSignals(False)
        self._refresh_summary()

    def _refresh_summary(self, *_args) -> None:
        selected = self.selected_shots()
        total = sum(shot.duration_s for shot in selected)
        color = "#6fcf97" if total >= self._target_duration else "#e0b25c"
        self.summary.setText(
            f"已选 {len(selected)} 个镜头，总时长 {total:.2f} 秒 / "
            f"目标 {self._target_duration:.2f} 秒"
        )
        self.summary.setStyleSheet(f"color: {color};")

    def selected_shots(self) -> list[Shot]:
        result: list[Shot] = []
        for row, shot in enumerate(self._shots):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                result.append(shot)
        return result

    def selected_ids(self) -> list[int]:
        return [shot.index for shot in self.selected_shots()]

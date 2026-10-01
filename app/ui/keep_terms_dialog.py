from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.engine.translate import KeepCandidate, _base_term


class KeepTermsDialog(QDialog):
    """Checklist of words that stay in English for this translation."""

    def __init__(self, candidates: list[KeepCandidate], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keep in English")
        self.setMinimumWidth(480)
        self._rows: list[tuple[QCheckBox, str]] = []

        hint = QLabel(
            "Checked words stay in English, including a simple plural such as branches. "
            "Uncheck any word that should be translated."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        holder = QWidget()
        holder.setLayout(self._list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setMinimumHeight(180)

        for candidate in candidates:
            self._add_row(candidate.term, candidate.quote, checked=True)

        self._empty = QLabel("No words were suggested. Add any that should stay in English.")
        self._empty.setObjectName("hintLabel")
        self._empty.setWordWrap(True)
        self._list.addWidget(self._empty)
        self._list.addStretch(1)
        self._refresh_empty()

        self._add_edit = QLineEdit()
        self._add_edit.setPlaceholderText("Add a word or phrase")
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_from_field)
        self._add_edit.returnPressed.connect(self._add_from_field)
        add_row = QHBoxLayout()
        add_row.addWidget(self._add_edit, 1)
        add_row.addWidget(add_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Translate")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(scroll, 1)
        layout.addLayout(add_row)
        layout.addWidget(buttons)

    def selected_terms(self) -> list[str]:
        chosen: list[str] = []
        for box, term in self._rows:
            if box.isChecked():
                chosen.append(term)
        return chosen

    def _add_from_field(self) -> None:
        term = _base_term(" ".join(self._add_edit.text().split()))
        if not term:
            return
        existing = self._row_for(term)
        if existing is not None:
            existing.setChecked(True)
        else:
            self._add_row(term, "Added by you", checked=True)
            self._refresh_empty()
        self._add_edit.clear()

    def _add_row(self, term: str, quote: str, checked: bool) -> None:
        box = QCheckBox(term)
        box.setChecked(checked)
        note = QLabel(quote)
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        note.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(box, 0, Qt.AlignmentFlag.AlignTop)
        row_layout.addWidget(note, 1)
        self._list.insertWidget(len(self._rows), row)
        self._rows.append((box, term))

    def _row_for(self, term: str) -> QCheckBox | None:
        key = _base_term(term).casefold()
        for box, existing in self._rows:
            if _base_term(existing).casefold() == key:
                return box
        return None

    def _refresh_empty(self) -> None:
        self._empty.setVisible(not self._rows)

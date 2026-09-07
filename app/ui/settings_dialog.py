from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from app.engine.settings import load_settings, save_settings


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        data = load_settings()

        self.provider = QComboBox()
        self.provider.addItem("edge-tts (free, no key)", "edge-tts")
        self.provider.addItem("OpenAI", "openai")
        self.provider.addItem("ElevenLabs", "elevenlabs")
        index = self.provider.findData(data.get("tts_provider", "edge-tts"))
        if index >= 0:
            self.provider.setCurrentIndex(index)

        self.openai_key = QLineEdit(data.get("openai_api_key", ""))
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setPlaceholderText("sk-...")

        self.eleven_key = QLineEdit(data.get("elevenlabs_api_key", ""))
        self.eleven_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.eleven_key.setPlaceholderText("ElevenLabs API key")

        form = QFormLayout()
        form.addRow("TTS provider", self.provider)
        form.addRow("OpenAI API key", self.openai_key)
        form.addRow("ElevenLabs API key", self.eleven_key)

        hint = QLabel(
            "Keys stay on this computer only. The app works without keys "
            "using edge-tts. OpenAI is also used to auto-translate the script."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def accept(self) -> None:
        current = load_settings()
        current.update(
            {
                "tts_provider": self.provider.currentData(),
                "openai_api_key": self.openai_key.text().strip(),
                "elevenlabs_api_key": self.eleven_key.text().strip(),
            }
        )
        save_settings(current)
        super().accept()

# SPDX-License-Identifier: GPL-3.0-or-later
"""
Custom widget subclasses that ignore scroll wheel events.
Prevents accidental value changes when scrolling the window.
"""
from PyQt6.QtWidgets import QSpinBox, QComboBox, QDoubleSpinBox
from PyQt6.QtCore import Qt


class NoScrollSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoScrollComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()

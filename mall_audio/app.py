from __future__ import annotations

import csv
import math
import subprocess
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import qtawesome as qta

from PySide6.QtCore import QEasingCurve, QEvent, QMimeData, QPoint, QPointF, QRect, QRectF, QStandardPaths, Qt, QTime, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QActionGroup, QBrush, QColor, QDrag, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtMultimedia import QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QLineEdit, QMenu, QPushButton, QSizePolicy, QSlider, QToolTip, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyleOptionComboBox, QStyleOptionSlider, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTimeEdit, QHeaderView,
    QVBoxLayout, QWidget,
)

from .audio import AudioController
from .autostart import is_enabled as autostart_enabled, set_enabled as set_autostart_enabled
from .database import Database
from .i18n import LANGUAGES, current_language, set_language, tr
from .scheduler import Scheduler
from .timeline import audio_duration_seconds, upcoming_occurrences, voice_ad_start_times


APP_NAME = "Mall Audio Scheduler"
# Sampled from the logo artwork. The band green is #182f2b, which at text size
# reads as black rather than green, so the wordmark keeps that exact hue and
# saturation and lifts only the value: still unmistakably the logo's colour, and
# 8:1 against the sidebar. The gold is likewise the artwork's #d8b460 deepened,
# which on its own scores 1.84:1 here and cannot be read.
BRAND_FONT_PX = 29
BRAND_GREEN = "#2a534c"
BRAND_GOLD = "#886c2c"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac"}
PLAYING_ROLE = Qt.ItemDataRole.UserRole.value + 1
METER_ROLE = Qt.ItemDataRole.UserRole.value + 2
# Two player cards side by side, each over its own list.
PLAYER_CARD_HEIGHT = 244
PLAYER_VISUALIZER_HEIGHT = 96
METER_IDLE = (0.16, 0.16, 0.16, 0.16)
METER_INTERVAL_MS = 110
# How close the player must report back before a seek is considered landed,
# and how long to wait before giving up and trusting the player again.
SEEK_TOLERANCE_MS = 1_200
SEEK_SETTLE_SECONDS = 2.0


def _meter_frames(count: int = 24) -> tuple[tuple[float, ...], ...]:
    """Pre-compute the little bar meter shown beside the playing track.

    It is decoration rather than a reading of the signal, so the motion is a
    fixed loop: integer frequencies make it seamless, and the per-bar phases
    stop the four bars from bouncing in lockstep.
    """
    shape = ((3, 1, 0.00), (2, 4, 0.31), (4, 2, 0.62), (2, 3, 0.85))
    frames = []
    for step in range(count):
        position = step / count
        bars = []
        for frequency, harmonic, phase in shape:
            level = (
                0.62 * math.sin(2 * math.pi * (frequency * position + phase))
                + 0.38 * math.sin(2 * math.pi * (harmonic * position + phase * 1.7))
            )
            bars.append(0.16 + 0.84 * (level + 1) / 2)
        frames.append(tuple(bars))
    return tuple(frames)


METER_FRAMES = _meter_frames()


def asset_path(name: str) -> Path:
    """Locate a bundled asset in both a source checkout and a frozen build.

    PyInstaller unpacks data files under _MEIPASS rather than beside the module.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    packaged = base / "assets" / name
    return packaged if packaged.exists() else Path(__file__).resolve().parent / "assets" / name


def application_data_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / "mall_audio.sqlite3"


def make_text_button(icon_name: str, label: str, object_name: str) -> QPushButton:
    """Labelled action button - an icon alone does not say what it does.

    Module-level rather than a MainWindow method, so dialogs that are not a
    MainWindow (AudioGainDialog, for one) can build the same style of button.
    """
    button = QPushButton(f"  {label}")
    button.setObjectName(object_name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setIcon(qta.icon(icon_name, color="#ffffff" if object_name == "primaryAction" else "#4e545c"))
    button.setAccessibleName(label)
    return button


class DropAudioTable(QTableWidget):
    """Playlist table that accepts dropped files and hand-dragged reordering.

    Qt's own InternalMove drops a row with no motion at all, so the reorder is
    drawn here instead: the row lifts under the cursor, an insertion line glides
    to the gap it would land in, and the landed row fades out of an accent tint.
    """

    files_dropped = Signal(list)
    rows_reordered = Signal(int, int)

    INDICATOR_MS = 120
    SETTLE_MS = 520

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.empty_message = tr("Drop audio files here")
        self.no_match_message = tr("Nothing matches that search")
        self.filtered = False
        self.hovered_row = -1
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        # The insertion line is drawn here so it can be animated.
        self.setDropIndicatorShown(False)
        self.drag_row = -1
        self.drop_index = -1
        self._indicator_y = 0.0
        self._indicator_visible = False
        self._indicator_anim = QVariantAnimation(self)
        self._indicator_anim.setDuration(self.INDICATOR_MS)
        self._indicator_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._indicator_anim.valueChanged.connect(self._on_indicator_value)
        self._settle_row = -1
        self._settle_strength = 0.0
        self._settle_anim = QVariantAnimation(self)
        self._settle_anim.setDuration(self.SETTLE_MS)
        self._settle_anim.setStartValue(1.0)
        self._settle_anim.setEndValue(0.0)
        self._settle_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._settle_anim.valueChanged.connect(self._on_settle_value)
        self._settle_anim.finished.connect(self._on_settle_finished)

    # ----- reorder drag ----------------------------------------------------
    def is_reordering(self) -> bool:
        """True while a row is in flight, so refreshes leave the view alone."""
        return self.drag_row >= 0

    def startDrag(self, actions) -> None:
        row = self.currentRow()
        if row < 0:
            return
        self.drag_row = row
        drag = QDrag(self)
        data = QMimeData()
        data.setData("application/x-mall-audio-row", str(row).encode())
        drag.setMimeData(data)
        drag.setPixmap(self._row_pixmap(row))
        drag.setHotSpot(QPoint(24, self.rowHeight(row) // 2))
        drag.exec(Qt.DropAction.MoveAction)
        self._end_reorder()

    def _row_pixmap(self, row: int) -> QPixmap:
        """A translucent copy of the row, so the drag looks like a lifted card."""
        width, height = self.viewport().width(), self.rowHeight(row)
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(int(width * ratio), int(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(0.92)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRect(0, 0, width - 1, height - 1)
        painter.setPen(QPen(QColor("#c3d0ef"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, 0, width - 1, height - 1)
        painter.setPen(QColor("#1f2226"))
        for column in range(self.columnCount()):
            cell = self.item(row, column)
            if cell is None:
                continue
            rect = QRect(self.columnViewportPosition(column) + 9, 0, self.columnWidth(column) - 18, height)
            alignment = Qt.AlignmentFlag.AlignVCenter | (
                Qt.AlignmentFlag.AlignRight if column else Qt.AlignmentFlag.AlignLeft
            )
            painter.drawText(rect, alignment, cell.text())
        painter.end()
        return pixmap

    def _insertion_index(self, y: int) -> int:
        """Which gap the cursor sits in, counting the gap past the last row."""
        for row in range(self.rowCount()):
            top = self.rowViewportPosition(row)
            if y < top + self.rowHeight(row) / 2:
                return row
        return self.rowCount()

    def _gap_y(self, index: int) -> float:
        if index >= self.rowCount():
            last = self.rowCount() - 1
            return self.rowViewportPosition(last) + self.rowHeight(last) if last >= 0 else 0.0
        return float(self.rowViewportPosition(index))

    def _move_indicator(self, index: int) -> None:
        target = self._gap_y(index)
        if not self._indicator_visible:
            self._indicator_visible = True
            self._indicator_y = target
        elif abs(target - self._indicator_y) > 0.5:
            self._indicator_anim.stop()
            self._indicator_anim.setStartValue(float(self._indicator_y))
            self._indicator_anim.setEndValue(target)
            self._indicator_anim.start()
        self.drop_index = index
        self.viewport().update()

    def _on_indicator_value(self, value) -> None:
        self._indicator_y = float(value)
        self.viewport().update()

    def _on_settle_value(self, value) -> None:
        self._settle_strength = float(value)
        self.viewport().update()

    def _on_settle_finished(self) -> None:
        self._settle_row = -1
        self.viewport().update()

    def settle_row(self, row: int) -> None:
        """Fade an accent tint off the row that just landed."""
        if row < 0:
            return
        self._settle_row = row
        self._settle_anim.stop()
        self._settle_anim.start()

    def _end_reorder(self) -> None:
        self.drag_row = -1
        self.drop_index = -1
        self._indicator_visible = False
        self._indicator_anim.stop()
        self.viewport().update()

    # ----- drops -----------------------------------------------------------
    @staticmethod
    def _is_internal(event) -> bool:
        return event.mimeData().hasFormat("application/x-mall-audio-row")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._is_internal(event) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._is_internal(event):
            self._move_indicator(self._insertion_index(int(event.position().y())))
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._indicator_visible = False
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if self._is_internal(event):
            source = int(bytes(event.mimeData().data("application/x-mall-audio-row")).decode())
            target = self._insertion_index(int(event.position().y()))
            self._end_reorder()
            if target not in (source, source + 1):
                event.acceptProposedAction()
                self.rows_reordered.emit(source, target)
            else:
                event.ignore()
            return
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        valid = [file for file in files if Path(file).suffix.lower() in AUDIO_EXTENSIONS]
        if valid:
            self.files_dropped.emit(valid)
            event.acceptProposedAction()
        else:
            event.ignore()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        visible = sum(not self.isRowHidden(row) for row in range(self.rowCount()))
        if visible == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor("#7f8aa0"))
            message = self.no_match_message if self.filtered else self.empty_message
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, message)
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._settle_row >= 0 and self._settle_strength > 0:
            tint = QColor("#315beb")
            tint.setAlphaF(0.16 * self._settle_strength)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRect(
                QRect(1, self.rowViewportPosition(self._settle_row), self.viewport().width() - 2,
                      self.rowHeight(self._settle_row)))
        if self._indicator_visible:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#315beb"))
            painter.drawRect(QRect(2, int(self._indicator_y) - 1, self.viewport().width() - 4, 3))

    def mouseMoveEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        row = index.row() if index.isValid() else -1
        if row != self.hovered_row:
            self.hovered_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self.hovered_row != -1:
            self.hovered_row = -1
            self.viewport().update()
        super().leaveEvent(event)


class PlaylistRowDelegate(QStyledItemDelegate):
    """Keeps the live track visible without confusing it with table selection."""

    def paint(self, painter, option, index) -> None:
        is_playing = bool(index.data(PLAYING_ROLE))
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hovered = index.row() == getattr(self.parent(), "hovered_row", -1)
        if is_selected:
            background = QColor("#e4ebff")
        elif is_playing:
            background = QColor("#f0f4ff")
        elif is_hovered:
            background = QColor("#f6f8fb")
        else:
            background = None
        if background:
            option.backgroundBrush = QBrush(background)
            option.palette.setColor(QPalette.ColorRole.Base, background)
            option.palette.setColor(QPalette.ColorRole.AlternateBase, background)
            if is_selected:
                option.palette.setColor(QPalette.ColorRole.Highlight, background)
        if is_playing:
            option.palette.setColor(QPalette.ColorRole.Text, QColor("#315beb"))
            option.palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#1f3fa8"))
        levels = index.data(METER_ROLE)
        if levels:
            # Drawn, not typed: no font fallback, no elision, no width guessing.
            self._paint_meter(painter, option, levels)
        else:
            super().paint(painter, option, index)
        if is_playing and index.column() == 0:
            painter.fillRect(option.rect.x(), option.rect.y(), 3, option.rect.height(), QColor("#315beb"))

    @staticmethod
    def _paint_meter(painter, option, levels) -> None:
        painter.save()
        painter.fillRect(option.rect, option.backgroundBrush)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#315beb"))
        bar, gap = 3, 2
        span = len(levels) * bar + (len(levels) - 1) * gap
        left = option.rect.x() + (option.rect.width() - span) / 2
        usable = option.rect.height() - 8
        floor = option.rect.y() + option.rect.height() - 4
        for position, level in enumerate(levels):
            height = max(2.0, usable * float(level))
            painter.drawRect(QRectF(left + position * (bar + gap), floor - height, bar, height))
        painter.restore()


class SeekSlider(QSlider):
    """Playback timeline that seeks where the user actually points.

    A plain QSlider pages by one step when the groove is clicked and streams a
    value on every mouse move.  For a media timeline that means clicking the
    middle of the bar barely moves, and dragging fires a seek per mouse event,
    which makes the decoder stutter and fights the position updates coming back
    from the player.  This reports a drag as one committed seek plus cheap
    preview values, and treats a click as "seek here".
    """

    scrub_preview = Signal(int)
    seek_requested = Signal(int)

    def _value_at(self, x: float) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        offset = x - groove.x() - handle.width() / 2
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(offset), max(1, span))

    def _track(self, x: float) -> None:
        value = self._value_at(x)
        self.setValue(value)
        self.scrub_preview.emit(value)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            self.setSliderDown(True)
            self._track(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown():
            self._track(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.isSliderDown() and event.button() == Qt.MouseButton.LeftButton:
            value = self._value_at(event.position().x())
            self.setValue(value)
            self.setSliderDown(False)
            self.seek_requested.emit(value)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _nudge(self, delta_ms: int) -> None:
        if self.maximum() <= self.minimum():
            return
        value = max(self.minimum(), min(self.maximum(), self.value() + delta_ms))
        self.setValue(value)
        self.seek_requested.emit(value)

    def keyPressEvent(self, event) -> None:
        steps = {
            Qt.Key.Key_Left: -5_000, Qt.Key.Key_Right: 5_000,
            Qt.Key.Key_PageUp: 30_000, Qt.Key.Key_PageDown: -30_000,
        }
        if event.key() in steps:
            self._nudge(steps[event.key()])
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Home, Qt.Key.Key_End):
            target = self.minimum() if event.key() == Qt.Key.Key_Home else self.maximum()
            self.setValue(target)
            self.seek_requested.emit(target)
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if self.maximum() <= self.minimum():
            return
        self._nudge(5_000 if event.angleDelta().y() > 0 else -5_000)
        event.accept()


# Ten looks for the spectrum; the order here is the order in the menu.
VISUALIZER_STYLES = (
    # Grouped by family, which is also how they read in the menu.
    "Bars", "Pillars", "Blocks", "Equalizer", "Needles", "Peaks",
    "Mirror", "Comb", "Spine", "Wings",
    "Wave", "Steps", "Ribbon", "Aurora", "Horizon", "Lattice",
    "Dots", "Droplets", "Grid", "Scatter",
    "Radial", "Ring", "Tunnel", "Spiral", "Arcs", "Beams",
    "Pulse", "Sonar", "Trail", "Embers",
)

# name -> (background, (low, mid, high) stops across the spectrum)
VISUALIZER_THEMES = {
    "Daylight": ("#f7f7f5", ("#38bdf8", "#315beb", "#8b5cf6")),
    "Midnight": ("#101a2f", ("#38bdf8", "#6366f1", "#c084fc")),
    "Ember":    ("#1b1210", ("#fbbf24", "#f97316", "#ef4444")),
    "Neon":     ("#0b0b12", ("#22d3ee", "#a855f7", "#ec4899")),
    "Forest":   ("#0f1b16", ("#a3e635", "#22c55e", "#14b8a6")),
    "Sunset":   ("#1e1220", ("#fb923c", "#f43f5e", "#a855f7")),
    "Mono":     ("#f4f4f5", ("#a1a1aa", "#71717a", "#3f3f46")),
    "Ocean":    ("#08202c", ("#2dd4bf", "#0ea5e9", "#4f46e5")),
    "Candy":    ("#fdf2f8", ("#f9a8d4", "#e879f9", "#a78bfa")),
    "Aurora":   ("#071a1a", ("#4ade80", "#22d3ee", "#818cf8")),
}


class SpectrumVisualizer(QWidget):
    """Real-time spectrum display fed by QMediaPlayer PCM buffers.

    The audio thread delivers bands in bursts that are uneven in both rate and
    level, which on its own looks like flicker rather than movement.  So the
    incoming bands are only a *target*: each column is a damped spring chasing
    it on a 60fps clock, which keeps the motion continuous between buffers and
    gives the slight overshoot that reads as dancing.  A slow automatic gain
    keeps quiet passages using the full height instead of hugging the floor.
    """

    POINTS = 64
    FRAME_MS = 16
    # Asymmetric, the way a level meter behaves: a transient is followed almost
    # immediately so beats land on time, while the fall is sprung so the display
    # still moves rather than snapping. A symmetric spring lagged every onset.
    ATTACK = 0.45
    STIFFNESS = 0.30
    DAMPING = 0.62
    PEAK_GRAVITY = 0.0016
    # Per-band envelope tracking. Both snap instantly to a new extreme and creep
    # back slowly, so each band keeps a window sized to its own recent history.
    FLOOR_RISE = 0.0015
    CEILING_FALL = 0.0022
    # Never expand a band flatter than this, or its noise fills the display.
    MIN_SPAN = 0.045
    # Bands whose loudest recent moment is still this quiet are genuinely empty
    # (an encoder's low-pass, say) and stay down instead of showing amplified hiss.
    GATE_FLOOR = 0.15
    GATE_WIDTH = 0.16
    # Frames kept for the waterfall, and how many specks Embers carries.
    # The history is stored at half resolution: 26 full-width smoothed paths
    # cost ~18 ms a frame, over the 16 ms budget, and the older lines carry no
    # detail worth that. Halving both brings it to about a third of the cost.
    TRAIL_FRAMES = 18
    TRAIL_STRIDE = 2
    EMBERS = 54

    def __init__(self, style: str = "Bars", theme: str = "Daylight") -> None:
        super().__init__()
        self.setMinimumHeight(170)
        self.setObjectName("visualizer")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.active = False
        self.style_name = style if style in VISUALIZER_STYLES else "Bars"
        self.theme_name = theme if theme in VISUALIZER_THEMES else "Daylight"
        self.levels = np.zeros(self.POINTS, dtype=np.float32)
        self.velocity = np.zeros(self.POINTS, dtype=np.float32)
        self.targets = np.zeros(self.POINTS, dtype=np.float32)
        self.peaks = np.zeros(self.POINTS, dtype=np.float32)
        self.peak_velocity = np.zeros(self.POINTS, dtype=np.float32)
        self._source_x = np.linspace(0.0, 1.0, 48, dtype=np.float32)
        self._target_x = np.linspace(0.0, 1.0, self.POINTS, dtype=np.float32)
        self._band_floor = np.ones(self.POINTS, dtype=np.float32)
        self._band_ceiling = np.zeros(self.POINTS, dtype=np.float32)
        self._phase = 0.0
        # State for the styles that remember something between frames.
        rng = np.random.default_rng(11)
        self._history: list[np.ndarray] = []
        self._ember_x = rng.random(self.EMBERS).astype(np.float32)
        self._ember_y = rng.random(self.EMBERS).astype(np.float32)
        self._ember_rise = (0.003 + rng.random(self.EMBERS) * 0.009).astype(np.float32)
        self._ember_size = (0.9 + rng.random(self.EMBERS) * 1.9).astype(np.float32)
        self._sonar: list[float] = []
        self._bass_previous = 0.0
        # A fixed dust field, so Scatter does not boil from frame to frame.
        self._scatter = [
            (float(x), float(y), float(0.7 + s * 1.9))
            for x, y, s in zip(rng.random(90), rng.random(90), rng.random(90))
        ]
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    # ----- configuration ---------------------------------------------------
    def set_style(self, name: str) -> None:
        if name in VISUALIZER_STYLES:
            self.style_name = name
            self.update()

    def set_theme(self, name: str) -> None:
        if name in VISUALIZER_THEMES:
            self.theme_name = name
            self.update()

    def background_color(self) -> QColor:
        return QColor(VISUALIZER_THEMES[self.theme_name][0])

    def set_active(self, active: bool) -> None:
        self.active = active
        if not active:
            # Fall to rest rather than snapping to a flat line, and forget the
            # old track's envelopes so the next one is measured from scratch.
            self.targets[:] = 0.0
            self._band_floor[:] = 1.0
            self._band_ceiling[:] = 0.0
            self._history.clear()
            self._sonar.clear()
        elif not self._timer.isActive():
            self._timer.start()

    # ----- data ------------------------------------------------------------
    def set_spectrum(self, values: np.ndarray) -> None:
        if not self.active:
            return
        bands = np.asarray(values, dtype=np.float32)
        if bands.size != self._source_x.size:
            self._source_x = np.linspace(0.0, 1.0, bands.size, dtype=np.float32)
        # Interpolating to more points than the FFT gives keeps the curve styles
        # smooth without pretending to a resolution the analysis does not have.
        resampled = np.interp(self._target_x, self._source_x, bands).astype(np.float32)
        # Per-band, not global. Bass runs far louder than treble, so one shared
        # gain either pins the mids at full height or flattens everything above
        # them - which is exactly what a single window did. Giving each band its
        # own slow floor and ceiling lets every column use the full height
        # against its own recent range, so neighbours move independently.
        self._band_floor = np.minimum(resampled, self._band_floor + self.FLOOR_RISE)
        self._band_ceiling = np.maximum(resampled, self._band_ceiling - self.CEILING_FALL)
        span = np.maximum(self._band_ceiling - self._band_floor, self.MIN_SPAN)
        expanded = (resampled - self._band_floor) / span
        gate = np.clip((self._band_ceiling - self.GATE_FLOOR) / self.GATE_WIDTH, 0.0, 1.0)
        # 0.92 leaves the top of the display for the spring's overshoot.
        self.targets = np.clip(expanded * gate, 0.0, 1.0) * 0.92

    def _advance(self) -> None:
        """One frame of spring physics, independent of the audio buffer rate."""
        if not self.active:
            self.targets *= 0.90
        delta = self.targets - self.levels
        rising = delta > 0
        acceleration = delta * self.STIFFNESS - self.velocity * self.DAMPING
        falling_velocity = self.velocity + acceleration
        # Rising: chase the target directly, and carry a little of that move as
        # velocity so the bar still overshoots slightly instead of arriving dead.
        self.velocity = np.where(rising, delta * self.ATTACK * 0.35, falling_velocity)
        self.levels = np.clip(
            np.where(rising, self.levels + delta * self.ATTACK, self.levels + falling_velocity),
            0.0, 1.35,
        )
        risen = self.levels >= self.peaks
        self.peaks[risen] = self.levels[risen]
        self.peak_velocity[risen] = 0.0
        self.peak_velocity[~risen] += self.PEAK_GRAVITY
        self.peaks = np.maximum(self.peaks - self.peak_velocity, self.levels)
        self._phase += self.FRAME_MS / 1000.0
        self._advance_effects()
        # Idle and settled: stop burning frames until audio or a repaint returns.
        if not self.active and float(self.levels.max()) < 0.002 and float(self.peaks.max()) < 0.002:
            self.levels[:] = 0.0
            self.peaks[:] = 0.0
            self.velocity[:] = 0.0
            self._timer.stop()
        self.update()

    def _advance_effects(self) -> None:
        """Move the styles that carry state, whether or not one is on screen.

        Kept cheap and unconditional: switching style should show motion that is
        already under way rather than a field that starts from nothing.
        """
        self._history.append(self.levels[:: self.TRAIL_STRIDE].copy())
        if len(self._history) > self.TRAIL_FRAMES:
            del self._history[0]
        bass = float(self.levels[: self.POINTS // 6].mean())
        # Embers drift up faster when there is more low end behind them.
        self._ember_y -= self._ember_rise * (0.35 + bass * 2.2)
        spent = self._ember_y <= 0.0
        if spent.any():
            count = int(spent.sum())
            self._ember_y[spent] = 1.0
            self._ember_x[spent] = np.random.default_rng().random(count).astype(np.float32)
        # A ring per bass onset, not per loud frame, or they arrive as a wall.
        onset = bass > 0.45 and bass > self._bass_previous * 1.25
        if onset:
            self._sonar.append(0.0)
        self._sonar = [radius + 0.018 for radius in self._sonar if radius < 1.0][-8:]
        self._bass_previous = bass

    # ----- painting --------------------------------------------------------
    def _gradient(self, rect: QRectF, vertical: bool = False) -> QLinearGradient:
        low, mid, high = VISUALIZER_THEMES[self.theme_name][1]
        if vertical:
            gradient = QLinearGradient(rect.left(), rect.bottom(), rect.left(), rect.top())
        else:
            gradient = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
        gradient.setColorAt(0.0, QColor(low))
        gradient.setColorAt(0.5, QColor(mid))
        gradient.setColorAt(1.0, QColor(high))
        return gradient

    def _colors(self) -> tuple[QColor, QColor, QColor]:
        low, mid, high = VISUALIZER_THEMES[self.theme_name][1]
        return QColor(low), QColor(mid), QColor(high)

    @staticmethod
    def _smooth_path(points: list[QPointF]) -> QPainterPath:
        """Quadratic midpoint smoothing - curvy without cubic overshoot."""
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        for index in range(1, len(points)):
            previous, current = points[index - 1], points[index]
            middle = QPointF((previous.x() + current.x()) / 2, (previous.y() + current.y()) / 2)
            path.quadTo(previous, middle)
        path.lineTo(points[-1])
        return path

    @staticmethod
    def _closed_smooth_path(points: list[QPointF]) -> QPainterPath:
        """Smoothing that wraps, so a ring has no seam where it closes."""
        path = QPainterPath()
        if len(points) < 3:
            return path
        midpoint = lambda a, b: QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        start = midpoint(points[-1], points[0])
        path.moveTo(start)
        for index, current in enumerate(points):
            following = points[(index + 1) % len(points)]
            path.quadTo(current, midpoint(current, following))
        path.closeSubpath()
        return path

    def _circular_levels(self) -> np.ndarray:
        """Mirror the spectrum around the circle.

        Wrapped straight round, the silent treble end leaves a dead arc and the
        loud bass end a lopsided clump; mirroring gives a symmetric figure whose
        ends meet seamlessly.
        """
        half = self.levels[: self.POINTS // 2]
        return np.concatenate([half, half[::-1]])

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        if rect.width() < 8 or rect.height() < 8:
            painter.fillRect(rect, self.background_color())
            return
        painter.fillRect(rect, self.background_color())
        painter.setPen(Qt.PenStyle.NoPen)
        getattr(self, f"_draw_{self.style_name.lower()}")(painter, rect)

    # Each style below owns its whole look; they share only the level array.
    def _draw_bars(self, painter: QPainter, rect: QRectF) -> None:
        count = self.POINTS
        slot = rect.width() / count
        width = slot * 0.62
        floor = rect.bottom() - 3
        usable = rect.height() - 10
        painter.setBrush(self._gradient(rect))
        for index in range(count):
            height = float(self.levels[index]) * usable
            if height < 1.2:
                continue
            x = rect.left() + index * slot + (slot - width) / 2
            painter.drawRect(QRectF(x, floor - height, width, height))

    def _draw_mirror(self, painter: QPainter, rect: QRectF) -> None:
        count = self.POINTS
        slot = rect.width() / count
        width = slot * 0.6
        middle = rect.center().y()
        usable = (rect.height() - 8) / 2
        painter.setBrush(self._gradient(rect))
        for index in range(count):
            height = float(self.levels[index]) * usable
            if height < 1.0:
                continue
            x = rect.left() + index * slot + (slot - width) / 2
            painter.drawRect(QRectF(x, middle - height, width, height * 2))

    def _wave_points(self, rect: QRectF, amplitude: float, middle: float) -> list[QPointF]:
        step = rect.width() / (self.POINTS - 1)
        return [
            QPointF(rect.left() + index * step, middle - float(self.levels[index]) * amplitude)
            for index in range(self.POINTS)
        ]

    def _draw_wave(self, painter: QPainter, rect: QRectF) -> None:
        points = self._wave_points(rect, rect.height() - 12, rect.bottom() - 4)
        path = self._smooth_path(points)
        fill = QPainterPath(path)
        fill.lineTo(rect.right(), rect.bottom())
        fill.lineTo(rect.left(), rect.bottom())
        fill.closeSubpath()
        gradient = self._gradient(rect)
        painter.setBrush(gradient)
        painter.setOpacity(0.30)
        painter.drawPath(fill)
        painter.setOpacity(1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(gradient, 2.4)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_ribbon(self, painter: QPainter, rect: QRectF) -> None:
        middle = rect.center().y()
        gradient = self._gradient(rect)
        for scale, opacity in ((1.0, 0.34), (0.66, 0.30), (0.36, 0.42)):
            amplitude = (rect.height() / 2 - 4) * scale
            upper = self._wave_points(rect, amplitude, middle)
            lower = [QPointF(p.x(), middle + (middle - p.y())) for p in reversed(upper)]
            path = self._smooth_path(upper)
            for point in lower:
                path.lineTo(point)
            path.closeSubpath()
            painter.setOpacity(opacity)
            painter.setBrush(gradient)
            painter.drawPath(path)
        painter.setOpacity(1.0)

    def _draw_dots(self, painter: QPainter, rect: QRectF) -> None:
        count = self.POINTS
        slot = rect.width() / count
        radius = min(3.4, slot * 0.34)
        floor = rect.bottom() - 4
        usable = rect.height() - 12
        low, mid, high = self._colors()
        painter.setBrush(self._gradient(rect))
        for index in range(count):
            x = rect.left() + index * slot + slot / 2
            painter.drawEllipse(QPointF(x, floor - float(self.levels[index]) * usable), radius, radius)
        peak_color = QColor(high)
        peak_color.setAlphaF(0.55)
        painter.setBrush(peak_color)
        for index in range(count):
            x = rect.left() + index * slot + slot / 2
            painter.drawEllipse(QPointF(x, floor - float(self.peaks[index]) * usable), radius * 0.6, radius * 0.6)

    def _draw_radial(self, painter: QPainter, rect: QRectF) -> None:
        centre = rect.center()
        inner = min(rect.height(), rect.width()) * 0.16
        reach = min(rect.height(), rect.width()) * 0.32
        painter.save()
        painter.translate(centre)
        painter.rotate(self._phase * 9.0)
        painter.setBrush(self._gradient(QRectF(-reach, -reach, reach * 2, reach * 2)))
        width = max(1.6, (2 * math.pi * inner) / self.POINTS * 0.55)
        levels = self._circular_levels()
        for index in range(self.POINTS):
            painter.save()
            painter.rotate(360.0 * index / self.POINTS)
            length = inner + float(levels[index]) * reach
            painter.drawRect(QRectF(-width / 2, -length, width, length - inner))
            painter.restore()
        painter.restore()

    def _draw_ring(self, painter: QPainter, rect: QRectF) -> None:
        centre = rect.center()
        base = min(rect.height(), rect.width()) * 0.22
        reach = min(rect.height(), rect.width()) * 0.24
        levels = self._circular_levels()
        points = []
        for index in range(self.POINTS):
            angle = 2 * math.pi * index / self.POINTS + self._phase * 0.35
            radius = base + float(levels[index]) * reach
            points.append(QPointF(centre.x() + math.cos(angle) * radius, centre.y() + math.sin(angle) * radius))
        path = self._closed_smooth_path(points)
        # Punching out the middle makes it read as a ring rather than a blob.
        hole = QPainterPath()
        hole.addEllipse(centre, base * 0.55, base * 0.55)
        painter.setOpacity(0.55)
        painter.setBrush(self._gradient(rect))
        painter.drawPath(path.subtracted(hole))
        painter.setOpacity(1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._gradient(rect), 2.0))
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_blocks(self, painter: QPainter, rect: QRectF) -> None:
        count = self.POINTS
        slot = rect.width() / count
        width = slot * 0.66
        segments = max(4, int(rect.height() // 7))
        gap = 1.6
        segment_height = (rect.height() - 8 - gap * (segments - 1)) / segments
        low, mid, high = self._colors()
        for index in range(count):
            lit = int(round(float(self.levels[index]) * segments))
            x = rect.left() + index * slot + (slot - width) / 2
            for segment in range(segments):
                y = rect.bottom() - 4 - (segment + 1) * segment_height - segment * gap
                fraction = segment / max(1, segments - 1)
                color = QColor(low) if fraction < 0.5 else QColor(mid)
                if fraction > 0.82:
                    color = QColor(high)
                color.setAlphaF(0.92 if segment < lit else 0.07)
                painter.setBrush(color)
                painter.drawRect(QRectF(x, y, width, segment_height))

    def _draw_pulse(self, painter: QPainter, rect: QRectF) -> None:
        centre = rect.center()
        third = self.POINTS // 3
        bass = float(self.levels[:third].mean())
        middle = float(self.levels[third : third * 2].mean())
        treble = float(self.levels[third * 2 :].mean())
        reach = min(rect.height(), rect.width()) * 0.46
        low, mid, high = self._colors()
        for energy, color, scale in ((bass, low, 1.0), (middle, mid, 0.72), (treble, high, 0.46)):
            radius = reach * scale * (0.34 + energy * 0.9)
            tint = QColor(color)
            tint.setAlphaF(0.20 + energy * 0.42)
            painter.setBrush(tint)
            painter.drawEllipse(centre, radius, radius)

    def _draw_aurora(self, painter: QPainter, rect: QRectF) -> None:
        low, mid, high = self._colors()
        for layer, color in enumerate((low, mid, high)):
            amplitude = (rect.height() - 10) * (0.46 + layer * 0.20)
            drift = math.sin(self._phase * (0.5 + layer * 0.24)) * rect.height() * 0.06
            step = rect.width() / (self.POINTS - 1)
            points = [
                QPointF(
                    rect.left() + index * step,
                    rect.bottom() - 2 + drift - float(self.levels[min(index + layer * 5, self.POINTS - 1)]) * amplitude,
                )
                for index in range(self.POINTS)
            ]
            path = self._smooth_path(points)
            path.lineTo(rect.right(), rect.bottom())
            path.lineTo(rect.left(), rect.bottom())
            path.closeSubpath()
            tint = QColor(color)
            tint.setAlphaF(0.34)
            painter.setBrush(tint)
            painter.drawPath(path)

    # ----- line and area families ------------------------------------------
    def _draw_needles(self, painter: QPainter, rect: QRectF) -> None:
        slot = rect.width() / self.POINTS
        floor, usable = rect.bottom() - 3, rect.height() - 10
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._gradient(rect), 1.7))
        for index in range(self.POINTS):
            height = float(self.levels[index]) * usable
            if height < 1.0:
                continue
            x = rect.left() + index * slot + slot / 2
            painter.drawLine(QPointF(x, floor), QPointF(x, floor - height))
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_steps(self, painter: QPainter, rect: QRectF) -> None:
        step = rect.width() / self.POINTS
        floor, usable = rect.bottom() - 2, rect.height() - 10
        path = QPainterPath()
        path.moveTo(rect.left(), floor)
        for index in range(self.POINTS):
            y = floor - float(self.levels[index]) * usable
            left = rect.left() + index * step
            path.lineTo(left, y)
            path.lineTo(left + step, y)
        path.lineTo(rect.right(), floor)
        path.closeSubpath()
        painter.setOpacity(0.85)
        painter.setBrush(self._gradient(rect))
        painter.drawPath(path)
        painter.setOpacity(1.0)

    def _draw_horizon(self, painter: QPainter, rect: QRectF) -> None:
        """A band between the live level and its falling peak."""
        middle = rect.center().y()
        reach = (rect.height() - 8) / 2
        step = rect.width() / (self.POINTS - 1)
        upper = [QPointF(rect.left() + i * step, middle - float(self.levels[i]) * reach)
                 for i in range(self.POINTS)]
        lower = [QPointF(rect.left() + i * step, middle + float(self.peaks[i]) * reach)
                 for i in reversed(range(self.POINTS))]
        path = self._smooth_path(upper)
        for point in lower:
            path.lineTo(point)
        path.closeSubpath()
        painter.setOpacity(0.5)
        painter.setBrush(self._gradient(rect))
        painter.drawPath(path)
        painter.setOpacity(1.0)

    def _draw_lattice(self, painter: QPainter, rect: QRectF) -> None:
        floor, usable = rect.bottom() - 3, rect.height() - 10
        step = rect.width() / (self.POINTS - 1)
        tops = [QPointF(rect.left() + i * step, floor - float(self.levels[i]) * usable)
                for i in range(self.POINTS)]
        pen = QPen(self._gradient(rect), 1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        for index in range(0, self.POINTS, 2):
            painter.drawLine(QPointF(tops[index].x(), floor), tops[index])
        painter.setPen(QPen(self._gradient(rect), 1.8))
        painter.drawPath(self._smooth_path(tops))
        painter.setPen(Qt.PenStyle.NoPen)

    # ----- symmetry families ------------------------------------------------
    def _draw_comb(self, painter: QPainter, rect: QRectF) -> None:
        slot = rect.width() / self.POINTS
        width = slot * 0.55
        middle = rect.center().y()
        reach = (rect.height() - 8) / 2
        painter.setBrush(self._gradient(rect))
        for index in range(self.POINTS):
            height = float(self.levels[index]) * reach
            if height < 1.0:
                continue
            x = rect.left() + index * slot + (slot - width) / 2
            top = middle - height if index % 2 == 0 else middle
            painter.drawRect(QRectF(x, top, width, height))

    def _draw_spine(self, painter: QPainter, rect: QRectF) -> None:
        middle = rect.center().y()
        reach = (rect.height() - 10) / 2
        slot = rect.width() / self.POINTS
        low, mid, high = self._colors()
        painter.setBrush(QColor(mid))
        painter.drawRect(QRectF(rect.left(), middle - 0.5, rect.width(), 1.0))
        painter.setBrush(self._gradient(rect))
        for index in range(self.POINTS):
            height = float(self.levels[index]) * reach
            x = rect.left() + index * slot + slot / 2
            painter.drawRect(QRectF(x - 1.0, middle - height, 2.0, height))
            painter.drawRect(QRectF(x - 1.0, middle, 2.0, height * 0.45))

    def _draw_wings(self, painter: QPainter, rect: QRectF) -> None:
        """Mirrored around the vertical centre, opening outward."""
        centre_x = rect.center().x()
        middle = rect.center().y()
        reach = (rect.height() - 8) / 2
        half = self.POINTS // 2
        step = (rect.width() / 2) / (half - 1)
        painter.setBrush(self._gradient(rect))
        painter.setOpacity(0.55)
        for direction in (1, -1):
            points = [QPointF(centre_x + direction * i * step,
                              middle - float(self.levels[i]) * reach) for i in range(half)]
            path = self._smooth_path(points)
            for i in reversed(range(half)):
                path.lineTo(QPointF(centre_x + direction * i * step,
                                    middle + float(self.levels[i]) * reach))
            path.closeSubpath()
            painter.drawPath(path)
        painter.setOpacity(1.0)

    def _draw_pillars(self, painter: QPainter, rect: QRectF) -> None:
        """Bars with a lighter cap, so each one reads as a solid block."""
        slot = rect.width() / self.POINTS
        width = slot * 0.66
        floor, usable = rect.bottom() - 3, rect.height() - 12
        low, mid, high = self._colors()
        cap = QColor(high).lighter(135)
        for index in range(self.POINTS):
            height = float(self.levels[index]) * usable
            if height < 1.5:
                continue
            x = rect.left() + index * slot + (slot - width) / 2
            painter.setBrush(self._gradient(rect))
            painter.drawRect(QRectF(x, floor - height, width, height))
            painter.setBrush(cap)
            painter.drawRect(QRectF(x, floor - height, width, 2.5))

    def _draw_equalizer(self, painter: QPainter, rect: QRectF) -> None:
        """Segmented columns with a peak cap riding above each."""
        slot = rect.width() / self.POINTS
        width = slot * 0.62
        segments = max(5, int(rect.height() // 9))
        gap = 1.8
        height = (rect.height() - 8 - gap * (segments - 1)) / segments
        low, mid, high = self._colors()
        for index in range(self.POINTS):
            lit = int(round(float(self.levels[index]) * segments))
            x = rect.left() + index * slot + (slot - width) / 2
            for segment in range(segments):
                y = rect.bottom() - 4 - (segment + 1) * height - segment * gap
                fraction = segment / max(1, segments - 1)
                colour = QColor(low if fraction < 0.55 else (mid if fraction < 0.85 else high))
                colour.setAlphaF(0.95 if segment < lit else 0.06)
                painter.setBrush(colour)
                painter.drawRect(QRectF(x, y, width, height))
            peak = int(round(float(self.peaks[index]) * segments))
            if peak:
                y = rect.bottom() - 4 - min(peak, segments) * (height + gap)
                painter.setBrush(QColor(high))
                painter.drawRect(QRectF(x, y, width, 2.0))

    # ----- point families ---------------------------------------------------
    def _draw_droplets(self, painter: QPainter, rect: QRectF) -> None:
        slot = rect.width() / self.POINTS
        floor, usable = rect.bottom() - 6, rect.height() - 14
        painter.setBrush(self._gradient(rect))
        for index in range(self.POINTS):
            level = float(self.levels[index])
            radius = 1.0 + level * min(6.5, slot * 0.6)
            x = rect.left() + index * slot + slot / 2
            painter.drawEllipse(QPointF(x, floor - level * usable), radius, radius)

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        columns = min(self.POINTS, max(8, int(rect.width() // 12)))
        rows = max(4, int(rect.height() // 11))
        cell_w, cell_h = rect.width() / columns, (rect.height() - 6) / rows
        radius = min(2.4, min(cell_w, cell_h) * 0.22)
        low, mid, high = self._colors()
        stride = self.POINTS / columns
        for column in range(columns):
            level = float(self.levels[min(self.POINTS - 1, int(column * stride))])
            lit = int(round(level * rows))
            x = rect.left() + column * cell_w + cell_w / 2
            for row in range(rows):
                y = rect.bottom() - 3 - (row + 0.5) * cell_h
                fraction = row / max(1, rows - 1)
                colour = QColor(low if fraction < 0.5 else (mid if fraction < 0.85 else high))
                colour.setAlphaF(0.95 if row < lit else 0.07)
                painter.setBrush(colour)
                painter.drawEllipse(QPointF(x, y), radius, radius)

    def _draw_scatter(self, painter: QPainter, rect: QRectF) -> None:
        """A fixed dust field lit by whatever band sits under each speck."""
        low, mid, high = self._colors()
        for x_fraction, y_fraction, scale in self._scatter:
            index = min(self.POINTS - 1, int(x_fraction * self.POINTS))
            level = float(self.levels[index])
            if level < 0.04:
                continue
            colour = QColor(low if x_fraction < 0.4 else (mid if x_fraction < 0.75 else high))
            colour.setAlphaF(min(1.0, 0.16 + level * 0.84))
            painter.setBrush(colour)
            radius = scale * (0.8 + level * 3.6)
            painter.drawEllipse(
                QPointF(rect.left() + x_fraction * rect.width(),
                        rect.bottom() - (0.06 + y_fraction * 0.88 * level) * rect.height()),
                radius, radius)

    def _draw_peaks(self, painter: QPainter, rect: QRectF) -> None:
        """Only the falling peak caps, over a faint trace of the live level."""
        slot = rect.width() / self.POINTS
        width = slot * 0.7
        floor, usable = rect.bottom() - 3, rect.height() - 10
        low, mid, high = self._colors()
        faint = QColor(mid)
        faint.setAlphaF(0.18)
        for index in range(self.POINTS):
            x = rect.left() + index * slot + (slot - width) / 2
            height = float(self.levels[index]) * usable
            if height > 1.0:
                painter.setBrush(faint)
                painter.drawRect(QRectF(x, floor - height, width, height))
        painter.setBrush(self._gradient(rect))
        for index in range(self.POINTS):
            x = rect.left() + index * slot + (slot - width) / 2
            painter.drawRect(QRectF(x, floor - float(self.peaks[index]) * usable, width, 2.4))

    # ----- moving state families -------------------------------------------
    def _draw_trail(self, painter: QPainter, rect: QRectF) -> None:
        """A waterfall: the current frame in front, older ones receding."""
        if not self._history:
            return
        depth = len(self._history)
        points_per_frame = len(self._history[0])
        step = rect.width() / (points_per_frame - 1)
        usable = (rect.height() - 8) * 0.55
        for age, frame in enumerate(self._history):
            recency = (age + 1) / depth
            offset = (1.0 - recency) * (rect.height() - 10) * 0.5
            colour = QColor(self._colors()[1] if recency < 0.75 else self._colors()[2])
            colour.setAlphaF(0.06 + recency * 0.72)
            painter.setPen(QPen(colour, 1.0 + recency * 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            points = [QPointF(rect.left() + i * step,
                              rect.bottom() - 4 - offset - float(frame[i]) * usable)
                      for i in range(points_per_frame)]
            painter.drawPath(self._smooth_path(points))
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_embers(self, painter: QPainter, rect: QRectF) -> None:
        low, mid, high = self._colors()
        for index in range(self._ember_x.size):
            x_fraction = float(self._ember_x[index])
            band = min(self.POINTS - 1, int(x_fraction * self.POINTS))
            level = float(self.levels[band])
            y_fraction = float(self._ember_y[index])
            colour = QColor(high if y_fraction < 0.4 else (mid if y_fraction < 0.75 else low))
            colour.setAlphaF(max(0.0, min(1.0, y_fraction * (0.25 + level * 0.9))))
            painter.setBrush(colour)
            radius = float(self._ember_size[index]) * (0.6 + level)
            painter.drawEllipse(
                QPointF(rect.left() + x_fraction * rect.width(),
                        rect.bottom() - y_fraction * rect.height()),
                radius, radius)

    def _draw_sonar(self, painter: QPainter, rect: QRectF) -> None:
        """Rings launched by the bass, expanding until they leave the frame."""
        centre = rect.center()
        reach = max(rect.width(), rect.height()) * 0.55
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for radius in self._sonar:
            colour = QColor(self._colors()[1])
            colour.setAlphaF(max(0.0, 0.55 * (1.0 - radius)))
            painter.setPen(QPen(colour, 1.0 + 2.0 * (1.0 - radius)))
            painter.drawEllipse(centre, radius * reach, radius * reach)
        painter.setPen(Qt.PenStyle.NoPen)
        bass = float(self.levels[: self.POINTS // 6].mean())
        core = QColor(self._colors()[0])
        core.setAlphaF(0.35 + bass * 0.5)
        painter.setBrush(core)
        size = min(rect.height(), rect.width()) * (0.05 + bass * 0.12)
        painter.drawEllipse(centre, size, size)

    # ----- circular families ------------------------------------------------
    def _draw_tunnel(self, painter: QPainter, rect: QRectF) -> None:
        centre = rect.center()
        reach = min(rect.height(), rect.width()) * 0.48
        rings = 7
        painter.setBrush(Qt.BrushStyle.NoBrush)
        low, mid, high = self._colors()
        for ring in range(rings):
            slice_start = int(self.POINTS * ring / rings)
            slice_end = int(self.POINTS * (ring + 1) / rings)
            level = float(self.levels[slice_start:slice_end].mean())
            fraction = ring / (rings - 1)
            colour = QColor(low if fraction < 0.4 else (mid if fraction < 0.75 else high))
            colour.setAlphaF(0.25 + level * 0.7)
            painter.setPen(QPen(colour, 1.4 + level * 3.0))
            radius = reach * (0.16 + 0.84 * fraction) * (0.75 + level * 0.5)
            painter.drawEllipse(centre, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_spiral(self, painter: QPainter, rect: QRectF) -> None:
        centre = rect.center()
        reach = min(rect.height(), rect.width()) * 0.46
        turns = 2.6
        painter.setBrush(self._gradient(rect))
        for index in range(self.POINTS):
            fraction = index / (self.POINTS - 1)
            angle = fraction * turns * 2 * math.pi + self._phase * 0.5
            radius = reach * (0.12 + 0.88 * fraction)
            level = float(self.levels[index])
            painter.drawEllipse(
                QPointF(centre.x() + math.cos(angle) * radius,
                        centre.y() + math.sin(angle) * radius),
                1.0 + level * 4.0, 1.0 + level * 4.0)

    def _draw_arcs(self, painter: QPainter, rect: QRectF) -> None:
        centre_x = rect.center().x()
        base = rect.bottom() - 4
        reach = rect.height() - 8
        groups = 9
        painter.setBrush(Qt.BrushStyle.NoBrush)
        low, mid, high = self._colors()
        for group in range(groups):
            start = int(self.POINTS * group / groups)
            end = int(self.POINTS * (group + 1) / groups)
            level = float(self.levels[start:end].mean())
            fraction = group / (groups - 1)
            radius = reach * (0.18 + 0.82 * fraction)
            colour = QColor(low if fraction < 0.4 else (mid if fraction < 0.75 else high))
            colour.setAlphaF(0.2 + level * 0.75)
            painter.setPen(QPen(colour, 1.2 + level * 3.4))
            span = QRectF(centre_x - radius, base - radius, radius * 2, radius * 2)
            painter.drawArc(span, 0, 180 * 16)
        painter.setPen(Qt.PenStyle.NoPen)

    def _draw_beams(self, painter: QPainter, rect: QRectF) -> None:
        """Rays fanning from the left edge, length set by each band."""
        origin = QPointF(rect.left() + 6, rect.center().y())
        reach = rect.width() - 14
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index in range(self.POINTS):
            fraction = index / (self.POINTS - 1)
            angle = math.radians(-46 + 92 * fraction)
            level = float(self.levels[index])
            length = reach * (0.10 + level * 0.9)
            colour = QColor(self._colors()[1] if fraction < 0.6 else self._colors()[2])
            colour.setAlphaF(0.18 + level * 0.8)
            painter.setPen(QPen(colour, 1.0 + level * 2.0))
            painter.drawLine(origin, QPointF(origin.x() + math.cos(angle) * length,
                                             origin.y() + math.sin(angle) * length))
        painter.setPen(Qt.PenStyle.NoPen)


class AutomationComboBox(QComboBox):
    """Selector with a deliberate chevron instead of platform-native chrome."""

    def paintEvent(self, event) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.subControls &= ~QStyle.SubControl.SC_ComboBoxArrow
        # QComboBox::paintEvent substitutes the placeholder itself when nothing is
        # selected; overriding it means doing that here, or the box reads as broken.
        if self.currentIndex() < 0 and self.placeholderText():
            option.currentText = self.placeholderText()
            option.palette.setBrush(QPalette.ColorRole.ButtonText, QBrush(QColor("#9aa1ad")))
            option.palette.setBrush(QPalette.ColorRole.Text, QBrush(QColor("#9aa1ad")))
        painter = QPainter(self)
        self.style().drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option, painter, self)
        self.style().drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option, painter, self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#315beb" if self.isEnabled() else "#b3b9c4"), 1.7))
        center_x = self.width() - 19
        center_y = self.height() // 2 - 2
        painter.drawLine(center_x - 4, center_y, center_x, center_y + 4)
        painter.drawLine(center_x, center_y + 4, center_x + 4, center_y)


class EmptyStateTable(QTableWidget):
    """Table that explains why it is empty instead of showing a blank void."""

    def __init__(self, *args, placeholder: str = "", hint: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.placeholder = placeholder
        self.hint = hint

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.rowCount():
            return
        painter = QPainter(self.viewport())
        rect = self.viewport().rect()
        # The app sizes fonts in px via the stylesheet, so pointSizeF() is -1 here.
        base = self.font().pixelSize() if self.font().pixelSize() > 0 else 14
        font = QFont(self.font())
        font.setPixelSize(base)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#8b9097"))
        painter.drawText(rect.adjusted(0, -10, 0, -10), Qt.AlignmentFlag.AlignCenter, self.placeholder)
        font.setPixelSize(max(10, base - 2))
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor("#a9aeb8"))
        painter.drawText(rect.adjusted(0, 16, 0, 16), Qt.AlignmentFlag.AlignCenter, self.hint)


class PlaybackRulesPanel(QWidget):
    """The voice-ad scheduling editor.

    A rule belongs to the whole playlist, not to one recording: at each moment
    a rule fires, every recording goes out, back to back, in list order. So the
    form asks only *when*.
    """

    # English keys; tr() turns them into the running language at build time.
    DAY_KEYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    DAY_PRESETS = (("All", (0, 1, 2, 3, 4, 5, 6)), ("Weekdays", (0, 1, 2, 3, 4)), ("Weekend", (5, 6)))

    @classmethod
    def day_name(cls, index: int) -> str:
        return tr(cls.DAY_KEYS[index])

    def __init__(self, database: Database, on_change=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        # Lets the host refresh its own "next up" readout after a rule changes.
        self.on_change = on_change
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        setup_box = QFrame()
        setup_box.setObjectName("automationCard")
        form = QVBoxLayout(setup_box)
        form.setContentsMargins(20, 17, 20, 18)
        form.setSpacing(15)

        rule_header = QHBoxLayout()
        rule_title = QLabel(tr("New playback rule"))
        rule_title.setObjectName("ruleTitle")
        rule_header.addWidget(rule_title)
        rule_header.addStretch()
        self.add_button = QPushButton(tr("Save rule"))
        self.add_button.setObjectName("saveRule")
        self.add_button.setIcon(qta.icon("fa5s.check", color="#ffffff", color_disabled="#a5abb5"))
        self.add_button.setToolTip(tr("Save automated playback"))
        self.add_button.setAccessibleName(tr("Save automated playback"))
        self.add_button.clicked.connect(self.add_schedule)
        rule_header.addWidget(self.add_button)
        form.addLayout(rule_header)

        # Said up front, because it is the thing that was not obvious before.
        self.note = QLabel()
        self.note.setObjectName("ruleSummary")
        self.note.setWordWrap(True)
        form.addWidget(self.note)

        self.time = QTimeEdit(QTime(10, 0))
        self.time.setDisplayFormat("HH:mm")
        self.time.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.time.setMinimumWidth(130)
        self.time.setToolTip(tr("Uses this computer's local clock."))
        self.time.timeChanged.connect(self._sync_controls)
        self.repeat = QSpinBox()
        self.repeat.setRange(0, 1440)
        self.repeat.setSuffix(tr(" min"))
        # 0 is a real choice, not an empty value, so it gets words rather than a tooltip.
        self.repeat.setSpecialValueText(tr("Play once a day"))
        self.repeat.setSingleStep(15)
        self.repeat.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.repeat.setMinimumWidth(130)
        self.repeat.setValue(120)
        self.repeat.valueChanged.connect(self._sync_controls)
        # Optional end of the day's run; unticked, the rule runs until midnight.
        self.until_enabled = QCheckBox(tr("Stop after"))
        self.until_enabled.toggled.connect(self._sync_controls)
        self.until = QTimeEdit(QTime(21, 0))
        self.until.setDisplayFormat("HH:mm")
        self.until.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.until.setMinimumWidth(130)
        self.until.setEnabled(False)
        self.until.timeChanged.connect(self._sync_controls)
        self.until_enabled.toggled.connect(self.until.setEnabled)
        until_field = QVBoxLayout()
        until_field.setSpacing(6)
        until_field.addWidget(self.until_enabled)
        until_field.addWidget(self.until)
        timing = QHBoxLayout()
        timing.setSpacing(14)
        timing.addLayout(self._field(tr("FIRST PLAY"), self.time), 1)
        timing.addLayout(self._field(tr("REPEAT INTERVAL"), self.repeat), 1)
        timing.addLayout(until_field, 1)
        form.addLayout(timing)

        days_block = QVBoxLayout()
        days_block.setSpacing(7)
        days_header = QHBoxLayout()
        days_label = QLabel(tr("ACTIVE DAYS"))
        days_label.setObjectName("fieldLabel")
        days_header.addWidget(days_label)
        days_header.addStretch()
        days_header.setSpacing(4)
        for text, preset_days in self.DAY_PRESETS:
            preset = QPushButton(tr(text))
            preset.setObjectName("dayPreset")
            preset.setCursor(Qt.CursorShape.PointingHandCursor)
            preset.clicked.connect(lambda _, chosen=preset_days: self._apply_day_preset(chosen))
            days_header.addWidget(preset)
        days_block.addLayout(days_header)
        days = QHBoxLayout()
        days.setSpacing(6)
        self.day_checks: list[QPushButton] = []
        for label in self.DAY_KEYS:
            check = QPushButton(tr(label))
            check.setObjectName("dayToggle")
            check.setCheckable(True)
            check.setChecked(True)
            check.setCursor(Qt.CursorShape.PointingHandCursor)
            check.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            check.toggled.connect(self._sync_controls)
            self.day_checks.append(check)
            days.addWidget(check)
        days_block.addLayout(days)
        form.addLayout(days_block)

        # Plain-language echo of the fields above, so the rule can be checked
        # without mentally reassembling it from separate inputs.
        self.summary = QLabel()
        self.summary.setObjectName("ruleSummary")
        self.summary.setWordWrap(True)
        form.addWidget(self.summary)
        layout.addWidget(setup_box)

        scheduled_header = QHBoxLayout()
        scheduled_title = QLabel(tr("Scheduled playbacks"))
        scheduled_title.setObjectName("dialogSectionTitle")
        self.remove_button = QPushButton()
        self.remove_button.setObjectName("quietControl")
        self.remove_button.setIcon(qta.icon("fa5s.trash-alt", color="#4e545c", color_disabled="#c6cad1"))
        self.remove_button.setToolTip(tr("Remove selected automation"))
        self.remove_button.setAccessibleName(tr("Remove selected automation"))
        self.remove_button.clicked.connect(self.remove_schedule)
        scheduled_header.addWidget(scheduled_title)
        scheduled_header.addStretch()
        scheduled_header.addWidget(self.remove_button)
        layout.addLayout(scheduled_header)

        self.table = EmptyStateTable(
            0, 4,
            placeholder=tr("No playback rules yet"),
            hint=tr("Saved rules appear here and run while the app stays open."),
        )
        self.table.setObjectName("automationTable")
        self.table.setHorizontalHeaderLabels([tr("First play"), tr("Days"), tr("Repeat"), tr("Until")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setFixedHeight(34)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.itemSelectionChanged.connect(self._sync_controls)
        layout.addWidget(self.table, 1)
        self.refresh()

    @staticmethod
    def _field(caption: str, widget: QWidget) -> QVBoxLayout:
        """One label-over-input pair, so every field shares the same rhythm."""
        field = QVBoxLayout()
        field.setSpacing(6)
        label = QLabel(caption)
        label.setObjectName("fieldLabel")
        field.addWidget(label)
        field.addWidget(widget)
        return field

    def _apply_day_preset(self, chosen: tuple[int, ...]) -> None:
        for index, check in enumerate(self.day_checks):
            check.setChecked(index in chosen)

    def _selected_days(self) -> list[int]:
        return [index for index, check in enumerate(self.day_checks) if check.isChecked()]

    @staticmethod
    def _interval_phrase(minutes: int) -> str:
        if minutes % 60 == 0:
            hours = minutes // 60
            return tr("every hour") if hours == 1 else tr("every {count} hours", count=hours)
        return tr("every {count} minutes", count=minutes)

    @classmethod
    def _days_label(cls, weekdays: str) -> str:
        """Short form for the table, so the column never truncates mid-word."""
        days = sorted(int(day) for day in weekdays.split(","))
        if len(days) == 7:
            return tr("Every day")
        if days == [0, 1, 2, 3, 4]:
            return tr("Mon–Fri")
        if days == [5, 6]:
            return tr("Sat & Sun")
        return ", ".join(cls.day_name(day) for day in days)

    def _days_phrase(self, days: list[int]) -> str:
        if len(days) == 7:
            return tr("every day")
        if days == [0, 1, 2, 3, 4]:
            return tr("on weekdays")
        if days == [5, 6]:
            return tr("on weekends")
        names = [self.day_name(day) for day in days]
        joined = names[0] if len(names) == 1 else tr(
            "{first} and {last}", first=", ".join(names[:-1]), last=names[-1]
        )
        return tr("on {days}", days=joined)

    @staticmethod
    def _tone(label: QLabel, tone: str) -> None:
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)

    def _sync_controls(self, *_: object) -> None:
        """Keep the buttons and the summary honest about the current form state."""
        days = self._selected_days()
        self.remove_button.setEnabled(self.table.currentRow() >= 0 and self.table.rowCount() > 0)
        until = self._until_value()
        if not days:
            self.add_button.setEnabled(False)
            self.summary.setText(tr("Pick at least one day for this rule to run."))
            self._tone(self.summary, "warn")
            return
        if until is not None and until <= self.time.time().toString("HH:mm"):
            self.add_button.setEnabled(False)
            self.summary.setText(tr("The stop time must be later than the first play."))
            self._tone(self.summary, "warn")
            return
        self.add_button.setEnabled(True)
        start = self.time.time().toString("HH:mm")
        end = tr("until {time}", time=until) if until else tr("until midnight")
        cadence = tr(", then {interval} {end}",
                     interval=self._interval_phrase(self.repeat.value()), end=end) if self.repeat.value() else ""
        self.summary.setText(tr(
            "Voice ads play {days} at {time}{cadence}, in playlist order.",
            days=self._days_phrase(days), time=start, cadence=cadence,
        ))
        self._tone(self.summary, "info")

    def _until_value(self) -> str | None:
        return self.until.time().toString("HH:mm") if self.until_enabled.isChecked() else None

    def refresh(self) -> None:
        schedules = self.database.schedules()
        self.table.setRowCount(len(schedules))
        for row, schedule in enumerate(schedules):
            values = [
                schedule.time_of_day,
                self._days_label(schedule.weekdays),
                tr("Once a day") if not schedule.repeat_minutes
                else self._interval_phrase(schedule.repeat_minutes).capitalize(),
                schedule.active_until or tr("Midnight"),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column:
                    cell.setForeground(QBrush(QColor("#5c636e")))
                self.table.setItem(row, column, cell)
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, schedule.id)
        if self.database.audio_items("announcement"):
            self.note.setText(tr("Rules apply to the whole voice-ad playlist: at each scheduled time every recording plays, one after another, in list order."))
            self._tone(self.note, "info")
        else:
            self.note.setText(tr("No recordings yet. Add some in the Voice ads panel and the rules will play them in turn."))
            self._tone(self.note, "warn")
        self._sync_controls()

    def add_schedule(self) -> None:
        selected_days = self._selected_days()
        if not selected_days:
            return
        self.database.add_schedule(
            self.time.time().toString("HH:mm"),
            selected_days,
            self.repeat.value() or None,
            self._until_value(),
        )
        self.refresh()
        if self.on_change:
            self.on_change()

    def remove_schedule(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.database.delete_schedule(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        self.refresh()
        if self.on_change:
            self.on_change()


class VoiceAutomationDialog(QDialog):
    """In-context automation settings, hosting the shared scheduling panel."""

    def __init__(self, database: Database, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Voice ad automation"))
        # A popup stays visually within the application and Qt dismisses it when
        # the user clicks outside it, matching modern in-app settings panels.
        self.setWindowFlags(Qt.WindowType.Popup)
        self.resize(680, 620)
        self.setMinimumSize(600, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        self.panel = PlaybackRulesPanel(database, parent=self)
        layout.addWidget(self.panel)

    def refresh(self) -> None:
        self.panel.refresh()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


class AudioGainDialog(QDialog):
    """Per-recording volume trim, for the one ad that came out of the mic hot.

    A slider rather than a number entry: the point is to nudge a recording
    until it sits level with the others by ear, with Preview to check as you
    go, not to hit an exact decibel figure.
    """

    MIN_DB, MAX_DB = -24, 6

    def __init__(self, item, parent: QWidget) -> None:
        super().__init__(parent)
        self.item = item
        self.setWindowTitle(tr("Adjust volume"))
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        card = QFrame()
        card.setObjectName("automationCard")
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 17, 20, 18)
        form.setSpacing(12)

        title = QLabel(tr("Adjust volume"))
        title.setObjectName("ruleTitle")
        name = QLabel(self.display_name())
        name.setObjectName("fieldLabel")
        name.setWordWrap(True)
        form.addWidget(title)
        form.addWidget(name)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(self.MIN_DB, self.MAX_DB)
        self.slider.setValue(round(max(self.MIN_DB, min(self.MAX_DB, item.gain_db))))
        self.slider.setTickInterval(6)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.value_label = QLabel()
        self.value_label.setObjectName("fieldValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value_label.setFixedWidth(64)
        self.slider.valueChanged.connect(self._on_value_changed)
        slider_row = QHBoxLayout()
        slider_row.setSpacing(10)
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.value_label)
        form.addLayout(slider_row)
        # A positive trim can only push the volume up to unity gain, which is
        # already spoken for once the master slider is near 100%.
        hint = QLabel(tr("Turns this recording down (or up, within headroom) relative to the others."))
        hint.setObjectName("cardHint")
        hint.setWordWrap(True)
        form.addWidget(hint)
        self._on_value_changed(self.slider.value())
        layout.addWidget(card)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        # quietControl is sized for icon-only buttons (fixed ~25px) and
        # clipped these labels; quietAction is the labelled-button class the
        # Settings page uses for the same kind of secondary action.
        reset = make_text_button("fa5s.undo", tr("Reset"), "quietAction")
        reset.clicked.connect(lambda: self.slider.setValue(0))
        preview = make_text_button("fa5s.play", tr("Preview"), "quietAction")
        preview.clicked.connect(self._preview)
        save = make_text_button("fa5s.check", tr("Save"), "primaryAction")
        save.clicked.connect(self.accept)
        buttons.addWidget(reset)
        buttons.addWidget(preview)
        buttons.addStretch()
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def display_name(self) -> str:
        return MainWindow.display_name(self.item.name)

    def _on_value_changed(self, value: int) -> None:
        self.value_label.setText(tr("No change") if value == 0 else f"{value:+d} dB")

    def value(self) -> float:
        return float(self.slider.value())

    def _preview(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_preview_announcement_gain"):
            parent._preview_announcement_gain(self.item, self.value())

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


class EvenSplitRow(QWidget):
    """Lays out two columns side by side at an exact 50/50 width split.

    A plain QHBoxLayout only shares *extra* space by stretch factor: the
    voice-ad table has one more fixed-width column than the music table, so
    its minimum width is larger, and equal stretch factors then settle the
    two columns a fixed amount apart instead of an even split - worse the
    narrower the window gets. Fixing each side's width on every resize keeps
    them even no matter the window size.
    """

    def __init__(self, spacing: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spacing = spacing
        self.left = QWidget()
        self.right = QWidget()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(spacing)
        layout.addWidget(self.left)
        layout.addWidget(self.right)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        half = (event.size().width() - self._spacing) // 2
        self.left.setFixedWidth(half)
        self.right.setFixedWidth(event.size().width() - self._spacing - half)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        icon = asset_path("logo.png")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1280, 760)
        self.setMinimumSize(1080, 650)
        self.database = Database(application_data_path())
        set_language(self.database.setting("language", "en"))
        self.audio = AudioController()
        self.scheduler = Scheduler(self.database)
        self.current_music_item_id: int | None = None
        self.search_fields: dict[str, QLineEdit] = {}
        # Set right before a Preview-triggered play, consumed by the very next
        # announcement_started: tells the live gain-sync to leave THAT one play
        # at its trial value instead of overwriting it back to the saved one.
        self._skip_next_gain_sync = False
        self._playback_status = ""
        self._meter_frame = 0
        self._meter_cells: dict[int, tuple] = {}
        self._build_ui()
        self._connect_services()
        self.refresh_all()
        self.scheduler.start()
        # A beat after the window is up, so the audio device has settled.
        QTimer.singleShot(500, self._autoplay_if_enabled)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        nav = QVBoxLayout(sidebar)
        # No layout inset: the brand and the nav buttons carry their own padding,
        # so a second one here only narrowed the usable width.
        nav.setContentsMargins(0, 0, 0, 8)
        nav.setSpacing(0)
        nav.addWidget(self._brand_widget())
        self.pages = QStackedWidget()
        self.nav_buttons: list[QPushButton] = []
        # Music, voice ads and scheduling are all driven from the dashboard
        # panels and the gear beside the voice-ad list, so they get no nav entry.
        for index, label in enumerate(("Dashboard", "Settings", "Playback log")):
            button = QPushButton(tr(label))
            button.setObjectName("navButtonActive" if index == 0 else "navButton")
            button.clicked.connect(lambda _, page_index=index: self._set_page(page_index))
            self.nav_buttons.append(button)
            nav.addWidget(button)
        nav.addStretch()
        nav.addWidget(QLabel(tr("Offline • Local storage")))
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(sidebar)
        self.main_splitter.addWidget(self.pages)
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 8)
        self._sidebar = sidebar
        layout.addWidget(self.main_splitter)
        self.setCentralWidget(root)
        self.pages.addWidget(self._dashboard_page())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._logs_page())
        self.setStyleSheet("""
            QWidget { background: #ffffff; color: #1f2226; font-size: 13px; }
            QMenu { background: #ffffff; border: 1px solid #d8dbe1; padding: 3px; }
            QMenu::item { background: transparent; padding: 5px 26px 5px 22px; font-size: 12px; color: #2b3038; }
            QMenu::item:selected { background: #eef2ff; color: #315beb; }
            QMenu::item:disabled { color: #b4b9c1; }
            QMenu::separator { height: 1px; background: #ecedf0; margin: 3px 6px; }
            QMenu::indicator { width: 13px; height: 13px; left: 6px; }
            QMenu::indicator:checked { image: none; background: #315beb; }
            QMenu::right-arrow { width: 7px; height: 7px; right: 8px; }
            QToolTip { background: #24282e; color: #ffffff; border: 0; padding: 4px 7px; font-size: 11px; }
            QLabel#pageTitle { font-size: 17px; font-weight: 700; color: #1f2226; background: transparent; }
            QLabel#pageHint { color: #7a818c; font-size: 11px; background: transparent; margin-bottom: 4px; }
            QFrame#settingsCard { background: #ffffff; border: 1px solid #e4e6ea; }
            QLabel#cardTitle { color: #8b9097; font-size: 10px; font-weight: 800; letter-spacing: .9px; background: transparent; }
            QLabel#cardHint { color: #8b9097; font-size: 11px; background: transparent; }
            QCheckBox { background: transparent; }
            QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #c5c9d1; background: #ffffff; }
            QCheckBox::indicator:hover { border-color: #97a0ae; }
            QCheckBox::indicator:checked { background: #315beb; border-color: #315beb; }
            QLabel#fieldValue { color: #3f454e; font-size: 11px; font-weight: 700; background: transparent; }
            QFrame#settingsCard QLabel { font-size: 12px; color: #4a505a; background: transparent; }
            QFrame#settingsCard QCheckBox { font-size: 12px; color: #2b3038; background: transparent; spacing: 8px; }
            QFrame#settingsCard QComboBox, QFrame#settingsCard QSpinBox {
                background: #ffffff; color: #242830; border: 1px solid #d9dce2; min-height: 26px; padding: 0 9px; font-size: 12px;
            }
            QFrame#settingsCard QComboBox:hover, QFrame#settingsCard QSpinBox:hover { border-color: #b9c0cb; }
            QFrame#settingsCard QComboBox:focus, QFrame#settingsCard QSpinBox:focus { border-color: #315beb; }
            QFrame#settingsCard QComboBox::drop-down { width: 26px; border: 0; background: transparent; }
            QFrame#settingsCard QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #d9dce2; outline: 0; selection-background-color: #eef2ff; selection-color: #315beb; }
            QPushButton#primaryAction { background: #315beb; color: #ffffff; font-size: 12px; font-weight: 700; padding: 6px 14px; min-height: 26px; }
            QPushButton#primaryAction:hover { background: #274ac0; }
            QPushButton#quietAction { background: #ffffff; color: #4a505a; border: 1px solid #d9dce2; font-size: 12px; font-weight: 700; padding: 6px 12px; min-height: 26px; }
            QPushButton#quietAction:hover { background: #f5f6f8; border-color: #b9c0cb; }
            QTableWidget#dataTable { border: 1px solid #e4e6ea; font-size: 11px; alternate-background-color: transparent; }
            QTableWidget#dataTable::item { padding: 3px 9px; border-bottom: 1px solid #f3f4f6; }
            QTableWidget#dataTable QHeaderView::section { background: #fafbfc; color: #8b9097; font-size: 10px; font-weight: 800; letter-spacing: .5px; padding: 5px 9px; border: 0; border-bottom: 1px solid #e9eaee; }
            #sidebar { background: #f7f7f5; color: #1f2226; border-right: 1px solid #e7e7e3; } #sidebar QLabel { background: transparent; color: #8b9097; padding: 7px 14px; }
            #brand { font-size: 29px; font-weight: 800; padding: 18px 10px 16px 12px !important; }
            #navButton, #navButtonActive { border: 0; color: #747980; text-align: left; padding: 9px 12px; margin: 1px 8px; background: transparent; font-weight: 600; }
            #navButton:hover { background: #eceeea; color: #1f2226; } #navButtonActive { background: #e9eeff; color: #315beb; }
            QPushButton { background: #315beb; color: white; border: 0; padding: 9px 13px; font-weight: 700; }
            QPushButton:hover { background: #274ac0; } QPushButton:pressed { background: #1f3aa0; }
            QPushButton:disabled { background: #edeff3; color: #a5abb5; }
            QPushButton#quietControl { background: transparent; color: #4e545c; border: 1px solid #dfe1e5; }
            QPushButton#quietControl:hover { background: #f5f6f8; border-color: #c8ccd2; }
            QPushButton#quietControl:disabled { background: transparent; color: #c6cad1; border-color: #eef0f3; }
            QPushButton#toolIcon, QPushButton#quietControl, QPushButton#iconControl, QPushButton#primaryPlay { min-width: 25px; max-width: 25px; min-height: 20px; max-height: 20px; padding: 2px; }
            QPushButton#repeatAll { min-width: 21px; max-width: 21px; min-height: 21px; max-height: 21px; padding: 1px; background: transparent; border: 0; }
            QPushButton#repeatAll:hover { background: transparent; }
            QPushButton#iconControl { background: transparent; color: #1f2226; border: 1px solid #dfe1e5; }
            QGroupBox { background: #ffffff; border: 1px solid #e6e7ea; margin-top: 14px; padding: 15px; font-weight: 750; color: #1f2226; }
            QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; }
            QTableWidget { background: #ffffff; border: 0; gridline-color: transparent; alternate-background-color: #fafafa; }
            QTableWidget::item { padding: 4px 7px; border-bottom: 1px solid #f3f3f2; } QTableWidget::item:selected { background: #e4ebff; color: #315beb; }
            QHeaderView::section { background: #fafafa; padding: 5px 7px; border: 0; font-size: 11px; font-weight: 700; color: #7a818c; }
            #radioCard { background: #ffffff; border: 1px solid #e6e7ea; padding: 0; }
            #visualControls { background: transparent; border: 0; padding: 0; }
            #visualControls QPushButton { padding: 2px; min-height: 18px; max-height: 18px; }
            #visualControls QPushButton#iconControl { padding: 1px; min-height: 18px; max-height: 18px; }
            #visualControls QPushButton#quietControl { padding: 1px; min-height: 18px; max-height: 18px; }
            #visualControls QLabel { font-size: 10px; }
            #playbackTimeline QLabel { color: #858c96; font-size: 10px; background: transparent; }
            QSlider#musicTimeline::groove:horizontal { height: 3px; background: #e0e4ec; }
            QSlider#musicTimeline::sub-page:horizontal { background: #315beb; }
            QSlider#musicTimeline::handle:horizontal { background: #315beb; width: 9px; height: 9px; margin: -3px 0; }
            QSlider#adTimeline::groove:horizontal { height: 3px; background: #e0e4ec; }
            QSlider#adTimeline::sub-page:horizontal { background: #315beb; }
            QSlider#adTimeline::handle:horizontal { background: #315beb; width: 9px; height: 9px; margin: -3px 0; }
            QFrame#queuePanel { background: #ffffff; border: 1px solid #e6e7ea; padding: 0; }
            QFrame#automationCard { background: #ffffff; border: 1px solid #e3e7f0; }
            QLabel#fieldLabel { color: #6d7480; font-size: 11px; font-weight: 700; background: transparent; }
            QLabel#ruleTitle { color: #1f2226; font-size: 14px; font-weight: 750; background: transparent; }
            QLabel#dialogSectionTitle { color: #1f2226; font-size: 13px; font-weight: 750; background: transparent; }
            QFrame#automationCard QCheckBox { background: transparent; }
            QFrame#automationCard QComboBox#automationSelector, QFrame#automationCard QTimeEdit, QFrame#automationCard QSpinBox {
                background: #f8fafc; color: #242830; border: 1px solid #dbe1ea; min-height: 36px; padding: 0 13px; font-weight: 600;
            }
            QFrame#automationCard QComboBox#automationSelector:disabled { background: #f4f5f7; color: #a5abb5; border-color: #e7e9ee; font-weight: 500; }
            QFrame#automationCard QComboBox#automationSelector:hover, QFrame#automationCard QTimeEdit:hover, QFrame#automationCard QSpinBox:hover { border-color: #b9c4d5; background: #ffffff; }
            QFrame#automationCard QComboBox#automationSelector:focus, QFrame#automationCard QTimeEdit:focus, QFrame#automationCard QSpinBox:focus { border: 1px solid #315beb; background: #ffffff; }
            QFrame#automationCard QComboBox#automationSelector::drop-down { width: 38px; border: 0; background: transparent; }
            QFrame#automationCard QComboBox#automationSelector QAbstractItemView { background: #ffffff; border: 1px solid #d9e0ec; outline: 0; padding: 5px; selection-background-color: #edf2ff; selection-color: #315beb; }
            QFrame#automationCard QComboBox#automationSelector QAbstractItemView::item { min-height: 30px; padding: 0 9px; }
            QPushButton#saveRule { min-height: 30px; max-height: 30px; padding: 2px 14px; font-size: 11px; }
            QPushButton#dayToggle { min-height: 31px; max-height: 31px; padding: 1px 7px; color: #646b75; background: #f6f7f9; border: 1px solid #e1e4e9; font-size: 11px; font-weight: 700; }
            QPushButton#dayToggle:hover { background: #eef2ff; border-color: #b9c8ff; color: #315beb; }
            QPushButton#dayToggle:checked { background: #eaf0ff; border-color: #a9bdff; color: #2549c4; }
            QPushButton#dayToggle:checked:hover { background: #dfe8ff; border-color: #8ea9ff; }
            QPushButton#dayPreset { background: transparent; color: #7a818c; border: 0; padding: 3px 8px; font-size: 11px; font-weight: 700; }
            QPushButton#dayPreset:hover { background: #eef2ff; color: #315beb; }
            QLabel#ruleSummary { background: #f6f8fc; border: 1px solid #e8ecf4; padding: 9px 12px; color: #5c636e; font-size: 12px; }
            QLabel#ruleSummary[tone="warn"] { background: #fff8ed; border-color: #f4e3c6; color: #8a6224; }
            QTableWidget#automationTable { border: 1px solid #e6e9f0; alternate-background-color: transparent; padding: 1px; }
            QTableWidget#automationTable::item { padding: 7px 10px; border-bottom: 1px solid #f2f3f6; }
            QTableWidget#automationTable QHeaderView::section { background: transparent; color: #7a818c; font-size: 11px; font-weight: 800; letter-spacing: .4px; padding: 8px 10px; border: 0; border-bottom: 1px solid #ebedf2; }
            QFrame#queuePanel QPushButton { padding: 2px; min-height: 18px; max-height: 18px; }
            QLineEdit#playlistSearch { background: #fbfbfa; border: 1px solid #e4e6ea; padding: 3px 7px; font-size: 11px; color: #2b3038; min-height: 20px; max-height: 20px; margin: 0 1px 2px 1px; }
            QLineEdit#playlistSearch:focus { border-color: #315beb; background: #ffffff; }
            QLineEdit#playlistSearch:hover { border-color: #c8ccd2; }
            QLabel#panelTitle { color: #1f2226; font-size: 12px; font-weight: 750; background: transparent; }
            QTableWidget#playlistTable { font-size: 10px; }
            QTableWidget#playlistTable::item { padding: 1px 6px; border-bottom: 1px solid #f5f5f4; }
            QTableWidget#playlistTable QHeaderView::section { background: transparent; font-size: 9px; letter-spacing: .5px; padding: 3px 6px; border-bottom: 1px solid #eeeeec; }
            QFrame#nowPlayingRow { background: #f7f9fc; border: 1px solid #edf0f5; }
            QFrame#nowPlayingRow QLabel#trackTitle { font-size: 12px; font-weight: 700; color: #2b3038; background: transparent; min-height: 0; padding: 0; }
            QLabel#eyebrow { color: #9aa0a8; background: transparent; font-size: 9px; font-weight: 800; letter-spacing: 1.1px; }
            QPushButton#onAir { color: #315beb; background: #edf1ff; border: 0; font-size: 9px; font-weight: 800; letter-spacing: .6px; padding: 4px 8px; min-height: 0; max-height: 18px; }
            QPushButton#onAir:hover { background: #dde5ff; }
            QPushButton#onAir:pressed { background: #cdd9ff; }
            QLabel#muted { color: #8b9097; font-size: 12px; background: transparent; }
            QSlider::groove:horizontal { height: 3px; background: #dfe2e8; } QSlider::sub-page:horizontal { background: #315beb; }
            QSlider::handle:horizontal { background: #315beb; width: 13px; height: 13px; margin: -5px 0; }
            QScrollBar:vertical { background: transparent; width: 7px; margin: 8px 2px 8px 0; }
            QScrollBar::handle:vertical { background: rgba(112, 130, 161, 90); min-height: 28px; }
            QScrollBar::handle:vertical:hover { background: rgba(76, 108, 157, 165); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0; background: transparent; }
            QScrollBar:horizontal { background: transparent; height: 7px; margin: 0 8px 2px 8px; }
            QScrollBar::handle:horizontal { background: rgba(112, 130, 161, 115); min-width: 28px; }
            QScrollBar::handle:horizontal:hover { background: rgba(76, 108, 157, 185); }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal, QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { width: 0; background: transparent; }
        """)
        self._size_sidebar()

    def _brand_widget(self) -> QLabel:
        """The wordmark, set in the logo's own colours.

        Two lines, tracked out so the shorter one sits to the same width as the
        longer, which is how the name reads on the sign itself.
        """
        brand = QLabel()
        brand.setObjectName("brand")
        brand.setText(
            "<div style='line-height:104%'>"
            f"<span style='color:{BRAND_GREEN}'>AŞGABAT</span><br>"
            f"<span style='color:{BRAND_GOLD}'>SDAM</span>"
            "</div>"
        )
        brand.setAccessibleName("AŞGABAT SDAM")
        return brand

    def _size_sidebar(self) -> None:
        """Fit the sidebar to its longest label in the current language.

        A width hard-coded for English clips Russian, where "Playback log"
        becomes "Журнал воспроизведения". Measured after the stylesheet lands,
        since that is what sets the nav font.
        """
        widest = 0
        brand_width = 0
        for button in self.nav_buttons:
            button.ensurePolished()
            widest = max(widest, button.fontMetrics().horizontalAdvance(button.text()))
        for label in self._sidebar.findChildren(QLabel):
            label.ensurePolished()
            if label.objectName() == "brand":
                # Ask the label how wide it actually renders. It is rich text
                # laid out by QTextDocument, which comes out wider than
                # QFontMetrics predicts for the same string, and the stylesheet
                # font it uses is not reflected in label.font() either. sizeHint
                # already includes the label's own padding.
                label.adjustSize()
                brand_width = label.sizeHint().width()
            else:
                widest = max(widest, label.fontMetrics().horizontalAdvance(label.text()))
        # Nav: button margin (8 each side) plus padding (12 each side) plus slack.
        # Brand: its own 10/12 padding plus slack. Kept tight, because every
        # pixel here is taken from the playlists.
        width = max(150, widest + 38, brand_width + 4)
        self._sidebar.setMinimumWidth(width)
        self.main_splitter.setSizes([width, max(400, self.width() - width)])

    def _change_language(self, _: int) -> None:
        code = self.language_box.currentData()
        if code == current_language():
            return
        set_language(code)
        self.database.set_setting("language", code)
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """Rebuild the interface in the newly chosen language.

        Every audio and scheduler signal is connected to a method on this
        window rather than to a widget, so the connections and the timers set
        up in _connect_services survive and must not be made a second time.
        """
        page = self.pages.currentIndex()
        # Values typed on the settings page but not yet saved would otherwise be
        # thrown away by the rebuild.
        pending = {
            "music_volume": self.music_volume.value(),
            "announcement_volume": self.announcement_volume.value(),
            "fade_duration": self.fade_duration.value(),
            "duck_music": self.duck_music.isChecked(),
            "autostart": self.autostart.isChecked(),
            "autoplay": self.autoplay.isChecked(),
            "device": self.output_device.currentData(),
        }
        # These point into widgets the rebuild is about to destroy.
        self._meter_cells.clear()
        self._position_text = ""
        self._timeline_duration = -1
        dialog = getattr(self, "_voice_automation_dialog", None)
        if dialog:
            dialog.close()
        self._build_ui()
        self.music_volume.setValue(pending["music_volume"])
        self.announcement_volume.setValue(pending["announcement_volume"])
        self.fade_duration.setValue(pending["fade_duration"])
        self.duck_music.setChecked(pending["duck_music"])
        self.autostart.setChecked(pending["autostart"])
        self.autoplay.setChecked(pending["autoplay"])
        restored = self.output_device.findData(pending["device"])
        if restored >= 0:
            self.output_device.setCurrentIndex(restored)
        self._set_page(page)
        self.refresh_all()
        self._update_dashboard_play_button()
        self._set_playback_status(self._playback_status)

    def _set_page(self, page_index: int) -> None:
        self.pages.setCurrentIndex(page_index)
        self._sync_visualizer_activity()
        for index, button in enumerate(self.nav_buttons):
            button.setObjectName("navButtonActive" if index == page_index else "navButton")
            button.style().unpolish(button)
            button.style().polish(button)

    def open_voice_automation(self) -> None:
        if getattr(self, "_voice_automation_dialog", None):
            self._voice_automation_dialog.raise_()
            return
        dialog = VoiceAutomationDialog(self.database, self)
        dialog.panel.on_change = self.update_next
        self._voice_automation_dialog = dialog
        dialog.move(
            self.geometry().center().x() - dialog.width() // 2,
            self.geometry().center().y() - dialog.height() // 2,
        )
        dialog.finished.connect(self._close_voice_automation)
        dialog.show()

    def _close_voice_automation(self, _: int) -> None:
        self._voice_automation_dialog = None
        self.refresh_schedules()
        self.refresh_audio("announcement")
        self.update_next()

    def _icon_button(self, icon_name: str, tooltip: str, object_name: str = "toolIcon") -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        color = "#ffffff" if object_name in {"toolIcon", "primaryPlay"} else "#4e545c"
        button.setIcon(qta.icon(icon_name, color=color))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button


    def _page_layout(self, heading: str, subheading: str) -> QVBoxLayout:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 20, 26, 20)
        layout.setSpacing(10)
        if heading:
            label = QLabel(heading)
            label.setObjectName("pageTitle")
            layout.addWidget(label)
        if subheading:
            hint = QLabel(subheading)
            hint.setObjectName("pageHint")
            layout.addWidget(hint)
        self._last_page = page
        return layout

    def _dashboard_page(self) -> QWidget:
        # No page title here: the player cards already say where you are.
        layout = self._page_layout("", "")
        layout.setContentsMargins(22, 12, 22, 14)
        layout.setSpacing(8)
        columns = EvenSplitRow(16)

        # ----- music column: its own player over the playlist -----
        music_column = QVBoxLayout(columns.left)
        music_column.setContentsMargins(0, 0, 0, 0)
        music_column.setSpacing(8)
        music_column.addWidget(self._music_player_card())
        music_column.addWidget(self._music_panel(), 1)

        # ----- voice-ad column: its own player over the recordings -----
        ad_column = QVBoxLayout(columns.right)
        ad_column.setContentsMargins(0, 0, 0, 0)
        ad_column.setSpacing(8)
        ad_column.addWidget(self._ad_player_card())
        ad_column.addWidget(self._ad_panel(), 1)

        layout.addWidget(columns, 1)
        return self._last_page

    # ----- shared pieces of a player card -----------------------------------
    def _player_card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("radioCard")
        card.setFixedHeight(PLAYER_CARD_HEIGHT)
        column = QVBoxLayout(card)
        column.setContentsMargins(12, 9, 12, 9)
        column.setSpacing(2)
        return card, column

    def _now_playing_row(self, eyebrow: str) -> tuple[QFrame, QLabel, QPushButton]:
        """Eyebrow, title and the on-air chip. The chip doubles as the
        visualiser picker: it is the one control always beside the display."""
        row = QFrame()
        row.setObjectName("nowPlayingRow")
        heading = QHBoxLayout(row)
        heading.setContentsMargins(10, 3, 5, 3)
        heading.setSpacing(8)
        label = QLabel(tr(eyebrow))
        label.setObjectName("eyebrow")
        title = QLabel("")
        title.setObjectName("trackTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        title.setWordWrap(False)
        chip = QPushButton(tr("\u25cf  OFF AIR"))
        chip.setObjectName("onAir")
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setToolTip(tr("Click to change the visualiser style and colours"))
        chip.clicked.connect(self._open_visualizer_menu)
        heading.addWidget(label)
        heading.addWidget(title, 1)
        heading.addWidget(chip)
        return row, title, chip

    def _new_visualizer(self) -> SpectrumVisualizer:
        visualizer = SpectrumVisualizer(
            self.database.setting("visualizer_style", "Bars"),
            self.database.setting("visualizer_theme", "Daylight"),
        )
        # Two cards side by side get half the width each; a shorter display
        # keeps the lists below them usable.
        visualizer.setMinimumHeight(PLAYER_VISUALIZER_HEIGHT)
        return visualizer

    def _timeline_row(self, slider: QSlider) -> tuple[QFrame, QLabel, QLabel]:
        row = QFrame()
        row.setObjectName("playbackTimeline")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 1, 0, 0)
        line.setSpacing(7)
        position, duration = QLabel("0:00"), QLabel("0:00")
        for label in (position, duration):
            label.setFont(self._tabular_font())
        position.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        duration.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._size_time_labels((position, duration), 0)
        slider.setRange(0, 0)
        slider.setFixedHeight(14)
        line.addWidget(position)
        line.addWidget(slider, 1)
        line.addWidget(duration)
        return row, position, duration

    def _controls_row(self) -> tuple[QFrame, QHBoxLayout]:
        row = QFrame()
        row.setObjectName("visualControls")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 4, 0, 0)
        line.setSpacing(5)
        return row, line

    # ----- music player card ------------------------------------------------
    def _music_player_card(self) -> QFrame:
        card, column = self._player_card()
        row, self.track_label, self.state_label = self._now_playing_row("NOW PLAYING")
        self.visualizer = self._new_visualizer()
        column.addWidget(row)
        column.addWidget(self.visualizer, 1)

        self.music_timeline = SeekSlider(Qt.Orientation.Horizontal)
        self.music_timeline.setObjectName("musicTimeline")
        self.music_timeline.setEnabled(False)
        self.music_timeline.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._timeline_duration = -1
        self._position_text = ""
        self._pending_seek: int | None = None
        self._pending_seek_deadline = 0.0
        self.music_timeline.scrub_preview.connect(self._preview_seek)
        self.music_timeline.seek_requested.connect(self._commit_seek)
        timeline, self.position_label, self.duration_label = self._timeline_row(self.music_timeline)
        column.addWidget(timeline)

        controls, line = self._controls_row()
        previous = self._icon_button("fa5s.step-backward", tr("Previous track"), "iconControl")
        previous.clicked.connect(self.audio.previous_track)
        self.dashboard_play = self._icon_button("fa5s.play", tr("Play music"), "primaryPlay")
        self.dashboard_play.clicked.connect(self.toggle_playlist)
        next_track = self._icon_button("fa5s.step-forward", tr("Next track"), "iconControl")
        next_track.clicked.connect(self.audio.next_track)
        volume_icon = self._icon_button("fa5s.volume-up", tr("Music volume"), "quietControl")
        volume_icon.setEnabled(False)
        self.dashboard_volume = QSlider(Qt.Orientation.Horizontal)
        self.dashboard_volume.setRange(0, 100)
        self.dashboard_volume.setValue(int(self.audio.music_volume * 100))
        self.dashboard_volume.setFixedWidth(120)
        self.dashboard_volume.valueChanged.connect(lambda value: self.audio.set_music_volume(value / 100))
        for widget in (previous, self.dashboard_play, next_track, volume_icon, self.dashboard_volume):
            line.addWidget(widget)
        line.addStretch()
        column.addWidget(controls)
        return card

    # ----- voice-ad player card ---------------------------------------------
    def _ad_player_card(self) -> QFrame:
        card, column = self._player_card()
        row, self.ad_track_label, self.ad_state_label = self._now_playing_row("VOICE AD")
        self.ad_track_label.setText(tr("Nothing playing"))
        self.ad_visualizer = self._new_visualizer()
        column.addWidget(row)
        column.addWidget(self.ad_visualizer, 1)

        # The same seekable timeline the music card has.
        self.ad_timeline = SeekSlider(Qt.Orientation.Horizontal)
        self.ad_timeline.setObjectName("adTimeline")
        self.ad_timeline.setEnabled(False)
        self.ad_timeline.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._ad_duration = -1
        self._ad_position_text = ""
        self._ad_pending_seek: int | None = None
        self._ad_pending_seek_deadline = 0.0
        self.ad_timeline.scrub_preview.connect(self._preview_ad_seek)
        self.ad_timeline.seek_requested.connect(self._commit_ad_seek)
        timeline, self.ad_position_label, self.ad_duration_label = self._timeline_row(self.ad_timeline)
        column.addWidget(timeline)

        # Same transport as the music card: previous / play-pause / next / volume.
        controls, line = self._controls_row()
        ad_previous = self._icon_button("fa5s.step-backward", tr("Previous voice ad"), "iconControl")
        ad_previous.clicked.connect(lambda: self.step_announcement(-1))
        self.ad_play = self._icon_button("fa5s.play", tr("Play selected voice recording"), "primaryPlay")
        self.ad_play.clicked.connect(self.toggle_announcement)
        ad_next = self._icon_button("fa5s.step-forward", tr("Next voice ad"), "iconControl")
        ad_next.clicked.connect(lambda: self.step_announcement(1))
        volume_icon = self._icon_button("fa5s.volume-up", tr("Voice ad volume"), "quietControl")
        volume_icon.setEnabled(False)
        self.ad_volume = QSlider(Qt.Orientation.Horizontal)
        self.ad_volume.setRange(0, 100)
        self.ad_volume.setValue(int(self.audio.announcement_volume * 100))
        self.ad_volume.setFixedWidth(120)
        self.ad_volume.valueChanged.connect(lambda value: self.audio.set_announcement_volume(value / 100))
        # What is due next belongs with the ads.
        self.next_label = QLabel(tr("No scheduled voice ad"))
        self.next_label.setObjectName("muted")
        # Ignored width: a long "Next: ..." must not push this column wider than
        # the music one. Both cards are meant to split the page evenly.
        self.next_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.next_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for widget in (ad_previous, self.ad_play, ad_next, volume_icon, self.ad_volume):
            line.addWidget(widget)
        line.addStretch()
        line.addWidget(self.next_label)
        column.addWidget(controls)
        return card

    # ----- the two lists -----------------------------------------------------
    def _music_panel(self) -> QFrame:
        box = QFrame()
        box.setObjectName("queuePanel")
        panel = QVBoxLayout(box)
        panel.setContentsMargins(5, 5, 5, 5)
        panel.setSpacing(3)
        header = QHBoxLayout()
        title = QLabel(tr("Music playlist"))
        title.setObjectName("panelTitle")
        # Mix belongs to the music list only; voice ads run to a schedule.
        mixing = self.database.setting("shuffle", "false") == "true"
        self.shuffle_button = self._icon_button("fa5s.random", tr("Mix: on"), "repeatAll")
        self.shuffle_button.setCheckable(True)
        self.shuffle_button.setChecked(mixing)
        self.shuffle_button.toggled.connect(self.toggle_shuffle)
        self.toggle_shuffle(mixing)
        self.repeat_all_button = self._icon_button("fa5s.redo-alt", tr("Repeat all: on"), "repeatAll")
        self.repeat_all_button.setCheckable(True)
        self.repeat_all_button.setChecked(True)
        self.repeat_all_button.toggled.connect(self.toggle_repeat_all)
        self.toggle_repeat_all(True)
        sync = self._icon_button("fa5s.sync-alt", tr("Sync playlist with its folder"), "repeatAll")
        sync.setIcon(qta.icon("fa5s.sync-alt", color="#315beb"))
        sync.clicked.connect(lambda _=False, button=sync: self.sync_playlist("music", button))
        header.addWidget(title)
        header.addStretch()
        header.addWidget(sync)
        header.addWidget(self.shuffle_button)
        header.addWidget(self.repeat_all_button)
        panel.addLayout(header)
        panel.addWidget(self._search_field("music"))
        self.dashboard_music_table, table = self._audio_table("music")
        self.dashboard_music_table.empty_message = tr("No music")
        self.dashboard_music_table.files_dropped.connect(lambda files: self.add_audio_files(files, "music"))
        self.dashboard_music_table.itemDoubleClicked.connect(self.play_music_item)
        panel.addWidget(table, 1)
        controls = QHBoxLayout()
        folder = self._icon_button("fa5s.folder-open", tr("Select folder"))
        folder.clicked.connect(lambda: self.import_folder("music"))
        add = self._icon_button("fa5s.file-audio", tr("Select music"), "quietControl")
        add.clicked.connect(lambda: self.import_audio("music"))
        remove = self._icon_button("fa5s.trash-alt", tr("Remove selected music"), "quietControl")
        remove.clicked.connect(lambda: self.remove_audio("music"))
        controls.addWidget(folder)
        controls.addWidget(add)
        controls.addStretch()
        controls.addWidget(remove)
        panel.addLayout(controls)
        return box

    def _ad_panel(self) -> QFrame:
        box = QFrame()
        box.setObjectName("queuePanel")
        panel = QVBoxLayout(box)
        panel.setContentsMargins(5, 5, 5, 5)
        panel.setSpacing(3)
        header = QHBoxLayout()
        title = QLabel(tr("Voice ads"))
        title.setObjectName("panelTitle")
        automation = self._icon_button("fa5s.cog", tr("Voice ad automation"))
        automation.clicked.connect(self.open_voice_automation)
        sync = self._icon_button("fa5s.sync-alt", tr("Sync voice ads with their folder"), "repeatAll")
        sync.setIcon(qta.icon("fa5s.sync-alt", color="#315beb"))
        sync.clicked.connect(lambda _=False, button=sync: self.sync_playlist("announcement", button))
        header.addWidget(title)
        header.addStretch()
        header.addWidget(sync)
        header.addWidget(automation)
        panel.addLayout(header)
        panel.addWidget(self._search_field("announcement"))
        self.dashboard_announcement_table, table = self._audio_table("announcement")
        self.dashboard_announcement_table.empty_message = tr("No voice ads")
        self.dashboard_announcement_table.files_dropped.connect(lambda files: self.add_audio_files(files, "announcement"))
        self.dashboard_announcement_table.itemDoubleClicked.connect(self.play_announcement_item)
        panel.addWidget(table, 1)
        controls = QHBoxLayout()
        folder = self._icon_button("fa5s.folder-open", tr("Select folder"))
        folder.clicked.connect(lambda: self.import_folder("announcement"))
        add = self._icon_button("fa5s.file-audio", tr("Select recordings"), "quietControl")
        add.clicked.connect(lambda: self.import_audio("announcement"))
        self.ad_remove = self._icon_button("fa5s.trash-alt", tr("Remove selected voice ad"), "quietControl")
        self.ad_remove.clicked.connect(self.remove_selected_announcement)
        controls.addWidget(folder)
        controls.addWidget(add)
        controls.addStretch()
        controls.addWidget(self.ad_remove)
        panel.addLayout(controls)
        return box

    def _search_field(self, kind: str) -> QLineEdit:
        """Filter box above a playlist."""
        box = QLineEdit()
        box.setObjectName("playlistSearch")
        box.setPlaceholderText(tr("Search"))
        box.setClearButtonEnabled(True)
        box.textChanged.connect(lambda _, k=kind: self._apply_filter(k))
        self.search_fields[kind] = box
        return box

    def _apply_filter(self, kind: str) -> None:
        """Hide rows that do not match, without touching the underlying list."""
        box = self.search_fields.get(kind)
        table = self.dashboard_music_table if kind == "music" else self.dashboard_announcement_table
        if box is None or table is None:
            return
        needle = box.text().strip().casefold()
        for row in range(table.rowCount()):
            cell = table.item(row, 0)
            hidden = bool(needle) and (cell is None or needle not in cell.text().casefold())
            table.setRowHidden(row, hidden)
        # Dragging while filtered would move a row to a position computed from
        # visible rows, which is not where it sits in the real running order.
        table.setDragEnabled(not needle)
        table.filtered = bool(needle)
        table.viewport().update()

    def _focus_audio_item(self, kind: str, item_id: int) -> None:
        """Clear the search but keep the chosen row selected and in view."""
        box = self.search_fields.get(kind)
        if box is not None and box.text():
            box.clear()          # triggers _apply_filter, unhiding every row
        table = self.dashboard_music_table if kind == "music" else self.dashboard_announcement_table
        for row in range(table.rowCount()):
            cell = table.item(row, 0)
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == item_id:
                table.selectRow(row)
                table.scrollToItem(cell, QAbstractItemView.ScrollHint.PositionAtCenter)
                table.setFocus(Qt.FocusReason.OtherFocusReason)
                return

    def _audio_table(self, kind: str) -> tuple[DropAudioTable, QWidget]:
        music = kind == "music"
        # Voice ads carry one column more than music: how long the recording
        # runs, and separately when it is next due to play.
        table = DropAudioTable(0, 3 if music else 4)
        headers = [tr("Track"), "", tr("Length")]
        if not music:
            headers.append(tr("At"))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setMouseTracking(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(22)
        # A long track name gets clipped with an ellipsis rather than stealing
        # room from the columns to its right; the full name stays in a tooltip.
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.setWordWrap(False)
        table.rows_reordered.connect(lambda source, target, k=kind, t=table: self.reorder_audio(k, source, target, t))
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(lambda pos, k=kind, t=table: self._audio_context_menu(k, t, pos))
        table.setToolTip(tr("Drag a row to change the running order."))
        # Both lists show a meter, so both need the delegate that paints it.
        table.setItemDelegate(PlaylistRowDelegate(table))
        table.setObjectName("playlistTable")
        table.setProperty("kind", kind)
        # The meter is painted at a known size, so this needs no font measuring.
        table.setColumnWidth(1, 26)
        # Wide enough for a countdown ("-12:34") and a clock time ("21:30");
        # at 48px the schedule column was clipping to "21:...".
        table.setColumnWidth(2, 58)
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        # Length is a magnitude and reads right; the schedule time is a label
        # and reads left, so their headings follow their cells.
        table.horizontalHeaderItem(2).setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if table.columnCount() > 3:
            table.horizontalHeaderItem(3).setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if not music:
            table.setColumnWidth(3, 58)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setMinimumHeight(19)
        return table, table

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        """A titled panel, so every page is built from the same block."""
        card = QFrame()
        card.setObjectName("settingsCard")
        column = QVBoxLayout(card)
        column.setContentsMargins(16, 13, 16, 15)
        column.setSpacing(11)
        heading = QLabel(title.upper())
        heading.setObjectName("cardTitle")
        column.addWidget(heading)
        return card, column

    def _settings_form(self) -> QFormLayout:
        """Consistent label/field alignment for every settings row."""
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        return form

    @staticmethod
    def _slider_row(slider: QSlider, readout: QLabel) -> QWidget:
        """A level slider is meaningless without its number beside it."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(10)
        slider.setFixedWidth(210)
        readout.setObjectName("fieldValue")
        readout.setFixedWidth(34)
        readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        line.addWidget(slider)
        line.addWidget(readout)
        line.addStretch()
        return row

    def _logs_page(self) -> QWidget:
        layout = self._page_layout(tr("Playback log"), tr("Local audit trail for scheduled and manual playback."))
        card, column = self._section(tr("Recent activity"))
        self.log_table = QTableWidget(0, 3)
        self.log_table.setObjectName("dataTable")
        self.log_table.setHorizontalHeaderLabels([tr("Time"), tr("Type"), tr("Message")])
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.setShowGrid(False)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.verticalHeader().setDefaultSectionSize(26)
        header = self.log_table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setFixedHeight(26)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.log_table.setColumnWidth(0, 150)
        column.addWidget(self.log_table, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        refresh = make_text_button("fa5s.sync-alt", tr("Refresh"), "quietAction")
        refresh.clicked.connect(self.refresh_logs)
        export = make_text_button("fa5s.file-export", tr("Export CSV"), "quietAction")
        export.clicked.connect(self.export_logs)
        buttons.addStretch()
        buttons.addWidget(refresh)
        buttons.addWidget(export)
        column.addLayout(buttons)
        layout.addWidget(card, 1)
        return self._last_page

    def _settings_page(self) -> QWidget:
        layout = self._page_layout(tr("Settings"), tr("Saved locally and applied immediately."))
        # A settings form reads badly stretched across a wide screen; one fixed
        # column keeps the cards, fields and the save button on a common edge.
        panel = QWidget()
        panel.setMaximumWidth(700)
        column_layout = QVBoxLayout(panel)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(10)
        audio_card, audio_column = self._section(tr("Audio playback"))
        form = self._settings_form()
        self.output_device = AutomationComboBox()
        self.output_device.setFixedWidth(320)
        self.audio_devices: list = []
        self.refresh_audio_devices()
        self.music_volume = QSlider(Qt.Orientation.Horizontal)
        self.music_volume.setRange(0, 100)
        self.music_volume.setValue(int(float(self.database.setting("music_volume", "0.55")) * 100))
        self.music_volume_value = QLabel()
        self.music_volume.valueChanged.connect(lambda v: self.music_volume_value.setText(f"{v}%"))
        self.music_volume_value.setText(f"{self.music_volume.value()}%")
        self.announcement_volume = QSlider(Qt.Orientation.Horizontal)
        self.announcement_volume.setRange(0, 100)
        self.announcement_volume.setValue(int(float(self.database.setting("announcement_volume", "0.85")) * 100))
        self.announcement_volume_value = QLabel()
        self.announcement_volume.valueChanged.connect(lambda v: self.announcement_volume_value.setText(f"{v}%"))
        self.announcement_volume_value.setText(f"{self.announcement_volume.value()}%")
        self.fade_duration = QSpinBox()
        self.fade_duration.setRange(0, 10_000)
        self.fade_duration.setSingleStep(250)
        self.fade_duration.setSuffix(tr(" ms"))
        self.fade_duration.setFixedWidth(110)
        self.fade_duration.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.fade_duration.setValue(int(self.database.setting("fade_duration_ms", "1500")))
        # Switching language rebuilds the interface, so it lives with the other
        # settings rather than in a menu the rebuild would have to reopen.
        self.language_box = AutomationComboBox()
        self.language_box.setFixedWidth(320)
        for code, name in LANGUAGES.items():
            self.language_box.addItem(name, code)
        index = self.language_box.findData(current_language())
        self.language_box.setCurrentIndex(max(0, index))
        self.language_box.activated.connect(self._change_language)
        form.addRow(tr("Language"), self.language_box)
        form.addRow(tr("Output device"), self.output_device)
        form.addRow(tr("Music volume"), self._slider_row(self.music_volume, self.music_volume_value))
        form.addRow(tr("Voice ad volume"), self._slider_row(self.announcement_volume, self.announcement_volume_value))
        form.addRow(tr("Fade duration"), self.fade_duration)
        audio_column.addLayout(form)
        hint = QLabel(tr("In a mall installation choose the dedicated USB audio interface here, not the computer\u2019s built-in speakers."))
        hint.setObjectName("cardHint")
        hint.setWordWrap(True)
        audio_column.addWidget(hint)
        column_layout.addWidget(audio_card)

        behaviour_card, behaviour_column = self._section(tr("Behaviour"))
        # Voice ads always pause the playlist so no music leaks underneath them;
        # this only chooses how the music comes back afterwards.
        self.duck_music = QCheckBox(tr("Fade the music back in after a voice ad"))
        self.duck_music.setChecked(self.database.setting("duck_music", "true") == "true")
        self.autostart = QCheckBox(tr("Start automatically when this computer logs in"))
        self.autostart.setChecked(autostart_enabled())
        # Together with auto-start this is what makes a power cut self-healing:
        # the machine boots, the app opens, and the music comes back by itself.
        self.autoplay = QCheckBox(tr("Start playing music when the app opens"))
        self.autoplay.setChecked(self.database.setting("autoplay", "true") == "true")
        behaviour_column.addWidget(self.duck_music)
        behaviour_column.addWidget(self.autostart)
        behaviour_column.addWidget(self.autoplay)
        column_layout.addWidget(behaviour_card)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        save = make_text_button("fa5s.check", tr("Save settings"), "primaryAction")
        save.clicked.connect(self.save_settings)
        actions.addStretch()
        actions.addWidget(save)
        column_layout.addLayout(actions)
        layout.addWidget(panel, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return self._last_page

    def _connect_services(self) -> None:
        self.audio.status_changed.connect(self._set_playback_status)
        self.audio.spectrum_ready.connect(self._on_spectrum)
        self.audio.announcement_spectrum_ready.connect(self._on_announcement_spectrum)
        self.audio.announcement_player.playbackStateChanged.connect(self._update_dashboard_play_button)
        self.audio.music_track_changed.connect(self._on_music_track_changed)
        self.audio.music_player.playbackStateChanged.connect(self._update_dashboard_play_button)
        self.audio.music_player.positionChanged.connect(self._update_timeline_position)
        self.audio.music_player.durationChanged.connect(self._update_timeline_duration)
        self.audio_device_monitor = QMediaDevices()
        self.audio_device_monitor.audioOutputsChanged.connect(self.refresh_audio_devices)
        self.audio.error.connect(self._audio_error)
        self.audio.announcement_started.connect(lambda: self.refresh_audio("announcement"))
        self.audio.announcement_started.connect(self._sync_live_announcement_gain)
        self.audio.announcement_finished.connect(self.refresh_logs)
        self.audio.announcement_finished.connect(lambda: self.refresh_audio("announcement"))
        self.scheduler.announcement_due.connect(self.play_scheduled_announcement)
        self.clock = QTimer(self); self.clock.setInterval(20_000); self.clock.timeout.connect(self._refresh_live_timeline); self.clock.start()
        self.meter_clock = QTimer(self)
        self.meter_clock.setInterval(METER_INTERVAL_MS)
        self.meter_clock.timeout.connect(self._animate_meter)
        self.meter_clock.start()
        self.playback_clock = QTimer(self)
        self.playback_clock.setInterval(80)
        self.playback_clock.timeout.connect(self._refresh_playback_timeline)
        self.playback_clock.start()
        self._update_dashboard_play_button()

    def _on_spectrum(self, values) -> None:
        """Forwarded rather than wired straight to the widget, so rebuilding the
        interface for a language change cannot leave the signal on a dead one."""
        self.visualizer.set_spectrum(values)

    def _on_announcement_spectrum(self, values) -> None:
        self.ad_visualizer.set_spectrum(values)

    def _meter_glyphs(self, kind: str = "music") -> tuple[float, ...]:
        player = self.audio.music_player if kind == "music" else self.audio.announcement_player
        if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return METER_IDLE
        return METER_FRAMES[self._meter_frame % len(METER_FRAMES)]

    def _animate_meter(self) -> None:
        """Advance the playing row's meter without rebuilding the table."""
        if not self._meter_cells or self.pages.currentIndex() != 0 or self.isMinimized():
            return
        state = QMediaPlayer.PlaybackState.PlayingState
        # Either player counts: music pauses while an ad is on air, and the ad's
        # own meter still has to move.
        if state in (self.audio.music_player.playbackState(),
                     self.audio.announcement_player.playbackState()):
            self._meter_frame += 1
        for table, row, kind in list(self._meter_cells.values()):
            cell = table.item(row, 1)
            # A refresh may have moved or cleared the row since it was recorded.
            if cell is None or not cell.data(PLAYING_ROLE):
                continue
            cell.setData(METER_ROLE, self._meter_glyphs(kind))
            remaining = self._remaining_text(kind)
            length_cell = table.item(row, 2)
            if remaining and length_cell is not None and length_cell.text() != remaining:
                length_cell.setText(remaining)

    def _set_playback_status(self, status: str) -> None:
        self._playback_status = status
        self._update_dashboard_play_button()

    def _open_visualizer_menu(self) -> None:
        """Pick the visualiser look from the on-air button itself."""
        menu = QMenu(self)
        styles = menu.addMenu(tr("Style"))
        style_group = QActionGroup(menu)
        style_group.setExclusive(True)
        for name in VISUALIZER_STYLES:
            action = styles.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == self.visualizer.style_name)
            action.triggered.connect(lambda _, chosen=name: self._set_visualizer_style(chosen))
            style_group.addAction(action)
        themes = menu.addMenu(tr("Colour"))
        theme_group = QActionGroup(menu)
        theme_group.setExclusive(True)
        for name in VISUALIZER_THEMES:
            action = themes.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == self.visualizer.theme_name)
            action.triggered.connect(lambda _, chosen=name: self._set_visualizer_theme(chosen))
            theme_group.addAction(action)
        anchor = self.sender() if isinstance(self.sender(), QPushButton) else self.state_label
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _set_visualizer_style(self, name: str) -> None:
        self.visualizer.set_style(name)
        self.ad_visualizer.set_style(name)
        self.database.set_setting("visualizer_style", name)

    def _set_visualizer_theme(self, name: str) -> None:
        self.visualizer.set_theme(name)
        self.ad_visualizer.set_theme(name)
        self.database.set_setting("visualizer_theme", name)

    def _sync_visualizer_activity(self) -> None:
        """Run each spectrum only while its player is live and on screen."""
        visible = self.pages.currentIndex() == 0 and not self.isMinimized()
        playing = QMediaPlayer.PlaybackState.PlayingState
        music_live = visible and self.audio.music_player.playbackState() == playing
        ad_live = visible and self.audio.announcement_player.playbackState() == playing
        self.visualizer.set_active(music_live)
        self.ad_visualizer.set_active(ad_live)
        self.audio.set_analysis_enabled(music_live, ad_live)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_visualizer_activity()
        super().changeEvent(event)

    @staticmethod
    def display_name(name: str) -> str:
        """Turn a download's filename into something readable.

        Files arrive as `-_Some_Track_(SkySound.cc).mp3`; the playlists used to
        show that verbatim while the now-playing line showed a tidied version of
        the very same track.
        """
        # Not Path().stem: it cuts at the LAST dot, which eats the tail of names
        # like "Track_(SkySound.cc)" or "Harris_ft._Rihanna". Only a real audio
        # extension is removed, and the stored names have none to begin with.
        clean_name = name
        suffix = Path(clean_name).suffix.lower()
        if suffix in AUDIO_EXTENSIONS:
            clean_name = clean_name[: -len(suffix)]
        clean_name = re.sub(r"\s*\((?:SkySound\.cc|www\.[^)]*)\)\s*", " ", clean_name, flags=re.IGNORECASE)
        clean_name = clean_name.lstrip("-_ ")
        clean_name = re.sub(r"[_-]+", " ", clean_name)
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        return clean_name or name

    def _on_music_track_changed(self, name: str) -> None:
        self.track_label.setText(self.display_name(name))
        items = self.database.audio_items("music")
        if items:
            self.current_music_item_id = items[max(0, min(self.audio.track_index, len(items) - 1))].id
            self.database.set_setting("last_track_id", str(self.current_music_item_id))
        self.refresh_audio("music")

    @staticmethod
    def _tabular_font() -> QFont:
        """Digits of equal width.

        With proportional figures a 1 is narrower than a 0, so a ticking clock
        changes the label's width every second and nudges the slider sideways.
        """
        font = QFont()
        try:
            font.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError, ValueError):
            # Older Qt has no font features; the fixed label width set below
            # still keeps the slider from moving.
            pass
        return font

    def _size_timeline_labels(self, duration_ms: int) -> None:
        self._size_time_labels((self.position_label, self.duration_label), duration_ms)

    @staticmethod
    def _size_time_labels(labels, duration_ms: int) -> None:
        """Reserve room for the longest time this item can show, once."""
        sample = "0:00:00" if duration_ms >= 3_600_000 else "00:00"
        width = QFontMetrics(labels[0].font()).horizontalAdvance(sample) + 4
        for label in labels:
            label.setFixedWidth(width)

    @staticmethod
    def _format_playback_time(milliseconds: int) -> str:
        total_seconds = max(0, milliseconds // 1_000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

    def _update_timeline_position(self, position: int) -> None:
        if self.music_timeline.isSliderDown():
            return
        if self._pending_seek is not None:
            # A just-issued seek has not landed yet; the player still reports the
            # old position for a moment.  Honouring it would snap the handle back
            # to where the track was before the click.
            if abs(position - self._pending_seek) > SEEK_TOLERANCE_MS and time.monotonic() < self._pending_seek_deadline:
                return
            self._pending_seek = None
        self.music_timeline.setValue(position)
        self._show_position(position)

    def _show_position(self, position: int) -> None:
        text = self._format_playback_time(position)
        if text != self._position_text:
            self._position_text = text
            self.position_label.setText(text)

    def _update_timeline_duration(self, duration: int) -> None:
        duration = max(0, duration)
        if duration == self._timeline_duration:
            return
        self._timeline_duration = duration
        # A new track invalidates any seek still in flight against the old one.
        self._pending_seek = None
        self.music_timeline.setRange(0, duration)
        self.music_timeline.setEnabled(duration > 0)
        self._size_timeline_labels(duration)
        self.duration_label.setText(self._format_playback_time(duration))

    def _refresh_playback_timeline(self) -> None:
        """Keep the UI smooth even when a platform emits sparse position events."""
        player = self.audio.music_player
        self._update_timeline_duration(player.duration())
        self._update_timeline_position(player.position())
        self._refresh_ad_timeline()

    def _refresh_ad_timeline(self) -> None:
        """Progress of the announcement on air; blank when there is none."""
        player = self.audio.announcement_player
        live = player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
        duration = max(0, player.duration()) if live else 0
        position = player.position() if live else 0
        if duration != self._ad_duration:
            self._ad_duration = duration
            self._ad_pending_seek = None
            self.ad_timeline.setRange(0, duration)
            self.ad_timeline.setEnabled(duration > 0)
            self._size_time_labels((self.ad_position_label, self.ad_duration_label), duration)
            self.ad_duration_label.setText(self._format_playback_time(duration))
        if self.ad_timeline.isSliderDown():
            return
        if self._ad_pending_seek is not None:
            # Same as the music timeline: a seek just issued has not landed yet.
            if abs(position - self._ad_pending_seek) > SEEK_TOLERANCE_MS and time.monotonic() < self._ad_pending_seek_deadline:
                return
            self._ad_pending_seek = None
        self.ad_timeline.setValue(position)
        self._show_ad_position(position)

    def _show_ad_position(self, position: int) -> None:
        text = self._format_playback_time(position)
        if text != self._ad_position_text:
            self._ad_position_text = text
            self.ad_position_label.setText(text)

    def _preview_ad_seek(self, position: int) -> None:
        self._show_ad_position(position)

    def _commit_ad_seek(self, position: int) -> None:
        self._ad_pending_seek = position
        self._ad_pending_seek_deadline = time.monotonic() + SEEK_SETTLE_SECONDS
        self.audio.seek_announcement(position)
        self._show_ad_position(position)

    def _preview_seek(self, position: int) -> None:
        """Show where the drag is without making the decoder chase every pixel."""
        self._show_position(position)

    def _commit_seek(self, position: int) -> None:
        self._pending_seek = position
        self._pending_seek_deadline = time.monotonic() + SEEK_SETTLE_SECONDS
        self.audio.seek(position)
        self._show_position(position)

    def _update_dashboard_play_button(self, *_: object) -> None:
        """Play button, both on-air chips and the visualisers, from the players.

        Read off the players' own states rather than the last status string:
        one stream of strings could not describe two players at once, so the
        music chip used to light up for an announcement.
        """
        playing_state = QMediaPlayer.PlaybackState.PlayingState
        music = self.audio.music_player.playbackState()
        ad = self.audio.announcement_player.playbackState()
        playing = music == playing_state
        self.dashboard_play.setIcon(qta.icon("fa5s.pause" if playing else "fa5s.play", color="#ffffff"))
        label = tr("Pause music") if playing else tr("Play music")
        self.dashboard_play.setToolTip(label)
        self.dashboard_play.setAccessibleName(label)
        if playing:
            self.state_label.setText(tr("●  ON AIR"))
        elif music == QMediaPlayer.PlaybackState.PausedState:
            self.state_label.setText(tr("●  PAUSED"))
        else:
            self.state_label.setText(tr("●  OFF AIR"))
        ad_playing = ad == playing_state
        ad_paused = ad == QMediaPlayer.PlaybackState.PausedState
        self.ad_play.setIcon(qta.icon("fa5s.pause" if ad_playing else "fa5s.play", color="#ffffff"))
        ad_label = tr("Pause voice ad") if ad_playing else (
            tr("Resume voice ad") if ad_paused else tr("Play selected voice recording"))
        self.ad_play.setToolTip(ad_label)
        self.ad_play.setAccessibleName(ad_label)
        self.ad_state_label.setText(
            tr("●  ON AIR") if ad_playing else (tr("●  PAUSED") if ad_paused else tr("●  OFF AIR")))
        self._sync_visualizer_activity()

    def _refresh_live_timeline(self) -> None:
        self.refresh_audio("music")
        self.refresh_audio("announcement")
        self.update_next()

    def refresh_all(self) -> None:
        # An unattended install logs continuously; trim the tail once per start.
        self.database.prune_logs()
        self.apply_audio_settings()
        self.sync_default_music_folder()
        self.refresh_audio("music")
        self.refresh_audio("announcement")
        self.refresh_schedules()
        self.refresh_logs()
        self.update_next()

    def apply_audio_settings(self) -> None:
        self.audio.configure(
            float(self.database.setting("music_volume", "0.55")),
            float(self.database.setting("announcement_volume", "0.85")),
            self.database.setting("duck_music", "true") == "true",
            int(self.database.setting("fade_duration_ms", "1500")),
        )
        for slider, level in (
            (getattr(self, "dashboard_volume", None), self.audio.music_volume),
            (getattr(self, "ad_volume", None), self.audio.announcement_volume),
        ):
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(int(level * 100))
                slider.blockSignals(False)
        self._route_saved_device()

    def _route_saved_device(self) -> None:
        """Send audio to the device chosen in Settings, if it is present.

        Called again whenever the OS reports a device change. At boot a USB
        interface often enumerates after the app has started; previously it
        then appeared in the list but the sound stayed on the built-in speakers.
        """
        selected_id = self.database.setting("audio_device", "") if self.database.setting("audio_device_mode", "auto") == "manual" else ""
        if not selected_id:
            self.audio.set_audio_device(QMediaDevices.defaultAudioOutput())
            return
        for device in QMediaDevices.audioOutputs():
            if device.id().data().hex() == selected_id:
                self.audio.set_audio_device(device)
                return
        self.audio.set_audio_device(QMediaDevices.defaultAudioOutput())

    def refresh_audio_devices(self) -> None:
        """Keep the output list and automatic route current for Bluetooth/USB changes."""
        if not hasattr(self, "output_device"):
            return
        selected_id = self.output_device.currentData() if self.output_device.count() else (
            self.database.setting("audio_device", "") if self.database.setting("audio_device_mode", "auto") == "manual" else ""
        )
        self.audio_devices = list(QMediaDevices.audioOutputs())
        self.output_device.clear()
        self.output_device.addItem(tr("Automatic (system default)"), "")
        for device in self.audio_devices:
            self.output_device.addItem(device.description(), device.id().data().hex())
        index = self.output_device.findData(selected_id)
        self.output_device.setCurrentIndex(index if index >= 0 else 0)
        self._route_saved_device()

    def save_settings(self) -> None:
        music = self.music_volume.value() / 100
        announcement = self.announcement_volume.value() / 100
        self.database.set_setting("music_volume", str(music))
        self.database.set_setting("announcement_volume", str(announcement))
        self.database.set_setting("duck_music", "true" if self.duck_music.isChecked() else "false")
        self.database.set_setting("fade_duration_ms", str(self.fade_duration.value()))
        self.database.set_setting("autoplay", "true" if self.autoplay.isChecked() else "false")
        selected = self.output_device.currentData() or ""
        self.database.set_setting("audio_device", selected)
        self.database.set_setting("audio_device_mode", "manual" if selected else "auto")
        try:
            set_autostart_enabled(self.autostart.isChecked())
        except OSError as error:
            QMessageBox.warning(self, APP_NAME, f"Could not update auto-start: {error}")
        self.apply_audio_settings()
        self.database.log("settings", "Audio and startup settings updated")
        self.refresh_logs()

    def sync_default_music_folder(self) -> None:
        self.sync_folder("music")

    def sync_folder(self, kind: str) -> int | None:
        """Add whatever is new in the remembered folder. None when there is none.

        Removals stay removed (skip_dismissed): a sync picks up files that were
        added, it does not undo what the operator took out. Both folders are
        chosen deliberately with the folder button, so both are scanned the
        way that button imports them: recursively.
        """
        folder_value = self.database.setting(f"{kind}_folder", "")
        if not folder_value:
            return None
        folder = Path(folder_value)
        if not folder.is_dir():
            return None
        files = folder.rglob("*")
        return self.database.add_audio_batch(
            (str(file) for file in files if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS),
            kind,
            skip_dismissed=True,
        )

    def sync_playlist(self, kind: str, button: QPushButton | None = None) -> None:
        """The sync button: rescan the folder, say how many files were new.

        The button is passed in rather than read from sender(): through a
        lambda-connected slot sender() is not dependable, and it is None when
        the method is called directly.
        """
        added = self.sync_folder(kind)
        if added is None:
            QMessageBox.information(self, APP_NAME, tr("Select a folder for this list first; sync re-scans it."))
            return
        self.refresh_audio(kind)
        self.update_next()
        self.database.log(kind, f"Synced folder: {added} new file(s)")
        self.refresh_logs()
        if button is not None:
            QToolTip.showText(button.mapToGlobal(button.rect().bottomLeft()),
                              tr("Added {count} new file(s)", count=added), button)

    def refresh_audio(self, kind: str) -> None:
        tables = (self.dashboard_music_table,) if kind == "music" else (self.dashboard_announcement_table,)
        items = self.database.audio_items(kind)
        time_map = {} if kind == "music" else voice_ad_start_times(
            self.database.schedules(), datetime.now(), [item.id for item in items],
        )
        if kind == "music":
            playing_item_id = self.current_music_item_id
        else:
            # Resolved by path: a queued ad is started inside the controller,
            # so its id is not known here until it actually holds the output.
            live = self.audio.current_announcement
            playing_item_id = next((item.id for item in items if item.path == live), None)
        for table in tables:
            if isinstance(table, DropAudioTable) and table.is_reordering():
                continue
            self._fill_audio_table(table, items, time_map, playing_item_id)
        if kind == "music":
            self.audio.set_music([item.path for item in items])
        else:
            live = self.audio.current_announcement
            self.ad_track_label.setText(self.display_name(live.name) if live else tr("Nothing playing"))
            self.refresh_schedules()

    def _fill_audio_table(self, table: QTableWidget, items: list, time_map: dict, playing_item_id: int | None) -> None:
        """Update cells in place.

        This runs on a timer while the operator may be scrolling or selecting.
        Rebuilding every row allocated two widgets per row per table and reset
        the selection each time; only rows whose text actually changed are touched.
        """
        kind = table.property("kind")
        self._meter_cells.pop(id(table), None)
        selected_item = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        selected_id = selected_item.data(Qt.ItemDataRole.UserRole) if selected_item else None
        columns = table.columnCount()
        if table.rowCount() != len(items):
            table.setRowCount(len(items))
        total_lengths: list[str] = []
        table.setUpdatesEnabled(False)
        try:
            for row, item in enumerate(items):
                is_playing = item.id == playing_item_id
                if is_playing:
                    self._meter_cells[id(table)] = (table, row, kind)
                # While a row plays, its length column counts down instead, so
                # the list says how much of it is left rather than how long it is.
                length_text = self._remaining_text(kind) if is_playing else None
                if length_text is None:
                    length_text = self._format_track_length(item.path)
                total_lengths.append(self._format_track_length(item.path))
                display = self.display_name(item.name)
                if item.gain_db:
                    # A visible reminder that this one has been trimmed, so it
                    # is not forgotten and mistaken for a fresh mismatch later.
                    display += f"  ({item.gain_db:+.0f} dB)"
                values = [display, "", length_text]
                if columns > 3:
                    scheduled_time = time_map.get(item.id)
                    values.append(self._next_play_label(scheduled_time))
                for column, text in enumerate(values[:columns]):
                    self._set_audio_cell(table, row, column, text, item.id, is_playing)
                meter_cell = table.item(row, 1)
                if meter_cell is not None:
                    meter_cell.setData(METER_ROLE, self._meter_glyphs(kind) if is_playing else None)
                if item.id == selected_id and table.currentRow() != row:
                    table.selectRow(row)
        finally:
            table.setUpdatesEnabled(True)
        self._size_time_columns(table, total_lengths)
        if kind in self.search_fields:
            self._apply_filter(kind)

    @staticmethod
    def _size_time_columns(table: QTableWidget, lengths: list[str]) -> None:
        """Fit the time columns to the widest thing they can ever hold.

        A width sized for "3:59" clips "1:23:23". Two things decide it: the
        longest time in the list *in its countdown form*, and the column
        heading, which is often the wider of the two. Both are derived from the
        item list rather than from what happens to be on screen - measuring the
        current contents made the width depend on whether a row was counting
        down, so it jittered every time playback started.
        """
        if not lengths:
            return
        metrics = table.fontMetrics()
        header = table.horizontalHeader().fontMetrics()

        def fit(column: int, widest_text: int) -> None:
            item = table.horizontalHeaderItem(column)
            heading = header.horizontalAdvance(item.text()) if item else 0
            # Cell padding, the delegate's margins and the alignment inset.
            wanted = max(52, max(widest_text, heading) + 24)
            if table.columnWidth(column) != wanted:
                table.setColumnWidth(column, wanted)

        fit(2, max(metrics.horizontalAdvance("-" + text) for text in lengths))
        if table.columnCount() > 3:
            fit(3, metrics.horizontalAdvance("Wed 00:00"))

    @staticmethod
    def _next_play_label(moment) -> str:
        """The next time this ad plays. A bare clock time was ambiguous: 09:30
        for a rule that has already run today meant tomorrow, and read as today."""
        if moment is None:
            return "—"
        clock = moment.strftime("%H:%M")
        if moment.date() == datetime.now().date():
            return clock
        return f"{PlaybackRulesPanel.day_name(moment.weekday())} {clock}"

    def _remaining_text(self, kind: str) -> str | None:
        """How much of the playing item is left, as a countdown.

        Returns None before the player knows the duration, so the caller can
        fall back to the file's own length rather than showing a bogus zero.
        """
        player = self.audio.music_player if kind == "music" else self.audio.announcement_player
        duration = player.duration()
        if duration <= 0:
            return None
        remaining = max(0, duration - player.position())
        return "-" + self._format_playback_time(remaining)

    @staticmethod
    def _format_track_length(path: Path) -> str:
        seconds = audio_duration_seconds(str(path))
        if seconds <= 0:
            return "—"
        minutes, remainder = divmod(int(round(seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{remainder:02}" if hours else f"{minutes}:{remainder:02}"

    @staticmethod
    def _set_audio_cell(table: QTableWidget, row: int, column: int, text: str, item_id: int, is_playing: bool) -> None:
        cell = table.item(row, column)
        if cell is None:
            cell = QTableWidgetItem()
            if column == 1:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            elif column == 2:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, column, cell)
        elif cell.text() == text and cell.data(Qt.ItemDataRole.UserRole) == item_id and bool(cell.data(PLAYING_ROLE)) == is_playing:
            return
        cell.setText(text)
        if column == 0:
            # The visible text may be elided, so keep the whole name reachable.
            cell.setToolTip(text)
        cell.setData(Qt.ItemDataRole.UserRole, item_id)
        cell.setData(PLAYING_ROLE, is_playing)
        cell.setForeground(QColor("#315beb") if is_playing else QBrush())
        cell.setBackground(QColor("#f0f4ff") if is_playing else QBrush())
        if column == 0:
            font = cell.font()
            font.setBold(is_playing)
            cell.setFont(font)

    def _audio_context_menu(self, kind: str, table: DropAudioTable, pos) -> None:
        cell = table.itemAt(pos)
        if cell is None:
            return
        item_id = table.item(cell.row(), 0).data(Qt.ItemDataRole.UserRole)
        item = next((audio for audio in self.database.audio_items(kind) if audio.id == item_id), None)
        if item is None:
            return
        menu = QMenu(self)
        label = tr("Show in Finder") if sys.platform == "darwin" else tr("Show in Explorer")
        menu.addAction(label).triggered.connect(lambda: self.reveal_in_file_manager(item.path))
        if kind == "announcement":
            volume_label = tr("Adjust volume…")
            if item.gain_db:
                volume_label += f"  ({item.gain_db:+.0f} dB)"
            menu.addAction(volume_label).triggered.connect(lambda: self._open_gain_dialog(item))
        menu.exec(table.viewport().mapToGlobal(pos))

    def _open_gain_dialog(self, item) -> None:
        dialog = AudioGainDialog(item, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            gain = dialog.value()
            self.database.set_audio_gain(item.id, gain)
            self.database.log(
                "announcement",
                f"Reset {item.name} volume to match the others" if gain == 0
                else f"Set {item.name} volume to {gain:+.0f} dB",
            )
            self.refresh_logs()
            self.refresh_audio("announcement")

    def _sync_live_announcement_gain(self) -> None:
        """Re-read this recording's saved trim right as it actually starts.

        A queued ad was given the trim it had at the moment the whole batch
        was scheduled. If the operator fixes that recording's volume while an
        earlier ad in the same batch is still playing, this is what makes the
        correction reach it instead of it playing at the stale, already-queued
        value. Skipped exactly once for a Preview-triggered play, so the trial
        value on the dialog's slider is heard rather than immediately replaced.
        """
        if self._skip_next_gain_sync:
            self._skip_next_gain_sync = False
            return
        path = self.audio.current_announcement
        if path is None:
            return
        item = next((audio for audio in self.database.audio_items("announcement") if audio.path == path), None)
        if item is not None:
            self.audio.set_announcement_gain(item.gain_db)

    def _preview_announcement_gain(self, item, gain_db: float) -> None:
        """Play the recording at a trial gain, from the volume dialog's Preview.

        Marked so the live gain-sync (which exists precisely to make a *saved*
        edit reach an already-queued ad) does not also catch this one and
        immediately overwrite the trial value with the saved one - that would
        make Preview always just play the saved volume, silently.
        """
        self._skip_next_gain_sync = True
        self.audio.play_announcement(item.path, manual=True, gain_db=gain_db)

    @staticmethod
    def reveal_in_file_manager(path: Path) -> None:
        """Open the OS file manager with this file selected."""
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", f"/select,{path}"])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError:
            pass

    def reorder_audio(self, kind: str, source: int, target: int, table: DropAudioTable) -> None:
        """Persist a dragged row's new place in the running order."""
        items = self.database.audio_items(kind)
        if not 0 <= source < len(items):
            return
        # The insertion index counts gaps, so removing the row first shifts any
        # gap below it up by one.
        destination = target - 1 if target > source else target
        order = [item.id for item in items]
        order.insert(destination, order.pop(source))
        self.database.reorder_audio(kind, order)
        self.refresh_audio(kind)
        table.settle_row(destination)
        moved = items[source].name
        self.database.log(kind, f"Moved {moved} to position {destination + 1}")

    def import_audio(self, kind: str) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("Choose audio files"), "",
                                               tr("Audio files (*.mp3 *.wav *.aac *.m4a *.ogg *.flac)"))
        self.add_audio_files(files, kind)

    def add_audio_files(self, files: list[str], kind: str) -> None:
        if not files:
            return
        self.database.add_audio_batch(files, kind)
        self.refresh_audio(kind)

    def import_folder(self, kind: str) -> None:
        """Pick the folder a list plays from. Sync re-scans this same folder."""
        title = tr("Choose a music folder") if kind == "music" else tr("Choose a voice-ad folder")
        folder = QFileDialog.getExistingDirectory(self, title)
        if not folder:
            return
        files = [str(path) for path in Path(folder).rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS]
        self.database.set_setting(f"{kind}_folder", folder)
        self.add_audio_files(files, kind)
        self.database.log(kind, f"Imported {len(files)} file(s) from {folder}")
        self.refresh_logs()

    def import_music_folder(self) -> None:
        self.import_folder("music")

    def selected_audio(self, kind: str):
        table = self.dashboard_music_table if kind == "music" else self.dashboard_announcement_table
        row = table.currentRow()
        if row < 0: return None
        item_id = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((item for item in self.database.audio_items(kind) if item.id == item_id), None)

    def remove_audio(self, kind: str) -> None:
        item = self.selected_audio(kind)
        if not item: return
        self.database.delete_audio(item.id)
        self.refresh_audio(kind)
        self.update_next()

    def _autoplay_if_enabled(self) -> None:
        """Resume the playlist on launch, from the track that was playing.

        Picks up where a crash or power cut left off rather than always from
        the top, so a restart mid-afternoon does not replay the morning's opener.
        """
        if self.database.setting("autoplay", "true") != "true":
            return
        items = self.database.audio_items("music")
        if not items:
            return
        last = self.database.setting("last_track_id", "")
        for index, item in enumerate(items):
            if str(item.id) == last:
                self.audio.track_index = index
                break
        self.audio.play_music()
        self.database.log("music", "Background playlist started automatically")
        self.refresh_logs()

    def start_playlist(self) -> None:
        self.audio.play_music()
        self.database.log("music", "Background playlist started manually")
        self.refresh_logs()

    def toggle_playlist(self) -> None:
        if self.audio.music_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.audio.pause_music()
            return
        self.start_playlist()

    def toggle_shuffle(self, enabled: bool) -> None:
        self.audio.set_shuffle(enabled)
        self.database.set_setting("shuffle", "true" if enabled else "false")
        self.shuffle_button.setIcon(qta.icon("fa5s.random", color="#315beb" if enabled else "#9aa1ab"))
        label = tr("Mix: on") if enabled else tr("Mix: off")
        self.shuffle_button.setToolTip(label)
        self.shuffle_button.setAccessibleName(label)

    def toggle_repeat_all(self, enabled: bool) -> None:
        self.audio.repeat_all = enabled
        self.repeat_all_button.setIcon(qta.icon("fa5s.redo-alt", color="#315beb" if enabled else "#9aa1ab"))
        label = tr("Repeat all: on") if enabled else tr("Repeat all: off")
        self.repeat_all_button.setToolTip(label)
        self.repeat_all_button.setAccessibleName(label)

    def play_music_item(self, table_item: QTableWidgetItem) -> None:
        """Play the row a user double-clicks, on either music playlist view."""
        item_id = table_item.data(Qt.ItemDataRole.UserRole)
        items = self.database.audio_items("music")
        for index, item in enumerate(items):
            if item.id == item_id:
                self.audio.play_music_index(index)
                self.database.log("music", f"Playing selected track: {item.name}")
                self.refresh_logs()
                # Searching was a means of finding this track; clear it, but
                # leave the track itself selected and scrolled into view.
                self._focus_audio_item("music", item_id)
                return

    def play_announcement_item(self, table_item: QTableWidgetItem) -> None:
        """Play the voice ad a user double-clicks.

        This is a manual play and deliberately leaves the schedule alone: it
        records nothing in schedule_runs, so the recording still goes out at its
        scheduled time as well.
        """
        item_id = table_item.data(Qt.ItemDataRole.UserRole)
        item = next((audio for audio in self.database.audio_items("announcement") if audio.id == item_id), None)
        if item:
            self._play_announcement_now(item)
            self._focus_audio_item("announcement", item_id)

    def play_selected_announcement(self) -> None:
        item = self.selected_audio("announcement")
        if not item:
            QMessageBox.information(self, APP_NAME, tr("Select a voice recording in the Voice ads panel first."))
            return
        self._play_announcement_now(item)

    def step_announcement(self, delta: int) -> None:
        """Play the recording before or after the one on air (or selected)."""
        items = self.database.audio_items("announcement")
        if not items:
            return
        live = self.audio.current_announcement
        table = self.dashboard_announcement_table
        if live is not None and any(item.path == live for item in items):
            index = next(i for i, item in enumerate(items) if item.path == live)
        elif table.currentRow() >= 0:
            chosen = table.item(table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
            index = next((i for i, item in enumerate(items) if item.id == chosen), 0)
        else:
            index = 0
        target = items[(index + delta) % len(items)]
        self._play_announcement_now(target)
        self._focus_audio_item("announcement", target.id)

    def remove_selected_announcement(self) -> None:
        """Delete a recording. Rules are the playlist's, so none go with it."""
        item = self.selected_audio("announcement")
        if not item:
            QMessageBox.information(self, APP_NAME, tr("Select a voice recording in the Voice ads panel first."))
            return
        if self.audio.current_announcement == item.path:
            self.audio.stop_announcement()
        self.remove_audio("announcement")
        self.database.log("announcement", f"Removed {item.name}")
        self.refresh_logs()

    def toggle_announcement(self) -> None:
        """Play the selected recording, or pause / resume the one on air."""
        state = self.audio.announcement_player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.audio.pause_announcement()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.audio.resume_announcement()
        else:
            self.play_selected_announcement()

    def _play_announcement_now(self, item) -> None:
        if self.audio.play_announcement(item.path, manual=True, gain_db=item.gain_db):
            queued = self.audio.current_announcement != item.path
            self.database.log("manual", f"Queued {item.name} after the scheduled ad" if queued else f"Playing {item.name}")
            self.refresh_logs()
            self.refresh_audio("announcement")

    def refresh_schedules(self) -> None:
        """Only the popup shows rules now, so refresh it when it is open."""
        dialog = getattr(self, "_voice_automation_dialog", None)
        if dialog:
            dialog.refresh()

    def play_scheduled_announcement(self, item_id: int, name: str) -> None:
        item = next((audio for audio in self.database.audio_items("announcement") if audio.id == item_id), None)
        if item and self.audio.play_announcement(item.path, gain_db=item.gain_db):
            waiting = self.audio.queued_announcements()
            self.database.log("scheduled", f"Queued {name} (#{waiting} in line)" if waiting else f"Playing {name}")
        else:
            self.database.log("failed", f"Could not play {name}; file missing or too many announcements queued")
        self.refresh_logs()

    def _audio_error(self, message: str) -> None:
        self.state_label.setText(tr("●  AUDIO ERROR"))
        self.database.log("error", message)
        self.refresh_logs()

    def update_next(self) -> None:
        """The soonest moment any rule fires, and how many recordings go out then."""
        moments = upcoming_occurrences(self.database.schedules(), datetime.now(), 1)
        items = self.database.audio_items("announcement")
        if not moments or not items:
            self.next_label.setText(tr("No scheduled voice ad"))
            self.next_label.setToolTip("")
            return
        when = self._next_play_label(moments[0])
        if len(items) == 1:
            text = tr("Next: {name} — {when}", name=self.display_name(items[0].name), when=when)
        else:
            text = tr("Next: all {count} voice ads — {when}", count=len(items), when=when)
        self.next_label.setText(text)
        self.next_label.setToolTip(text)

    def refresh_logs(self) -> None:
        rows = self.database.recent_logs()
        self.log_table.setRowCount(len(rows))
        for row, log in enumerate(rows):
            # Stored as ISO for sorting; shown the way a person reads a clock.
            try:
                stamp = datetime.fromisoformat(log["occurred_at"]).strftime("%d %b  %H:%M:%S")
            except ValueError:
                stamp = log["occurred_at"]
            values = (stamp, tr(log["event_type"].replace("_", " ").capitalize()), log["message"])
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col < 2:
                    cell.setForeground(QBrush(QColor("#7a818c")))
                self.log_table.setItem(row, col, cell)

    def export_logs(self) -> None:
        default_name = f"mall-audio-log-{datetime.now():%Y-%m-%d}.csv"
        path, _ = QFileDialog.getSaveFileName(self, tr("Export playback log"), default_name, tr("CSV files (*.csv)"))
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["Time", "Type", "Message"])
            for log in reversed(self.database.recent_logs(10_000)):
                writer.writerow([log["occurred_at"], log["event_type"], log["message"]])
        self.database.log("export", f"Playback log exported to {path}")
        self.refresh_logs()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class CurveEditor(QWidget):
    pointsChanged = Signal(list)
    dragFinished = Signal()

    def __init__(self, parent=None):
        """Initialize the curve editor, point storage, active channel, and histogram overlay state."""
        super().__init__(parent)
        self.setMinimumHeight(300)
        self.setMinimumWidth(300)
        self.setMouseTracking(True)
        self._points = [(0.0, 0.0), (1.0, 1.0)]
        self._drag_index = None
        self._channel = "master"
        self._colors = {"master": QColor(230, 230, 230), "red": QColor(220, 70, 70), "green": QColor(70, 220, 70), "blue": QColor(80, 120, 240)}
        self._hist = {"r": np.zeros(256), "g": np.zeros(256), "b": np.zeros(256)}

    def set_channel(self, channel: str):
        """Switch the visible and editable curve channel."""
        self._channel = channel
        self.update()

    def set_points(self, points):
        """Load a sorted set of control points into the editor."""
        self._points = sorted(points, key=lambda p: p[0])
        self.update()

    def set_histogram(self, hist):
        """Attach histogram data for background display inside the curve editor."""
        self._hist = hist
        self.update()

    def _content_rect(self) -> QRectF:
        """Return the square plotting area used for the curve graph."""
        m = 20
        side = max(40.0, min(self.width() - 2 * m, self.height() - 2 * m))
        x = (self.width() - side) / 2.0
        y = (self.height() - side) / 2.0
        return QRectF(x, y, side, side)

    def _to_widget(self, p):
        """Convert normalized curve coordinates into widget-space coordinates."""
        r = self._content_rect()
        return QPointF(r.left() + p[0] * r.width(), r.bottom() - p[1] * r.height())

    def _to_normalized(self, pos):
        """Convert a widget-space mouse position into normalized curve coordinates."""
        r = self._content_rect()
        return (
            float(np.clip((pos.x() - r.left()) / max(1.0, r.width()), 0, 1)),
            float(np.clip((r.bottom() - pos.y()) / max(1.0, r.height()), 0, 1)),
        )

    def _find_handle(self, pos):
        """Return the index of the curve point handle under the cursor, if any."""
        for i, p in enumerate(self._points):
            if (self._to_widget(p) - pos).manhattanLength() <= 12:
                return i
        return None

    def mousePressEvent(self, event):
        """Start dragging a point, add a new point, or remove a point depending on input."""
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._find_handle(event.position())
            if idx is not None:
                self._drag_index = idx
            else:
                x, y = self._to_normalized(event.position())
                self._points.append((x, y))
                self._points.sort(key=lambda p: p[0])
                self._drag_index = min(range(len(self._points)), key=lambda i: abs(self._points[i][0] - x) + abs(self._points[i][1] - y))
                self.pointsChanged.emit(list(self._points))
                self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            idx = self._find_handle(event.position())
            if idx is not None and idx not in (0, len(self._points) - 1):
                self._points.pop(idx)
                self.pointsChanged.emit(list(self._points))
                self.dragFinished.emit()
                self.update()

    def mouseMoveEvent(self, event):
        """Move the currently dragged curve point while preserving point order constraints."""
        if self._drag_index is None:
            return
        x, y = self._to_normalized(event.position())
        i = self._drag_index
        if i == 0:
            x = 0.0
        elif i == len(self._points) - 1:
            x = 1.0
        else:
            x = float(np.clip(x, self._points[i - 1][0] + 0.001, self._points[i + 1][0] - 0.001))
        self._points[i] = (x, y)
        self._points.sort(key=lambda p: p[0])
        self._drag_index = self._points.index((x, y))
        self.pointsChanged.emit(list(self._points))
        self.update()

    def mouseReleaseEvent(self, event):
        """End a curve drag operation and emit the interaction-finished signal."""
        if self._drag_index is not None:
            self._drag_index = None
            self.dragFinished.emit()

    def paintEvent(self, event):
        """Draw the histogram background, grid, border, active curve, and curve handles."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(24, 24, 26))
        r = self._content_rect()
        channel_map = {"master": ("r", "g", "b"), "red": ("r",), "green": ("g",), "blue": ("b",)}
        visible_channels = channel_map.get(self._channel, ("r", "g", "b"))
        maxv = max(1, int(max(np.max(self._hist[ch]) for ch in visible_channels)))
        hist_colors = {"r": QColor(255, 80, 80, 85), "g": QColor(80, 255, 80, 85), "b": QColor(80, 140, 255, 85)}
        for key in visible_channels:
            path = QPainterPath()
            vals = self._hist[key].astype(np.float32) / maxv
            for i, v in enumerate(vals):
                x = r.left() + (i / 255.0) * r.width()
                y = r.bottom() - v * r.height()
                if i == 0:
                    path.moveTo(x, r.bottom())
                    path.lineTo(x, y)
                else:
                    path.lineTo(x, y)
            path.lineTo(r.right(), r.bottom())
            path.closeSubpath()
            p.fillPath(path, hist_colors[key])
        p.setPen(QPen(QColor(55, 55, 60), 1))
        for i in range(5):
            x = r.left() + i * (r.width() / 4)
            y = r.top() + i * (r.height() / 4)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(QColor(110, 110, 120), 1.2))
        p.drawRect(r)
        color = self._colors.get(self._channel, QColor(230, 230, 230))
        pts = [self._to_widget(pp) for pp in self._points]
        if pts:
            path = QPainterPath()
            path.moveTo(pts[0])
            for pt in pts[1:]:
                path.lineTo(pt)
            p.setPen(QPen(color, 2.5))
            p.drawPath(path)
            p.setBrush(color)
            for pt in pts:
                p.drawEllipse(pt, 5, 5)

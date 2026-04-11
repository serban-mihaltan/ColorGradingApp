import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class HistogramWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.hist = {"r": np.zeros(256), "g": np.zeros(256), "b": np.zeros(256)}

    def set_histogram(self, hist):
        self.hist = hist
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(20, 20, 22))
        margin = 12
        r = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)
        p.setPen(QPen(QColor(85, 85, 95), 1))
        p.drawRect(r)
        maxv = max(1, int(max(self.hist["r"].max(), self.hist["g"].max(), self.hist["b"].max())))
        colors = {"r": QColor(255, 80, 80, 120), "g": QColor(80, 255, 80, 120), "b": QColor(80, 140, 255, 120)}
        for key in ("r", "g", "b"):
            path = QPainterPath()
            vals = self.hist[key].astype("float32") / maxv
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
            p.fillPath(path, colors[key])

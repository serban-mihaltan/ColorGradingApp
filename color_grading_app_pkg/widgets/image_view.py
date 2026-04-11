from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QBrush, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView

from ..config import MIN_CROP_SIZE


class ImageView(QGraphicsView):
    cropPreviewChanged = Signal(QRect)
    imageDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setScene(QGraphicsScene(self))
        self.pixmap_item = QGraphicsPixmapItem()
        self.scene().addItem(self.pixmap_item)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(35, 35, 38))
        self._crop_mode = False
        self._crop_aspect_lock = False
        self._crop_aspect_ratio = 1.0
        self._crop_rect = QRectF()
        self._staged_crop_rect = QRectF()
        self._active_handle: Optional[str] = None
        self._drag_origin_scene = QPointF()
        self._crop_rect_at_drag = QRectF()
        self._handle_rects: Dict[str, QRectF] = {}
        self.setMouseTracking(True)

    def set_image(self, pixmap: QPixmap):
        self.pixmap_item.setPixmap(pixmap)
        self.scene().setSceneRect(QRectF(pixmap.rect()))
        self.viewport().update()

    def fit_image(self):
        if not self.pixmap_item.pixmap().isNull():
            self.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_in(self):
        self.scale(1.2, 1.2)

    def zoom_out(self):
        self.scale(1 / 1.2, 1 / 1.2)

    def set_crop_mode(self, enabled: bool):
        self._crop_mode = enabled
        self.setDragMode(QGraphicsView.DragMode.NoDrag if enabled else QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().update()

    def set_crop_lock(self, enabled: bool, ratio: float):
        self._crop_aspect_lock = enabled
        self._crop_aspect_ratio = max(0.01, ratio)

    def set_crop_rect(self, rect: QRect):
        self._crop_rect = QRectF(rect)
        self._staged_crop_rect = QRectF(rect)
        self.viewport().update()

    def clear_crop_rect(self):
        self._crop_rect = QRectF()
        self._staged_crop_rect = QRectF()
        self.viewport().update()

    def current_crop_rect(self) -> QRect:
        r = self._staged_crop_rect.normalized()
        return QRect(int(round(r.x())), int(round(r.y())), int(round(r.width())), int(round(r.height())))

    def commit_staged_crop(self):
        self._crop_rect = QRectF(self._staged_crop_rect)
        self.viewport().update()

    def revert_staged_crop(self):
        self._staged_crop_rect = QRectF(self._crop_rect)
        self.viewport().update()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path.lower().endswith((".png", ".jpg", ".jpeg")):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path.lower().endswith((".png", ".jpg", ".jpeg")):
                    self.imageDropped.emit(path)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            self.zoom_in()
        else:
            self.zoom_out()
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF):
        super().drawForeground(painter, rect)
        if not self._crop_mode or self.pixmap_item.pixmap().isNull():
            return
        bounds = QRectF(self.pixmap_item.pixmap().rect())
        crop = self._staged_crop_rect.normalized() if not self._staged_crop_rect.isNull() else QRectF(bounds.center().x() - bounds.width() * 0.25, bounds.center().y() - bounds.height() * 0.25, bounds.width() * 0.5, bounds.height() * 0.5)
        crop = crop.intersected(bounds)
        self._staged_crop_rect = crop
        overlay = QPainterPath()
        overlay.addRect(bounds)
        hole = QPainterPath()
        hole.addRect(crop)
        painter.fillPath(overlay.subtracted(hole), QColor(0, 0, 0, 120))
        painter.setPen(QPen(QColor(240, 240, 245), 1.5))
        painter.drawRect(crop)
        thirds_x = [crop.left() + crop.width() / 3, crop.left() + 2 * crop.width() / 3]
        thirds_y = [crop.top() + crop.height() / 3, crop.top() + 2 * crop.height() / 3]
        painter.setPen(QPen(QColor(230, 230, 235, 100), 1))
        for x in thirds_x:
            painter.drawLine(QPointF(x, crop.top()), QPointF(x, crop.bottom()))
        for y in thirds_y:
            painter.drawLine(QPointF(crop.left(), y), QPointF(crop.right(), y))
        hs = max(4.0, 10.0 / self.transform().m11())
        centers = {
            "tl": QPointF(crop.left(), crop.top()), "tc": QPointF(crop.center().x(), crop.top()), "tr": QPointF(crop.right(), crop.top()),
            "rc": QPointF(crop.right(), crop.center().y()), "br": QPointF(crop.right(), crop.bottom()), "bc": QPointF(crop.center().x(), crop.bottom()),
            "bl": QPointF(crop.left(), crop.bottom()), "lc": QPointF(crop.left(), crop.center().y()), "move": crop.center(),
        }
        self._handle_rects = {}
        painter.setPen(QPen(QColor(30, 30, 35), 1))
        painter.setBrush(QBrush(QColor(245, 245, 250)))
        for key, center in centers.items():
            size = hs * 1.4 if key == "move" else hs
            rh = QRectF(center.x() - size / 2, center.y() - size / 2, size, size)
            self._handle_rects[key] = rh
            if key == "move":
                painter.setBrush(QBrush(QColor(245, 245, 250, 140)))
                painter.drawEllipse(rh)
                painter.setBrush(QBrush(QColor(245, 245, 250)))
            else:
                painter.drawRect(rh)

    def _scene_pos(self, event) -> QPointF:
        return self.mapToScene(event.position().toPoint())

    def _pick_handle(self, scene_pos: QPointF) -> Optional[str]:
        for key, rect in self._handle_rects.items():
            if rect.contains(scene_pos):
                return key
        if self._staged_crop_rect.contains(scene_pos):
            return "move"
        return None

    def _clamp_crop(self, rect: QRectF) -> QRectF:
        bounds = QRectF(self.pixmap_item.pixmap().rect())
        rect = rect.normalized()
        if rect.width() < MIN_CROP_SIZE:
            rect.setWidth(MIN_CROP_SIZE)
        if rect.height() < MIN_CROP_SIZE:
            rect.setHeight(MIN_CROP_SIZE)
        if rect.left() < bounds.left():
            rect.moveLeft(bounds.left())
        if rect.top() < bounds.top():
            rect.moveTop(bounds.top())
        if rect.right() > bounds.right():
            rect.moveRight(bounds.right())
        if rect.bottom() > bounds.bottom():
            rect.moveBottom(bounds.bottom())
        return rect.intersected(bounds).normalized()

    def _apply_aspect_to_corner(self, base: QRectF, moving_corner: str, scene_pos: QPointF) -> QRectF:
        ratio = self._crop_aspect_ratio
        left, top, right, bottom = base.left(), base.top(), base.right(), base.bottom()
        if moving_corner == "tl":
            anchor = QPointF(base.right(), base.bottom())
            dx = anchor.x() - scene_pos.x()
            dy = anchor.y() - scene_pos.y()
            if abs(dx) / max(1.0, abs(dy)) > ratio:
                dx = abs(dy) * ratio
            else:
                dy = abs(dx) / ratio
            left = anchor.x() - dx
            top = anchor.y() - dy
        elif moving_corner == "tr":
            anchor = QPointF(base.left(), base.bottom())
            dx = scene_pos.x() - anchor.x()
            dy = anchor.y() - scene_pos.y()
            if abs(dx) / max(1.0, abs(dy)) > ratio:
                dx = abs(dy) * ratio
            else:
                dy = abs(dx) / ratio
            right = anchor.x() + dx
            top = anchor.y() - dy
        elif moving_corner == "bl":
            anchor = QPointF(base.right(), base.top())
            dx = anchor.x() - scene_pos.x()
            dy = scene_pos.y() - anchor.y()
            if abs(dx) / max(1.0, abs(dy)) > ratio:
                dx = abs(dy) * ratio
            else:
                dy = abs(dx) / ratio
            left = anchor.x() - dx
            bottom = anchor.y() + dy
        else:
            anchor = QPointF(base.left(), base.top())
            dx = scene_pos.x() - anchor.x()
            dy = scene_pos.y() - anchor.y()
            if abs(dx) / max(1.0, abs(dy)) > ratio:
                dx = abs(dy) * ratio
            else:
                dy = abs(dx) / ratio
            right = anchor.x() + dx
            bottom = anchor.y() + dy
        return QRectF(QPointF(left, top), QPointF(right, bottom)).normalized()

    def _update_crop_from_handle(self, scene_pos: QPointF):
        base = QRectF(self._crop_rect_at_drag)
        dx = scene_pos.x() - self._drag_origin_scene.x()
        dy = scene_pos.y() - self._drag_origin_scene.y()
        rect = QRectF(base)
        h = self._active_handle
        if h == "move":
            rect.translate(dx, dy)
            self._staged_crop_rect = self._clamp_crop(rect)
            return
        if h in {"tl", "tr", "bl", "br"} and self._crop_aspect_lock:
            self._staged_crop_rect = self._clamp_crop(self._apply_aspect_to_corner(base, h, scene_pos))
            return
        if h in {"tl", "tc", "tr"}:
            rect.setTop(base.top() + dy)
        if h in {"bl", "bc", "br"}:
            rect.setBottom(base.bottom() + dy)
        if h in {"tl", "lc", "bl"}:
            rect.setLeft(base.left() + dx)
        if h in {"tr", "rc", "br"}:
            rect.setRight(base.right() + dx)
        rect = rect.normalized()
        if self._crop_aspect_lock and h in {"tc", "bc", "lc", "rc"}:
            ratio = self._crop_aspect_ratio
            if h in {"tc", "bc"}:
                new_w = rect.height() * ratio
                cx = base.center().x()
                rect.setLeft(cx - new_w / 2)
                rect.setRight(cx + new_w / 2)
            else:
                new_h = rect.width() / ratio
                cy = base.center().y()
                rect.setTop(cy - new_h / 2)
                rect.setBottom(cy + new_h / 2)
        self._staged_crop_rect = self._clamp_crop(rect)

    def mousePressEvent(self, event):
        if self._crop_mode and event.button() == Qt.MouseButton.LeftButton and not self.pixmap_item.pixmap().isNull():
            scene_pos = self._scene_pos(event)
            bounds = QRectF(self.pixmap_item.pixmap().rect())
            if self._staged_crop_rect.isNull():
                self._staged_crop_rect = QRectF(bounds.center().x() - bounds.width() * 0.25, bounds.center().y() - bounds.height() * 0.25, bounds.width() * 0.5, bounds.height() * 0.5)
            handle = self._pick_handle(scene_pos)
            if handle is None and bounds.contains(scene_pos):
                self._staged_crop_rect = QRectF(scene_pos.x(), scene_pos.y(), 1, 1)
                handle = "br"
            if handle is not None:
                self._active_handle = handle
                self._drag_origin_scene = scene_pos
                self._crop_rect_at_drag = QRectF(self._staged_crop_rect)
                self.viewport().update()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._crop_mode and self._active_handle is not None:
            scene_pos = self._scene_pos(event)
            self._update_crop_from_handle(scene_pos)
            self.cropPreviewChanged.emit(self.current_crop_rect())
            self.viewport().update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._crop_mode and event.button() == Qt.MouseButton.LeftButton and self._active_handle is not None:
            self._active_handle = None
            self.viewport().update()
            return
        super().mouseReleaseEvent(event)

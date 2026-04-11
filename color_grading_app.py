import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, Signal, QThread, QTimer
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

APP_TITLE = "Professional Color Grading Studio"
SUPPORTED_INPUT = "Images (*.png *.jpg *.jpeg)"
SUPPORTED_EXPORT = "PNG (*.png);;JPEG (*.jpg *.jpeg)"
PROJECT_FILTER = "Color Grading Project (*.cgproj)"
PRESET_FILTER = "Color Grading Preset (*.cgpreset)"
FULL_IDLE_DELAY_MS = 220
RENDER_DEBOUNCE_MS = 40
HISTOGRAM_MAX_SIDE = 512
PREVIEW_RENDER_SCALE = 1.35
MIN_CROP_SIZE = 12


def pil_to_numpy_rgba(img: Image.Image) -> np.ndarray:
    return np.ascontiguousarray(np.array(img.convert("RGBA"), dtype=np.uint8))


def numpy_to_qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w, c = arr.shape
    if c == 4:
        return QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


def numpy_to_pixmap(arr: np.ndarray) -> QPixmap:
    return QPixmap.fromImage(numpy_to_qimage(arr))


def resize_rgba(arr: np.ndarray, size: Tuple[int, int], fast: bool = True) -> np.ndarray:
    w, h = size
    if arr.shape[1] == w and arr.shape[0] == h:
        return arr
    if HAS_CV2:
        interp = cv2.INTER_LINEAR if fast else cv2.INTER_LANCZOS4
        return np.ascontiguousarray(cv2.resize(arr, (w, h), interpolation=interp))
    pil = Image.fromarray(arr, mode="RGBA")
    resample = Image.Resampling.BILINEAR if fast else Image.Resampling.LANCZOS
    return np.ascontiguousarray(np.array(pil.resize((w, h), resample=resample), dtype=np.uint8))


def downscale_rgba(arr: np.ndarray, max_side: int) -> np.ndarray:
    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return arr
    scale = max_side / float(longest)
    return resize_rgba(arr, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))), fast=True)


def hash_array(arr: np.ndarray) -> int:
    sample = arr[:: max(1, arr.shape[0] // 64), :: max(1, arr.shape[1] // 64), :3]
    return int(sample.sum())


def fit_size_preserving_aspect(src_w: int, src_h: int, max_w: int, max_h: int) -> Tuple[int, int]:
    max_w = max(1, max_w)
    max_h = max(1, max_h)
    src_w = max(1, src_w)
    src_h = max(1, src_h)
    scale = min(max_w / float(src_w), max_h / float(src_h))
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def unique_path(path: str) -> str:
    directory, filename = os.path.split(path)
    if not directory:
        directory = os.getcwd()
    stem, ext = os.path.splitext(filename)
    if not os.path.exists(path):
        return path
    pattern = re.compile(rf"^{re.escape(stem)}\((\d+)\){re.escape(ext)}$")
    used = set()
    try:
        for name in os.listdir(directory):
            if name == f"{stem}{ext}":
                used.add(0)
                continue
            m = pattern.match(name)
            if m:
                used.add(int(m.group(1)))
    except Exception:
        used = {0}
    n = 1
    while n in used:
        n += 1
    return os.path.join(directory, f"{stem}({n}){ext}")


def choose_save_path(parent, title: str, suggested: str, file_filter: str) -> str:
    suggested_abs = os.path.abspath(suggested)
    suggested_dir = os.path.dirname(suggested_abs) or os.getcwd()
    suggested_name = os.path.basename(unique_path(suggested_abs))
    dialog = QFileDialog(parent, title, suggested_dir)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setNameFilter(file_filter)
    dialog.selectFile(suggested_name)
    dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
    if dialog.exec():
        files = dialog.selectedFiles()
        if files:
            return files[0]
    return ""


def build_curve_lut(points: List[Tuple[float, float]]) -> np.ndarray:
    pts = sorted([(float(np.clip(x, 0, 1)), float(np.clip(y, 0, 1))) for x, y in points], key=lambda p: p[0])
    if not pts:
        pts = [(0.0, 0.0), (1.0, 1.0)]
    if pts[0][0] > 0.0:
        pts.insert(0, (0.0, pts[0][1]))
    if pts[-1][0] < 1.0:
        pts.append((1.0, pts[-1][1]))
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    ys = np.array([p[1] for p in pts], dtype=np.float32)
    lut_x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    lut_y = np.interp(lut_x, xs, ys)
    return np.clip((lut_y * 255.0).round(), 0, 255).astype(np.uint8)


def compose_scalar_lut(brightness: float, contrast: float, gamma: float, exposure: float) -> np.ndarray:
    vals = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    vals *= (2.0 ** exposure)
    vals += brightness
    vals = (vals - 0.5) * (1.0 + contrast) + 0.5
    vals = np.power(np.clip(vals, 0.0, 1.0), 1.0 / max(0.05, gamma))
    return np.clip((vals * 255.0).round(), 0, 255).astype(np.uint8)


def histogram_from_rgba(arr: np.ndarray) -> Dict[str, np.ndarray]:
    src = downscale_rgba(arr, HISTOGRAM_MAX_SIDE)
    rgb = src[:, :, :3]
    return {
        "r": np.histogram(rgb[:, :, 0].reshape(-1), bins=256, range=(0, 255))[0],
        "g": np.histogram(rgb[:, :, 1].reshape(-1), bins=256, range=(0, 255))[0],
        "b": np.histogram(rgb[:, :, 2].reshape(-1), bins=256, range=(0, 255))[0],
    }


def transformed_size(width: int, height: int, rotation: int) -> Tuple[int, int]:
    return (height, width) if rotation % 180 != 0 else (width, height)


def transform_point_original_to_view(x: float, y: float, width: int, height: int, rotation: int, flip_h: bool, flip_v: bool) -> Tuple[float, float]:
    rot = rotation % 360
    if rot == 0:
        tx, ty = x, y
        tw, th = width, height
    elif rot == 90:
        tx, ty = height - y, x
        tw, th = height, width
    elif rot == 180:
        tx, ty = width - x, height - y
        tw, th = width, height
    else:
        tx, ty = y, width - x
        tw, th = height, width
    if flip_h:
        tx = tw - tx
    if flip_v:
        ty = th - ty
    return tx, ty


def inverse_transform_point_view_to_original(x: float, y: float, width: int, height: int, rotation: int, flip_h: bool, flip_v: bool) -> Tuple[float, float]:
    rot = rotation % 360
    tw, th = transformed_size(width, height, rot)
    if flip_h:
        x = tw - x
    if flip_v:
        y = th - y
    if rot == 0:
        return x, y
    if rot == 90:
        return y, height - x
    if rot == 180:
        return width - x, height - y
    return width - y, x


def rect_view_to_original(rect: QRect, width: int, height: int, rotation: int, flip_h: bool, flip_v: bool) -> QRect:
    corners = [
        (rect.x(), rect.y()),
        (rect.x() + rect.width(), rect.y()),
        (rect.x(), rect.y() + rect.height()),
        (rect.x() + rect.width(), rect.y() + rect.height()),
    ]
    pts = [inverse_transform_point_view_to_original(px, py, width, height, rotation, flip_h, flip_v) for px, py in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    left = int(round(min(xs)))
    top = int(round(min(ys)))
    right = int(round(max(xs)))
    bottom = int(round(max(ys)))
    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    right = max(left + 1, min(right, width))
    bottom = max(top + 1, min(bottom, height))
    return QRect(left, top, right - left, bottom - top)


def rect_original_to_view(rect: QRect, width: int, height: int, rotation: int, flip_h: bool, flip_v: bool) -> QRect:
    corners = [
        (rect.x(), rect.y()),
        (rect.x() + rect.width(), rect.y()),
        (rect.x(), rect.y() + rect.height()),
        (rect.x() + rect.width(), rect.y() + rect.height()),
    ]
    pts = [transform_point_original_to_view(px, py, width, height, rotation, flip_h, flip_v) for px, py in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    left = int(round(min(xs)))
    top = int(round(min(ys)))
    right = int(round(max(xs)))
    bottom = int(round(max(ys)))
    tw, th = transformed_size(width, height, rotation)
    left = max(0, min(left, tw - 1))
    top = max(0, min(top, th - 1))
    right = max(left + 1, min(right, tw))
    bottom = max(top + 1, min(bottom, th))
    return QRect(left, top, right - left, bottom - top)


@dataclass
class CurveSet:
    master: List[Tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    red: List[Tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    green: List[Tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    blue: List[Tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    def to_json(self):
        return {k: [[float(x), float(y)] for x, y in getattr(self, k)] for k in ("master", "red", "green", "blue")}
    @staticmethod
    def from_json(data: Dict) -> "CurveSet":
        cs = CurveSet()
        for k in ("master", "red", "green", "blue"):
            pts = data.get(k, [[0, 0], [1, 1]])
            setattr(cs, k, [(float(p[0]), float(p[1])) for p in pts])
        return cs


@dataclass
class ToneRGB:
    r: float = 0.0
    g: float = 0.0
    b: float = 0.0
    def to_json(self):
        return asdict(self)
    @staticmethod
    def from_json(data: Dict) -> "ToneRGB":
        return ToneRGB(float(data.get("r", 0.0)), float(data.get("g", 0.0)), float(data.get("b", 0.0)))


@dataclass
class CropRect:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    enabled: bool = False
    def to_json(self):
        return asdict(self)
    @staticmethod
    def from_json(data: Dict) -> "CropRect":
        return CropRect(int(data.get("x", 0)), int(data.get("y", 0)), int(data.get("w", 0)), int(data.get("h", 0)), bool(data.get("enabled", False)))


@dataclass
class ResizeState:
    width: int = 0
    height: int = 0
    enabled: bool = False
    def to_json(self):
        return asdict(self)
    @staticmethod
    def from_json(data: Dict) -> "ResizeState":
        return ResizeState(int(data.get("width", 0)), int(data.get("height", 0)), bool(data.get("enabled", False)))


@dataclass
class AdjustmentState:
    brightness: float = 0.0
    contrast: float = 0.0
    gamma: float = 1.0
    exposure: float = 0.0
    temperature: float = 0.0
    tint: float = 0.0
    white_balance_strength: float = 0.0
    red_intensity: float = 0.0
    green_intensity: float = 0.0
    blue_intensity: float = 0.0
    shadows: ToneRGB = field(default_factory=ToneRGB)
    midtones: ToneRGB = field(default_factory=ToneRGB)
    highlights: ToneRGB = field(default_factory=ToneRGB)
    rotation: int = 0
    flip_h: bool = False
    flip_v: bool = False
    crop: CropRect = field(default_factory=CropRect)
    resize: ResizeState = field(default_factory=ResizeState)
    curves: CurveSet = field(default_factory=CurveSet)
    def clone(self) -> "AdjustmentState":
        return copy.deepcopy(self)
    def to_json(self):
        return {
            "brightness": self.brightness,
            "contrast": self.contrast,
            "gamma": self.gamma,
            "exposure": self.exposure,
            "temperature": self.temperature,
            "tint": self.tint,
            "white_balance_strength": self.white_balance_strength,
            "red_intensity": self.red_intensity,
            "green_intensity": self.green_intensity,
            "blue_intensity": self.blue_intensity,
            "shadows": self.shadows.to_json(),
            "midtones": self.midtones.to_json(),
            "highlights": self.highlights.to_json(),
            "rotation": self.rotation,
            "flip_h": self.flip_h,
            "flip_v": self.flip_v,
            "crop": self.crop.to_json(),
            "resize": self.resize.to_json(),
            "curves": self.curves.to_json(),
        }
    @staticmethod
    def from_json(data: Dict) -> "AdjustmentState":
        st = AdjustmentState()
        for k in ("brightness", "contrast", "gamma", "exposure", "temperature", "tint", "white_balance_strength", "red_intensity", "green_intensity", "blue_intensity"):
            setattr(st, k, float(data.get(k, getattr(st, k))))
        st.shadows = ToneRGB.from_json(data.get("shadows", {}))
        st.midtones = ToneRGB.from_json(data.get("midtones", {}))
        st.highlights = ToneRGB.from_json(data.get("highlights", {}))
        st.rotation = int(data.get("rotation", 0)) % 360
        st.flip_h = bool(data.get("flip_h", False))
        st.flip_v = bool(data.get("flip_v", False))
        st.crop = CropRect.from_json(data.get("crop", {}))
        st.resize = ResizeState.from_json(data.get("resize", {}))
        st.curves = CurveSet.from_json(data.get("curves", {}))
        return st


class HistoryManager:
    def __init__(self):
        self.undo_stack: List[AdjustmentState] = []
        self.redo_stack: List[AdjustmentState] = []
    def clear(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
    def push(self, state: AdjustmentState):
        if self.undo_stack and self.undo_stack[-1].to_json() == state.to_json():
            return
        self.undo_stack.append(state.clone())
        self.redo_stack.clear()
    def can_undo(self) -> bool:
        return len(self.undo_stack) > 1
    def can_redo(self) -> bool:
        return len(self.redo_stack) > 0
    def undo(self, current: AdjustmentState) -> AdjustmentState:
        if not self.can_undo():
            return current
        self.redo_stack.append(self.undo_stack.pop())
        return self.undo_stack[-1].clone()
    def redo(self, current: AdjustmentState) -> AdjustmentState:
        if not self.can_redo():
            return current
        state = self.redo_stack.pop()
        self.undo_stack.append(state.clone())
        return state.clone()


class ImageProcessor:
    @staticmethod
    def apply_transform(img: np.ndarray, rotation: int, flip_h: bool, flip_v: bool) -> np.ndarray:
        out = img
        if rotation % 360 != 0:
            k = (rotation % 360) // 90
            out = np.ascontiguousarray(np.rot90(out, k=4 - k))
        if flip_h:
            out = np.ascontiguousarray(np.flip(out, axis=1))
        if flip_v:
            out = np.ascontiguousarray(np.flip(out, axis=0))
        return out

    @staticmethod
    def apply_crop(img: np.ndarray, crop: CropRect) -> np.ndarray:
        out = img
        if crop.enabled and crop.w > 1 and crop.h > 1:
            h, w = out.shape[:2]
            x = int(np.clip(crop.x, 0, max(0, w - 1)))
            y = int(np.clip(crop.y, 0, max(0, h - 1)))
            cw = int(np.clip(crop.w, 1, max(1, w - x)))
            ch = int(np.clip(crop.h, 1, max(1, h - y)))
            out = np.ascontiguousarray(out[y:y + ch, x:x + cw])
        return out

    @staticmethod
    def geometry_pre_resize(img: np.ndarray, state: AdjustmentState) -> np.ndarray:
        out = ImageProcessor.apply_transform(img, state.rotation, state.flip_h, state.flip_v)
        out = ImageProcessor.apply_crop(out, state.crop)
        return out

    @staticmethod
    def apply_resize(img: np.ndarray, resize_state: ResizeState, fast: bool) -> np.ndarray:
        if resize_state.enabled and resize_state.width > 1 and resize_state.height > 1:
            return resize_rgba(img, (resize_state.width, resize_state.height), fast=fast)
        return img

    @staticmethod
    def apply_color(img: np.ndarray, state: AdjustmentState, skip_tonal: bool = False) -> np.ndarray:
        rgba = img.copy()
        rgb = rgba[:, :, :3]
        scalar_lut = compose_scalar_lut(state.brightness, state.contrast, state.gamma, state.exposure)
        master_lut = build_curve_lut(state.curves.master)
        red_curve = build_curve_lut(state.curves.red)
        green_curve = build_curve_lut(state.curves.green)
        blue_curve = build_curve_lut(state.curves.blue)
        lut_r = red_curve[master_lut[scalar_lut]]
        lut_g = green_curve[master_lut[scalar_lut]]
        lut_b = blue_curve[master_lut[scalar_lut]]
        if HAS_CV2:
            rgb[:, :, 0] = cv2.LUT(rgb[:, :, 0], lut_r)
            rgb[:, :, 1] = cv2.LUT(rgb[:, :, 1], lut_g)
            rgb[:, :, 2] = cv2.LUT(rgb[:, :, 2], lut_b)
        else:
            rgb[:, :, 0] = lut_r[rgb[:, :, 0]]
            rgb[:, :, 1] = lut_g[rgb[:, :, 1]]
            rgb[:, :, 2] = lut_b[rgb[:, :, 2]]
        rgbf = rgb.astype(np.float32) / 255.0
        wb = np.array([
            1.0 + state.temperature * 0.35 + state.white_balance_strength * 0.15,
            1.0 + state.tint * 0.10,
            1.0 - state.temperature * 0.35 - state.white_balance_strength * 0.15,
        ], dtype=np.float32).reshape(1, 1, 3)
        ch = np.array([
            1.0 + state.red_intensity,
            1.0 + state.green_intensity,
            1.0 + state.blue_intensity,
        ], dtype=np.float32).reshape(1, 1, 3)
        rgbf *= wb
        rgbf *= ch
        if not skip_tonal:
            lum = 0.2126 * rgbf[:, :, 0] + 0.7152 * rgbf[:, :, 1] + 0.0722 * rgbf[:, :, 2]
            shadows_w = np.clip((0.45 - lum) / 0.45, 0.0, 1.0)[..., None]
            highlights_w = np.clip((lum - 0.55) / 0.45, 0.0, 1.0)[..., None]
            midtones_w = 1.0 - np.clip(shadows_w + highlights_w, 0.0, 1.0)
            rgbf += shadows_w * np.array([state.shadows.r, state.shadows.g, state.shadows.b], dtype=np.float32).reshape(1, 1, 3)
            rgbf += midtones_w * np.array([state.midtones.r, state.midtones.g, state.midtones.b], dtype=np.float32).reshape(1, 1, 3)
            rgbf += highlights_w * np.array([state.highlights.r, state.highlights.g, state.highlights.b], dtype=np.float32).reshape(1, 1, 3)
        rgba[:, :, :3] = np.clip(rgbf * 255.0, 0, 255).astype(np.uint8)
        return np.ascontiguousarray(rgba)


class RenderRequest:
    def __init__(self, generation: int, source: np.ndarray, state: AdjustmentState, display_size: Tuple[int, int], full_quality: bool, skip_tonal: bool):
        self.generation = generation
        self.source = source
        self.state = state
        self.display_size = display_size
        self.full_quality = full_quality
        self.skip_tonal = skip_tonal


class RenderWorker(QObject):
    resultReady = Signal(int, object)
    histogramReady = Signal(int, object)
    def __init__(self):
        super().__init__()
        self.pending: Optional[RenderRequest] = None
        self.busy = False
    def submit(self, request: RenderRequest):
        self.pending = request
        if not self.busy:
            self._process_next()
    def _process_next(self):
        if self.pending is None:
            self.busy = False
            return
        self.busy = True
        req = self.pending
        self.pending = None
        try:
            base = ImageProcessor.geometry_pre_resize(req.source, req.state)
            work = ImageProcessor.apply_resize(base, req.state.resize, fast=not req.full_quality)
            target = fit_size_preserving_aspect(work.shape[1], work.shape[0], req.display_size[0], req.display_size[1])
            if work.shape[1] != target[0] or work.shape[0] != target[1]:
                work = resize_rgba(work, target, fast=not req.full_quality)
            work = ImageProcessor.apply_color(work, req.state, skip_tonal=req.skip_tonal)
            self.resultReady.emit(req.generation, work)
            if req.full_quality:
                self.histogramReady.emit(req.generation, histogram_from_rgba(work))
        except Exception as e:
            self.resultReady.emit(req.generation, e)
        self._process_next()


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
        maxv = max(1, int(max(np.max(self.hist["r"]), np.max(self.hist["g"]), np.max(self.hist["b"]))))
        colors = {"r": QColor(255, 80, 80, 120), "g": QColor(80, 255, 80, 120), "b": QColor(80, 140, 255, 120)}
        for key in ("r", "g", "b"):
            path = QPainterPath()
            vals = self.hist[key].astype(np.float32) / maxv
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


class CurveEditor(QWidget):
    pointsChanged = Signal(list)
    dragFinished = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(300)
        self.setMinimumWidth(300)
        self.setMouseTracking(True)
        self._points = [(0.0, 0.0), (1.0, 1.0)]
        self._drag_index: Optional[int] = None
        self._channel = "master"
        self._colors = {"master": QColor(230, 230, 230), "red": QColor(220, 70, 70), "green": QColor(70, 220, 70), "blue": QColor(80, 120, 240)}
        self._hist = {"r": np.zeros(256), "g": np.zeros(256), "b": np.zeros(256)}
    def set_channel(self, channel: str):
        self._channel = channel
        self.update()
    def set_points(self, points):
        self._points = sorted(points, key=lambda p: p[0])
        self.update()
    def set_histogram(self, hist):
        self._hist = hist
        self.update()
    def _content_rect(self) -> QRectF:
        m = 20
        side = max(40.0, min(self.width() - 2 * m, self.height() - 2 * m))
        x = (self.width() - side) / 2.0
        y = (self.height() - side) / 2.0
        return QRectF(x, y, side, side)
    def _to_widget(self, p):
        r = self._content_rect()
        return QPointF(r.left() + p[0] * r.width(), r.bottom() - p[1] * r.height())
    def _to_normalized(self, pos):
        r = self._content_rect()
        return (float(np.clip((pos.x() - r.left()) / max(1.0, r.width()), 0, 1)), float(np.clip((r.bottom() - pos.y()) / max(1.0, r.height()), 0, 1)))
    def _find_handle(self, pos):
        for i, p in enumerate(self._points):
            if (self._to_widget(p) - pos).manhattanLength() <= 12:
                return i
        return None
    def mousePressEvent(self, event):
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
        if self._drag_index is not None:
            self._drag_index = None
            self.dragFinished.emit()
    def paintEvent(self, event):
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
        centers = {"tl": QPointF(crop.left(), crop.top()), "tc": QPointF(crop.center().x(), crop.top()), "tr": QPointF(crop.right(), crop.top()), "rc": QPointF(crop.right(), crop.center().y()), "br": QPointF(crop.right(), crop.bottom()), "bc": QPointF(crop.center().x(), crop.bottom()), "bl": QPointF(crop.left(), crop.bottom()), "lc": QPointF(crop.left(), crop.center().y()), "move": crop.center()}
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1600, 950)
        self.original_rgba: Optional[np.ndarray] = None
        self.current_path: Optional[str] = None
        self.current_project_path: Optional[str] = None
        self.preview_rgba: Optional[np.ndarray] = None
        self.state = AdjustmentState()
        self.history = HistoryManager()
        self.history.push(self.state)
        self.current_curve_channel = "master"
        self.preview_original_while_held = False
        self._building_ui = False
        self._display_pixmap_key = None
        self._render_generation = 0
        self._pending_fit = False
        self._interactive_drag = False
        self._latest_histogram = {"r": np.zeros(256), "g": np.zeros(256), "b": np.zeros(256)}
        self._pending_fast_render: Optional[Tuple[AdjustmentState, bool]] = None
        self._is_dirty = False

        self._full_timer = QTimer(self)
        self._full_timer.setSingleShot(True)
        self._full_timer.timeout.connect(self.request_full_render)
        self._fast_debounce = QTimer(self)
        self._fast_debounce.setSingleShot(True)
        self._fast_debounce.timeout.connect(self.flush_fast_render_request)

        self.worker_thread = QThread(self)
        self.worker = RenderWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker.resultReady.connect(self.on_render_result)
        self.worker.histogramReady.connect(self.on_histogram_result)
        self.worker_thread.start()

        self.build_ui()
        self.apply_styles()
        self.refresh_actions()

    def closeEvent(self, event):
        if not self.confirm_discard_unsaved():
            event.ignore()
            return
        self.worker_thread.quit()
        self.worker_thread.wait(1000)
        super().closeEvent(event)

    def build_ui(self):
        self._building_ui = True
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter)
        self.controls_tabs = QTabWidget()
        self.controls_tabs.setMinimumWidth(430)
        splitter.addWidget(self.controls_tabs)
        self.viewer = ImageView()
        self.viewer.cropPreviewChanged.connect(self.on_crop_preview_changed)
        self.viewer.imageDropped.connect(self.load_image)
        splitter.addWidget(self.viewer)
        splitter.setStretchFactor(1, 1)
        self.create_actions()
        self.create_toolbar()
        self.controls_tabs.addTab(self.build_adjust_tab(), "Adjustments")
        self.controls_tabs.addTab(self.build_curve_tab(), "Curves")
        self.controls_tabs.addTab(self.build_histogram_tab(), "Histogram")
        self.controls_tabs.addTab(self.build_transform_tab(), "Transform")
        self.statusBar().showMessage("Open an image to begin")
        self._building_ui = False

    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1c1c1f; color: #ececf1; }
            QGroupBox { border: 1px solid #383840; border-radius: 10px; margin-top: 10px; padding-top: 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
            QPushButton { background: #2d2d33; border: 1px solid #404049; border-radius: 8px; padding: 8px 12px; }
            QPushButton:hover { background: #383842; }
            QSlider::groove:horizontal { border-radius: 4px; height: 8px; background: #34343a; }
            QSlider::handle:horizontal { background: #dcdce2; width: 16px; margin: -5px 0; border-radius: 8px; }
            QTabWidget::pane { border: 1px solid #35353d; border-radius: 10px; }
            QTabBar::tab { background: #26262b; padding: 10px 14px; border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 2px; }
            QTabBar::tab:selected { background: #34343c; }
        """)

    def build_adjust_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        self.controls = {}
        basic = QGroupBox("Basic grading")
        bl = QVBoxLayout(basic)
        self.controls.update(self.make_slider_group(bl, [
            ("brightness", "Brightness", -100, 100, 0),
            ("contrast", "Contrast", -100, 100, 0),
            ("gamma", "Gamma", 10, 300, 100),
            ("exposure", "Exposure", -300, 300, 0),
            ("temperature", "Temperature", -100, 100, 0),
            ("tint", "Tint", -100, 100, 0),
            ("white_balance_strength", "White balance", -100, 100, 0),
        ]))
        layout.addWidget(basic)
        ch = QGroupBox("Channel intensity")
        chl = QVBoxLayout(ch)
        self.controls.update(self.make_slider_group(chl, [
            ("red_intensity", "Red", -100, 100, 0),
            ("green_intensity", "Green", -100, 100, 0),
            ("blue_intensity", "Blue", -100, 100, 0),
        ]))
        row = QHBoxLayout()
        for label, channel in [("Reset Red", "red"), ("Reset Green", "green"), ("Reset Blue", "blue")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, chn=channel: self.reset_channel(chn))
            row.addWidget(btn)
        chl.addLayout(row)
        layout.addWidget(ch)
        layout.addWidget(self.make_tone_group("Shadows", "shadows"))
        layout.addWidget(self.make_tone_group("Midtones", "midtones"))
        layout.addWidget(self.make_tone_group("Highlights", "highlights"))
        reset_box = QGroupBox("Reset")
        rl = QHBoxLayout(reset_box)
        btn_all = QPushButton("Reset All")
        btn_tab = QPushButton("Reset Current Tab")
        btn_all.clicked.connect(self.reset_all)
        btn_tab.clicked.connect(self.reset_current_tab)
        rl.addWidget(btn_all)
        rl.addWidget(btn_tab)
        layout.addWidget(reset_box)
        layout.addStretch(1)
        return scroll

    def build_curve_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        row = QHBoxLayout()
        self.curve_channel_combo = QComboBox()
        self.curve_channel_combo.addItems(["master", "red", "green", "blue"])
        self.curve_channel_combo.currentTextChanged.connect(self.on_curve_channel_changed)
        row.addWidget(QLabel("Channel"))
        row.addWidget(self.curve_channel_combo)
        btn = QPushButton("Reset Current Curve")
        btn.clicked.connect(self.reset_current_curve)
        row.addWidget(btn)
        l.addLayout(row)
        self.curve_editor = CurveEditor()
        self.curve_editor.pointsChanged.connect(self.on_curve_points_changed)
        self.curve_editor.dragFinished.connect(self.finalize_interaction)
        l.addWidget(self.curve_editor)
        self.sync_curve_editor_from_state()
        return w

    def build_histogram_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        self.histogram_widget = HistogramWidget()
        l.addWidget(self.histogram_widget)
        txt = "Histogram updates after the idle/full-quality render and is shown inside the curve editor."
        if HAS_CV2:
            txt += " OpenCV is used for faster preview operations."
        l.addWidget(QLabel(txt))
        l.addStretch(1)
        return w

    def build_transform_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        nav = QGroupBox("Navigation")
        nl = QHBoxLayout(nav)
        for text, cb in [("Zoom In (+)", self.viewer.zoom_in), ("Zoom Out (-)", self.viewer.zoom_out), ("Fit to Window", self.viewer.fit_image)]:
            b = QPushButton(text)
            b.clicked.connect(cb)
            nl.addWidget(b)
        layout.addWidget(nav)
        transform = QGroupBox("Transform")
        tl = QGridLayout(transform)
        items = [
            ("Rotate Left", lambda: self.rotate_image(-90), 0, 0),
            ("Rotate Right", lambda: self.rotate_image(90), 0, 1),
            ("Flip Horizontal", lambda: self.toggle_flip("h"), 1, 0),
            ("Flip Vertical", lambda: self.toggle_flip("v"), 1, 1),
        ]
        for txt, cb, r, c in items:
            b = QPushButton(txt)
            b.clicked.connect(cb)
            tl.addWidget(b, r, c)
        layout.addWidget(transform)
        crop = QGroupBox("Crop")
        cl = QVBoxLayout(crop)
        self.crop_mode_check = QCheckBox("Enable crop mode and drag handles on the image")
        self.crop_mode_check.toggled.connect(self.viewer.set_crop_mode)
        self.crop_lock_check = QCheckBox("Lock crop aspect ratio")
        self.crop_ratio_combo = QComboBox()
        self.crop_ratio_combo.addItems(["Original", "1:1", "4:3", "3:2", "16:9", "21:9"])
        self.crop_lock_check.toggled.connect(self.update_crop_lock)
        self.crop_ratio_combo.currentTextChanged.connect(self.update_crop_lock)
        apply_crop = QPushButton("Apply Crop")
        cancel_crop = QPushButton("Cancel Crop Edit")
        clear_crop = QPushButton("Clear Crop")
        apply_crop.clicked.connect(self.apply_crop_from_view)
        cancel_crop.clicked.connect(self.cancel_crop_edit)
        clear_crop.clicked.connect(self.clear_crop)
        cl.addWidget(self.crop_mode_check)
        cl.addWidget(self.crop_lock_check)
        cl.addWidget(self.crop_ratio_combo)
        cl.addWidget(QLabel("Drag the corners, edges, or center of the crop box directly on the image. Use Apply Crop to commit."))
        cl.addWidget(apply_crop)
        cl.addWidget(cancel_crop)
        cl.addWidget(clear_crop)
        layout.addWidget(crop)
        resize_box = QGroupBox("Resize")
        rl = QFormLayout(resize_box)
        self.resize_w_slider, self.resize_w_label = self.make_single_slider(1, 12000, 1920)
        self.resize_h_slider, self.resize_h_label = self.make_single_slider(1, 12000, 1080)
        self.resize_enable = QCheckBox("Enable resize in pipeline")
        self.resize_lock_check = QCheckBox("Lock aspect ratio")
        self.resize_w_slider.valueChanged.connect(lambda: self.on_resize_dimension_changed("w"))
        self.resize_h_slider.valueChanged.connect(lambda: self.on_resize_dimension_changed("h"))
        self.resize_enable.toggled.connect(self.on_resize_controls_changed)
        rl.addRow("Width", self.wrap_slider_row(self.resize_w_slider, self.resize_w_label))
        rl.addRow("Height", self.wrap_slider_row(self.resize_h_slider, self.resize_h_label))
        rl.addRow("Apply", self.resize_enable)
        rl.addRow("Lock", self.resize_lock_check)
        rb = QPushButton("Reset Resize")
        rb.clicked.connect(self.reset_resize)
        rl.addRow(rb)
        layout.addWidget(resize_box)
        layout.addStretch(1)
        return w

    def create_actions(self):
        pairs = [
            ("Open", "Ctrl+O", self.open_image, "act_open"),
            ("Export", "Ctrl+S", self.export_image, "act_export"),
            ("Reset", "Ctrl+R", self.reset_all, "act_reset"),
            ("Undo", "Ctrl+Z", self.undo, "act_undo"),
            ("Redo", "Ctrl+Y", self.redo, "act_redo"),
            ("Zoom In", "+", self.viewer.zoom_in, "act_zoom_in"),
            ("Zoom Out", "-", self.viewer.zoom_out, "act_zoom_out"),
        ]
        for text, shortcut, cb, attr in pairs:
            act = QAction(text, self)
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(cb)
            setattr(self, attr, act)
            self.addAction(act)
        self.act_toggle_compare = QAction("Toggle Before/After", self)
        self.act_toggle_compare.setShortcut(QKeySequence(Qt.Key.Key_Space))
        self.act_toggle_compare.triggered.connect(self.toggle_before_after)
        self.addAction(self.act_toggle_compare)
        self.act_save_project = QAction("Save Project", self)
        self.act_save_project.triggered.connect(self.save_project)
        self.act_load_project = QAction("Load Project", self)
        self.act_load_project.triggered.connect(self.load_project)
        self.act_save_preset = QAction("Save Preset", self)
        self.act_save_preset.triggered.connect(self.save_preset)
        self.act_load_preset = QAction("Load Preset", self)
        self.act_load_preset.triggered.connect(self.load_preset)

    def create_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        for act in [self.act_open, self.act_export, self.act_undo, self.act_redo, self.act_reset]:
            tb.addAction(act)
        tb.addSeparator()
        for act in [self.act_save_project, self.act_load_project, self.act_save_preset, self.act_load_preset]:
            tb.addAction(act)
        tb.addSeparator()
        fit_act = QAction("Fit", self)
        fit_act.triggered.connect(self.viewer.fit_image)
        tb.addAction(fit_act)
        cmp_act = QAction("Before/After", self)
        cmp_act.triggered.connect(self.toggle_before_after)
        tb.addAction(cmp_act)

    def make_single_slider(self, mn: int, mx: int, default: int):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(mn, mx)
        slider.setValue(default)
        label = QLabel(str(default))
        label.setMinimumWidth(60)
        slider.valueChanged.connect(lambda v, lab=label: lab.setText(str(v)))
        return slider, label

    def wrap_slider_row(self, slider: QSlider, label: QLabel):
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(slider, 1)
        l.addWidget(label)
        return w

    def make_slider_group(self, parent_layout, spec_list):
        out = {}
        for key, title, mn, mx, default in spec_list:
            slider, label = self.make_single_slider(mn, mx, default)
            slider.valueChanged.connect(lambda _, k=key: self.on_slider_changed(k))
            parent_layout.addWidget(QLabel(title))
            parent_layout.addWidget(self.wrap_slider_row(slider, label))
            out[key] = (slider, label)
        return out

    def make_tone_group(self, title: str, prefix: str):
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        for channel in ("r", "g", "b"):
            key = f"{prefix}_{channel}"
            slider, label = self.make_single_slider(-100, 100, 0)
            slider.valueChanged.connect(lambda _, k=key: self.on_tone_slider_changed(k))
            layout.addWidget(QLabel(channel.upper()))
            layout.addWidget(self.wrap_slider_row(slider, label))
            self.controls[key] = (slider, label)
        b = QPushButton(f"Reset {title}")
        b.clicked.connect(lambda: self.reset_tone_group(prefix))
        layout.addWidget(b)
        return box

    def get_pre_resize_image(self, state: Optional[AdjustmentState] = None) -> Optional[np.ndarray]:
        if self.original_rgba is None:
            return None
        st = state or self.state
        return ImageProcessor.geometry_pre_resize(self.original_rgba, st)

    def get_display_target_size(self) -> Tuple[int, int]:
        pre = self.get_pre_resize_image()
        if pre is None:
            return (800, 600)
        w, h = pre.shape[1], pre.shape[0]
        if self.state.resize.enabled and self.state.resize.width > 1 and self.state.resize.height > 1:
            w, h = self.state.resize.width, self.state.resize.height
        if self.viewer.viewport().width() <= 1 or self.viewer.viewport().height() <= 1:
            return (w, h)
        vp = self.viewer.viewport().size()
        return fit_size_preserving_aspect(w, h, int(round((vp.width() - 8) * PREVIEW_RENDER_SCALE)), int(round((vp.height() - 8) * PREVIEW_RENDER_SCALE)))

    def get_crop_reference_size(self) -> Tuple[int, int]:
        if self.original_rgba is None:
            return (800, 600)
        transformed = ImageProcessor.apply_transform(self.original_rgba, self.state.rotation, self.state.flip_h, self.state.flip_v)
        return transformed.shape[1], transformed.shape[0]

    def get_current_crop_ratio(self):
        text = self.crop_ratio_combo.currentText()
        if text == "Original" and self.original_rgba is not None:
            ref_w, ref_h = self.get_crop_reference_size()
            return ref_w / max(1, ref_h)
        if ":" in text:
            a, b = text.split(":", 1)
            return float(a) / max(0.01, float(b))
        return 1.0

    def update_crop_lock(self):
        self.viewer.set_crop_lock(self.crop_lock_check.isChecked(), self.get_current_crop_ratio())
        if self.crop_mode_check.isChecked():
            self.refresh_staged_crop_aspect()

    def refresh_staged_crop_aspect(self):
        rect = self.viewer.current_crop_rect()
        if rect.width() <= 1 or rect.height() <= 1 or not self.crop_lock_check.isChecked():
            return
        ratio = self.get_current_crop_ratio()
        cx = rect.center().x()
        cy = rect.center().y()
        w = rect.width()
        h = rect.height()
        if w / max(1, h) > ratio:
            w = int(round(h * ratio))
        else:
            h = int(round(w / ratio))
        self.viewer.set_crop_rect(QRect(int(round(cx - w / 2)), int(round(cy - h / 2)), max(MIN_CROP_SIZE, w), max(MIN_CROP_SIZE, h)))

    def geometry_rect_to_display_rect(self, rect: QRect) -> QRect:
        ref_w, ref_h = self.get_crop_reference_size()
        disp_w, disp_h = fit_size_preserving_aspect(ref_w, ref_h, *self.get_display_target_size())
        sx = disp_w / max(1, ref_w)
        sy = disp_h / max(1, ref_h)
        return QRect(int(round(rect.x() * sx)), int(round(rect.y() * sy)), max(1, int(round(rect.width() * sx))), max(1, int(round(rect.height() * sy))))

    def display_rect_to_geometry_rect(self, rect: QRect) -> QRect:
        ref_w, ref_h = self.get_crop_reference_size()
        disp_w, disp_h = fit_size_preserving_aspect(ref_w, ref_h, *self.get_display_target_size())
        sx = ref_w / max(1, disp_w)
        sy = ref_h / max(1, disp_h)
        x = int(round(rect.x() * sx))
        y = int(round(rect.y() * sy))
        w = int(round(rect.width() * sx))
        h = int(round(rect.height() * sy))
        x = max(0, min(x, ref_w - 1))
        y = max(0, min(y, ref_h - 1))
        w = max(1, min(w, ref_w - x))
        h = max(1, min(h, ref_h - y))
        return QRect(x, y, w, h)

    def sync_viewer_crop_rect(self):
        if self.original_rgba is None:
            self.viewer.clear_crop_rect()
            return
        ref_w, ref_h = self.get_crop_reference_size()
        disp_w, disp_h = fit_size_preserving_aspect(ref_w, ref_h, *self.get_display_target_size())
        if self.state.crop.enabled and self.state.crop.w > 1 and self.state.crop.h > 1:
            self.viewer.set_crop_rect(self.geometry_rect_to_display_rect(QRect(self.state.crop.x, self.state.crop.y, self.state.crop.w, self.state.crop.h)))
        else:
            self.viewer.set_crop_rect(QRect(int(disp_w * 0.15), int(disp_h * 0.15), int(disp_w * 0.7), int(disp_h * 0.7)))
        if self.crop_mode_check.isChecked():
            self.refresh_staged_crop_aspect()

    def request_fast_render(self, skip_tonal: bool):
        if self.original_rgba is None:
            return
        self._pending_fast_render = (self.state.clone(), skip_tonal)
        self._fast_debounce.start(RENDER_DEBOUNCE_MS)

    def flush_fast_render_request(self):
        if self.original_rgba is None or self._pending_fast_render is None:
            return
        state, skip_tonal = self._pending_fast_render
        self._pending_fast_render = None
        self._render_generation += 1
        self.worker.submit(RenderRequest(self._render_generation, self.original_rgba, state, self.get_display_target_size(), False, skip_tonal))

    def request_full_render(self):
        if self.original_rgba is None:
            return
        self._render_generation += 1
        self.worker.submit(RenderRequest(self._render_generation, self.original_rgba, self.state.clone(), self.get_display_target_size(), True, False))

    def update_viewer_pixmap(self, show_original: bool = False):
        arr = self.original_rgba if show_original else self.preview_rgba
        if arr is None:
            return
        key = f"{'orig' if show_original else 'edit'}:{arr.shape}:{hash_array(arr)}"
        if key == self._display_pixmap_key:
            return
        self._display_pixmap_key = key
        self.viewer.set_image(numpy_to_pixmap(arr))
        if self._pending_fit:
            self.viewer.fit_image()
            self._pending_fit = False

    def apply_histogram_to_widgets(self, hist):
        self._latest_histogram = hist
        self.histogram_widget.set_histogram(hist)
        self.curve_editor.set_histogram(hist)

    def confirm_discard_unsaved(self) -> bool:
        if not self._is_dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved project")
        box.setText("You have unsaved changes.")
        box.setInformativeText("Do you want to save your project before exiting?")
        save_btn = box.addButton("Save Project", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == save_btn:
            self.save_project()
            return not self._is_dirty
        if clicked == discard_btn:
            return True
        return False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.original_rgba is not None:
            self.request_fast_render(skip_tonal=self._interactive_drag)
            self._full_timer.start(FULL_IDLE_DELAY_MS)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not self.preview_original_while_held:
            self.preview_original_while_held = True
            self.update_viewer_pixmap(show_original=True)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and self.preview_original_while_held:
            self.preview_original_while_held = False
            self.update_viewer_pixmap(show_original=False)
            return
        super().keyReleaseEvent(event)

    def on_render_result(self, generation: int, payload):
        if generation != self._render_generation:
            return
        if isinstance(payload, Exception):
            self.statusBar().showMessage(f"Render error: {payload}")
            return
        self.preview_rgba = payload
        self.update_viewer_pixmap(show_original=self.preview_original_while_held)
        self.sync_viewer_crop_rect()

    def on_histogram_result(self, generation: int, hist):
        if generation != self._render_generation:
            return
        self.apply_histogram_to_widgets(hist)

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Image", "", SUPPORTED_INPUT)
        if path:
            self.load_image(path)

    def load_image(self, path: str):
        try:
            img = Image.open(path)
            self.original_rgba = pil_to_numpy_rgba(img)
            self.current_path = path
            self.current_project_path = None
            self.state = AdjustmentState()
            self.history.clear()
            self.history.push(self.state)
            self.preview_rgba = self.original_rgba.copy()
            self._display_pixmap_key = None
            h, w = self.original_rgba.shape[:2]
            self.resize_w_slider.setValue(w)
            self.resize_h_slider.setValue(h)
            self.resize_enable.setChecked(False)
            self.resize_lock_check.setChecked(False)
            self.sync_controls_from_state()
            self.update_crop_lock()
            self.sync_viewer_crop_rect()
            self.apply_histogram_to_widgets(histogram_from_rgba(self.preview_rgba))
            self._pending_fit = True
            self._is_dirty = False
            self.request_fast_render(skip_tonal=False)
            self._full_timer.start(FULL_IDLE_DELAY_MS)
            self.statusBar().showMessage(f"Loaded: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e))

    def export_image(self):
        if self.original_rgba is None:
            return
        path = choose_save_path(self, "Export Image", "edited_image.png", SUPPORTED_EXPORT)
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if not ext:
            ext = ".png"
            path += ext
        path = unique_path(os.path.abspath(path))
        work = self.get_pre_resize_image(self.state)
        work = ImageProcessor.apply_resize(work, self.state.resize, fast=False)
        export_arr = ImageProcessor.apply_color(work, self.state, skip_tonal=False)
        try:
            if ext in (".jpg", ".jpeg"):
                if np.any(export_arr[:, :, 3] < 255):
                    QMessageBox.warning(self, "Transparency warning", "Exporting to JPEG will remove transparency.")
                QMessageBox.warning(self, "JPEG quality warning", "JPEG export uses lossy compression and may reduce image quality.")
                Image.fromarray(export_arr[:, :, :3], mode="RGB").save(path, quality=95)
            else:
                Image.fromarray(export_arr, mode="RGBA").save(path)
            self.statusBar().showMessage(f"Exported: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def save_preset(self):
        path = choose_save_path(self, "Save Preset", "preset.cgpreset", PRESET_FILTER)
        if path:
            if not os.path.splitext(path)[1]:
                path += ".cgpreset"
            path = unique_path(os.path.abspath(path))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"adjustments": self.state.to_json()}, f, indent=2)
            self._is_dirty = False
            self.statusBar().showMessage(f"Preset saved: {path}")

    def load_preset(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Preset", "", PRESET_FILTER)
        if path:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.commit_state(AdjustmentState.from_json(data.get("adjustments", {})), push_history=True)
            self._is_dirty = False
            self.statusBar().showMessage(f"Preset loaded: {path}")

    def save_project(self):
        if self.original_rgba is None:
            return
        suggested = self.current_project_path or "project.cgproj"
        path = choose_save_path(self, "Save Project", suggested, PROJECT_FILTER)
        if path:
            if not os.path.splitext(path)[1]:
                path += ".cgproj"
            abs_path = os.path.abspath(path)
            current_abs = os.path.abspath(self.current_project_path) if self.current_project_path else None
            path = unique_path(abs_path) if (current_abs is None or abs_path != current_abs) else abs_path
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"image_path": self.current_path, "adjustments": self.state.to_json()}, f, indent=2)
            self.current_project_path = path
            self._is_dirty = False
            self.statusBar().showMessage(f"Project saved: {path}")

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Project", "", PROJECT_FILTER)
        if path:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            img_path = data.get("image_path")
            if not img_path or not os.path.exists(img_path):
                QMessageBox.warning(self, "Missing image", "The source image path stored in this project could not be found.")
                return
            self.load_image(img_path)
            self.commit_state(AdjustmentState.from_json(data.get("adjustments", {})), push_history=True)
            self.current_project_path = path
            self._is_dirty = False
            self.statusBar().showMessage(f"Project loaded: {path}")

    def commit_state(self, state: AdjustmentState, push_history: bool = False):
        self.state = state
        self._is_dirty = True
        self.sync_controls_from_state()
        self.sync_viewer_crop_rect()
        self.request_fast_render(skip_tonal=self._interactive_drag)
        self._full_timer.start(FULL_IDLE_DELAY_MS)
        if push_history:
            self.history.push(self.state)
        self.refresh_actions()

    def finalize_interaction(self):
        self._interactive_drag = False
        self.history.push(self.state)
        self.sync_viewer_crop_rect()
        self.request_fast_render(skip_tonal=False)
        self._full_timer.start(FULL_IDLE_DELAY_MS)
        self.refresh_actions()

    def sync_controls_from_state(self):
        self._building_ui = True
        mappings = [("brightness", self.state.brightness), ("contrast", self.state.contrast), ("gamma", self.state.gamma), ("exposure", self.state.exposure), ("temperature", self.state.temperature), ("tint", self.state.tint), ("white_balance_strength", self.state.white_balance_strength), ("red_intensity", self.state.red_intensity), ("green_intensity", self.state.green_intensity), ("blue_intensity", self.state.blue_intensity)]
        for key, val in mappings:
            self.controls[key][0].setValue(int(round(val * 100)))
        for prefix in ("shadows", "midtones", "highlights"):
            tone = getattr(self.state, prefix)
            for ch in ("r", "g", "b"):
                self.controls[f"{prefix}_{ch}"][0].setValue(int(round(getattr(tone, ch) * 100)))
        pre = self.get_pre_resize_image(self.state)
        if pre is not None:
            base_h, base_w = pre.shape[:2]
        else:
            base_w, base_h = 1920, 1080
        target_w = self.state.resize.width if self.state.resize.enabled and self.state.resize.width > 0 else base_w
        target_h = self.state.resize.height if self.state.resize.enabled and self.state.resize.height > 0 else base_h
        self.resize_w_slider.setValue(max(1, target_w))
        self.resize_h_slider.setValue(max(1, target_h))
        self.resize_enable.setChecked(self.state.resize.enabled)
        self.sync_curve_editor_from_state()
        self.update_crop_lock()
        self._building_ui = False

    def sync_curve_editor_from_state(self):
        self.curve_editor.set_channel(self.current_curve_channel)
        self.curve_editor.set_points(getattr(self.state.curves, self.current_curve_channel))
        self.curve_editor.set_histogram(self._latest_histogram)

    def refresh_actions(self):
        self.act_export.setEnabled(self.original_rgba is not None)
        self.act_undo.setEnabled(self.history.can_undo())
        self.act_redo.setEnabled(self.history.can_redo())

    def on_slider_changed(self, key: str):
        if self._building_ui:
            return
        self._interactive_drag = True
        val = self.controls[key][0].value()
        st = self.state.clone()
        setattr(st, key, max(0.1, val / 100.0) if key == "gamma" else val / 100.0)
        self.commit_state(st, push_history=False)

    def on_tone_slider_changed(self, key: str):
        if self._building_ui:
            return
        self._interactive_drag = True
        value = self.controls[key][0].value() / 100.0
        st = self.state.clone()
        prefix, channel = key.split("_")
        setattr(getattr(st, prefix), channel, value)
        self.commit_state(st, push_history=False)

    def on_curve_channel_changed(self, channel: str):
        self.current_curve_channel = channel
        self.sync_curve_editor_from_state()

    def on_curve_points_changed(self, points):
        self._interactive_drag = True
        st = self.state.clone()
        setattr(st.curves, self.current_curve_channel, points)
        self.commit_state(st, push_history=False)

    def on_crop_preview_changed(self, rect: QRect):
        self.statusBar().showMessage(f"Crop preview: {rect.width()} × {rect.height()}")

    def apply_crop_from_view(self):
        if self.original_rgba is None:
            return
        geom_rect = self.display_rect_to_geometry_rect(self.viewer.current_crop_rect())
        st = self.state.clone()
        st.crop = CropRect(geom_rect.x(), geom_rect.y(), geom_rect.width(), geom_rect.height(), True)
        self.commit_state(st, push_history=True)
        self.crop_mode_check.setChecked(False)

    def cancel_crop_edit(self):
        self.viewer.revert_staged_crop()
        self.crop_mode_check.setChecked(False)

    def on_resize_dimension_changed(self, changed_axis: str):
        if self._building_ui or self.original_rgba is None:
            return
        self._interactive_drag = True
        base = self.get_pre_resize_image(self.state)
        if base is None:
            return
        base_h, base_w = base.shape[:2]
        if self.resize_lock_check.isChecked():
            self._building_ui = True
            ratio = base_w / max(1, base_h)
            if changed_axis == "w":
                new_w = max(1, int(self.resize_w_slider.value()))
                new_h = max(1, int(round(new_w / ratio)))
                self.resize_h_slider.setValue(min(self.resize_h_slider.maximum(), new_h))
            else:
                new_h = max(1, int(self.resize_h_slider.value()))
                new_w = max(1, int(round(new_h * ratio)))
                self.resize_w_slider.setValue(min(self.resize_w_slider.maximum(), new_w))
            self._building_ui = False
        self.on_resize_controls_changed()

    def on_resize_controls_changed(self):
        if self._building_ui:
            return
        st = self.state.clone()
        st.resize.enabled = self.resize_enable.isChecked()
        st.resize.width = max(1, int(self.resize_w_slider.value()))
        st.resize.height = max(1, int(self.resize_h_slider.value()))
        self.commit_state(st, push_history=False)

    def remap_crop_for_new_transform(self, old_state: AdjustmentState, new_state: AdjustmentState):
        if self.original_rgba is None:
            return
        if not old_state.crop.enabled or old_state.crop.w <= 1 or old_state.crop.h <= 1:
            return
        orig_h, orig_w = self.original_rgba.shape[:2]
        old_crop_view = QRect(old_state.crop.x, old_state.crop.y, old_state.crop.w, old_state.crop.h)
        crop_in_original = rect_view_to_original(old_crop_view, orig_w, orig_h, old_state.rotation, old_state.flip_h, old_state.flip_v)
        new_crop_view = rect_original_to_view(crop_in_original, orig_w, orig_h, new_state.rotation, new_state.flip_h, new_state.flip_v)
        new_state.crop = CropRect(new_crop_view.x(), new_crop_view.y(), new_crop_view.width(), new_crop_view.height(), True)

    def undo(self):
        self.finalize_interaction()
        self.state = self.history.undo(self.state)
        self.sync_controls_from_state()
        self.sync_viewer_crop_rect()
        self.request_fast_render(skip_tonal=False)
        self._full_timer.start(FULL_IDLE_DELAY_MS)
        self.refresh_actions()

    def redo(self):
        self.state = self.history.redo(self.state)
        self.sync_controls_from_state()
        self.sync_viewer_crop_rect()
        self.request_fast_render(skip_tonal=False)
        self._full_timer.start(FULL_IDLE_DELAY_MS)
        self.refresh_actions()

    def reset_all(self):
        if self.original_rgba is not None:
            self.commit_state(AdjustmentState(), push_history=True)

    def reset_current_tab(self):
        tab = self.controls_tabs.tabText(self.controls_tabs.currentIndex())
        st = self.state.clone()
        if tab == "Adjustments":
            keep = (st.rotation, st.flip_h, st.flip_v, st.crop, st.resize, st.curves)
            st = AdjustmentState()
            st.rotation, st.flip_h, st.flip_v, st.crop, st.resize, st.curves = keep
        elif tab == "Curves":
            st.curves = CurveSet()
        elif tab == "Transform":
            st.rotation = 0
            st.flip_h = False
            st.flip_v = False
            st.crop = CropRect()
            st.resize = ResizeState()
        self.commit_state(st, push_history=True)

    def reset_channel(self, channel: str):
        st = self.state.clone()
        setattr(st, f"{channel}_intensity", 0.0)
        self.commit_state(st, push_history=True)

    def reset_tone_group(self, prefix: str):
        st = self.state.clone()
        setattr(st, prefix, ToneRGB())
        self.commit_state(st, push_history=True)

    def reset_current_curve(self):
        st = self.state.clone()
        setattr(st.curves, self.current_curve_channel, [(0.0, 0.0), (1.0, 1.0)])
        self.commit_state(st, push_history=True)

    def clear_crop(self):
        st = self.state.clone()
        st.crop = CropRect()
        self.commit_state(st, push_history=True)
        self.viewer.clear_crop_rect()

    def reset_resize(self):
        st = self.state.clone()
        st.resize = ResizeState()
        self.commit_state(st, push_history=True)

    def rotate_image(self, delta: int):
        old_state = self.state.clone()
        st = self.state.clone()
        st.rotation = (st.rotation + delta) % 360
        self.remap_crop_for_new_transform(old_state, st)
        self.commit_state(st, push_history=True)

    def toggle_flip(self, axis: str):
        old_state = self.state.clone()
        st = self.state.clone()
        if axis == "h":
            st.flip_h = not st.flip_h
        else:
            st.flip_v = not st.flip_v
        self.remap_crop_for_new_transform(old_state, st)
        self.commit_state(st, push_history=True)

    def toggle_before_after(self):
        self.preview_original_while_held = not self.preview_original_while_held
        self.update_viewer_pixmap(show_original=self.preview_original_while_held)


def install_slider_commit_hooks(window: MainWindow):
    sliders = [v[0] for v in window.controls.values()] + [window.resize_w_slider, window.resize_h_slider]
    seen = set()
    for slider in sliders:
        if id(slider) in seen:
            continue
        seen.add(id(slider))
        slider.sliderPressed.connect(lambda w=window: setattr(w, "_interactive_drag", True))
        slider.sliderReleased.connect(window.finalize_interaction)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    win = MainWindow()
    install_slider_commit_hooks(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

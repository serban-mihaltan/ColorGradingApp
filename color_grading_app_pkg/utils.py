import os
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileDialog

from .config import HISTOGRAM_MAX_SIDE

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False


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

    existing_numbers = set()
    prefix = stem
    suffix = ext

    try:
        for name in os.listdir(directory):
            if not name.endswith(suffix):
                continue
            candidate_stem, candidate_ext = os.path.splitext(name)
            if candidate_ext != suffix:
                continue
            if candidate_stem == prefix:
                existing_numbers.add(0)
                continue
            if candidate_stem.startswith(prefix + "(") and candidate_stem.endswith(")"):
                middle = candidate_stem[len(prefix) + 1:-1]
                if middle.isdigit():
                    existing_numbers.add(int(middle))
    except Exception:
        existing_numbers = {0} if os.path.exists(path) else set()

    n = 1
    while n in existing_numbers:
        n += 1
    return os.path.join(directory, f"{stem}({n}){ext}")


def choose_save_path(parent, title: str, suggested: str, file_filter: str) -> str:
    suggested_abs = os.path.abspath(suggested)
    suggested_dir = os.path.dirname(suggested_abs) or os.getcwd()
    suggested_name = os.path.basename(suggested_abs)
    prefilled_name = os.path.basename(unique_path(os.path.join(suggested_dir, suggested_name)))

    dialog = QFileDialog(parent, title, suggested_dir)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setNameFilter(file_filter)
    dialog.selectFile(prefilled_name)
    dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
    if dialog.exec():
        files = dialog.selectedFiles()
        if files:
            return files[0]
    return ""


def build_curve_lut(points: list[tuple[float, float]]) -> np.ndarray:
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

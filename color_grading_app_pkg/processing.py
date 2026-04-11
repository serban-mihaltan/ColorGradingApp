import numpy as np

from .models import AdjustmentState, CropRect, ResizeState
from .utils import build_curve_lut, compose_scalar_lut, resize_rgba

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False


class ImageProcessor:
    @staticmethod
    def apply_crop_rotate_flip(img: np.ndarray, state: AdjustmentState) -> np.ndarray:
        out = img
        if state.rotation % 360 != 0:
            k = (state.rotation % 360) // 90
            out = np.ascontiguousarray(np.rot90(out, k=4 - k))
        if state.flip_h:
            out = np.ascontiguousarray(np.flip(out, axis=1))
        if state.flip_v:
            out = np.ascontiguousarray(np.flip(out, axis=0))
        if state.crop.enabled and state.crop.w > 1 and state.crop.h > 1:
            h, w = out.shape[:2]
            x = int(np.clip(state.crop.x, 0, max(0, w - 1)))
            y = int(np.clip(state.crop.y, 0, max(0, h - 1)))
            cw = int(np.clip(state.crop.w, 1, max(1, w - x)))
            ch = int(np.clip(state.crop.h, 1, max(1, h - y)))
            out = np.ascontiguousarray(out[y:y + ch, x:x + cw])
        return out

    @staticmethod
    def apply_resize(img: np.ndarray, state: AdjustmentState, fast: bool) -> np.ndarray:
        if state.resize.enabled and state.resize.width > 1 and state.resize.height > 1:
            return resize_rgba(img, (state.resize.width, state.resize.height), fast=fast)
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

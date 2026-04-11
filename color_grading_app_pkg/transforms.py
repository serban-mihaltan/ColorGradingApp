from typing import Tuple
from PySide6.QtCore import QRect


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

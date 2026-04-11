import copy
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple


@dataclass
class CurveSet:
    master: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    red: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    green: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])
    blue: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0), (1.0, 1.0)])

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
        self.undo_stack: list[AdjustmentState] = []
        self.redo_stack: list[AdjustmentState] = []

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

from typing import Optional, Tuple

import numpy as np
from PySide6.QtCore import QObject, Signal

from .models import AdjustmentState
from .processing import ImageProcessor
from .utils import fit_size_preserving_aspect, histogram_from_rgba, resize_rgba


class RenderRequest:
    def __init__(self, generation: int, source: np.ndarray, state: AdjustmentState, display_size: Tuple[int, int], full_quality: bool, skip_tonal: bool):
        """Store all parameters required for one preview render request."""
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
        """Initialize the background render worker and its pending-request state."""
        super().__init__()
        self.pending: Optional[RenderRequest] = None
        self.busy = False

    def submit(self, request: RenderRequest):
        """Queue a new render request, replacing any older pending request."""
        self.pending = request
        if not self.busy:
            self._process_next()

    def _process_next(self):
        """Process the next queued request and emit preview and histogram results."""
        if self.pending is None:
            self.busy = False
            return
        self.busy = True
        req = self.pending
        self.pending = None
        try:
            work = ImageProcessor.apply_crop_rotate_flip(req.source, req.state)
            work = ImageProcessor.apply_resize(work, req.state, fast=not req.full_quality)
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

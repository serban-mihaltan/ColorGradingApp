import json
import os
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image
from PySide6.QtCore import QThread, QTimer, Qt, QRect
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSlider, QSplitter, QTabWidget, QToolBar,
    QVBoxLayout, QWidget,
)

from .config import (
    APP_TITLE, FULL_IDLE_DELAY_MS, MIN_CROP_SIZE, PRESET_FILTER, PREVIEW_RENDER_SCALE,
    PROJECT_FILTER, RENDER_DEBOUNCE_MS, SUPPORTED_EXPORT, SUPPORTED_INPUT,
)
from .models import AdjustmentState, CropRect, CurveSet, HistoryManager, ResizeState, ToneRGB
from .processing import ImageProcessor
from .render import RenderRequest, RenderWorker
from .transforms import rect_original_to_view, rect_view_to_original
from .utils import (
    choose_save_path, fit_size_preserving_aspect, hash_array, histogram_from_rgba,
    numpy_to_pixmap, pil_to_numpy_rgba, unique_path,
)
from .widgets.curve_editor import CurveEditor
from .widgets.histogram_widget import HistogramWidget
from .widgets.image_view import ImageView


class MainWindow(QMainWindow):
    def __init__(self):
        """Initialize application state, worker thread, timers, UI, and action bindings."""
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
        """Prompt for unsaved changes and shut down the worker thread before closing."""
        if not self.confirm_discard_unsaved():
            event.ignore()
            return
        self.worker_thread.quit()
        self.worker_thread.wait(1000)
        super().closeEvent(event)

    def build_ui(self):
        """Construct the main window layout, tab panels, viewer, and toolbar."""
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
        """Apply the application's dark stylesheet and shared widget styling."""
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
        """Build the Adjustments tab with grading, channel, tonal, and reset controls."""
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
        """Build the Curves tab with channel selection and the interactive curve editor."""
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
        """Build the Histogram tab with the standalone histogram display."""
        w = QWidget()
        l = QVBoxLayout(w)
        self.histogram_widget = HistogramWidget()
        l.addWidget(self.histogram_widget)
        t = "Histogram updates after the idle/full-quality render and is shown inside the curve editor."
        l.addWidget(QLabel(t))
        l.addStretch(1)
        return w

    def build_transform_tab(self):
        """Build the Transform tab with navigation, crop, rotate, flip, and resize tools."""
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
        """Create reusable Qt actions and keyboard shortcuts for common operations."""
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
        """Create the top toolbar and populate it with file, history, and preview actions."""
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
        """Create one slider-label pair for a numeric UI control."""
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(mn, mx)
        slider.setValue(default)
        label = QLabel(str(default))
        label.setMinimumWidth(60)
        slider.valueChanged.connect(lambda v, lab=label: lab.setText(str(v)))
        return slider, label

    def wrap_slider_row(self, slider: QSlider, label: QLabel):
        """Wrap a slider and its value label into a compact horizontal row widget."""
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(slider, 1)
        l.addWidget(label)
        return w

    def make_slider_group(self, parent_layout, spec_list):
        """Create a group of labeled sliders from a compact control specification list."""
        out = {}
        for key, title, mn, mx, default in spec_list:
            slider, label = self.make_single_slider(mn, mx, default)
            slider.valueChanged.connect(lambda _, k=key: self.on_slider_changed(k))
            parent_layout.addWidget(QLabel(title))
            parent_layout.addWidget(self.wrap_slider_row(slider, label))
            out[key] = (slider, label)
        return out

    def make_tone_group(self, title: str, prefix: str):
        """Create a three-channel tonal control group for shadows, midtones, or highlights."""
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

    def get_display_target_size(self) -> Tuple[int, int]:
        """Compute the preview render target size from geometry state and viewport size."""
        if self.original_rgba is None:
            return (800, 600)
        base = ImageProcessor.apply_crop_rotate_flip(self.original_rgba, self.state)
        geom_w, geom_h = base.shape[1], base.shape[0]
        if self.state.resize.enabled and self.state.resize.width > 1 and self.state.resize.height > 1:
            geom_w, geom_h = self.state.resize.width, self.state.resize.height
        if self.viewer.viewport().width() <= 1 or self.viewer.viewport().height() <= 1:
            return geom_w, geom_h
        vp = self.viewer.viewport().size()
        return fit_size_preserving_aspect(geom_w, geom_h, int(round((vp.width() - 8) * PREVIEW_RENDER_SCALE)), int(round((vp.height() - 8) * PREVIEW_RENDER_SCALE)))

    def get_crop_reference_size(self) -> Tuple[int, int]:
        """Return the transformed image size used as the crop editing coordinate space."""
        if self.original_rgba is None:
            return (800, 600)
        tmp = self.state.clone()
        tmp.crop = CropRect()
        ref = ImageProcessor.apply_crop_rotate_flip(self.original_rgba, tmp)
        return ref.shape[1], ref.shape[0]

    def get_current_crop_ratio(self):
        """Return the active crop aspect ratio from the crop ratio selector."""
        text = self.crop_ratio_combo.currentText()
        if text == "Original" and self.original_rgba is not None:
            if self.state.crop.enabled and self.state.crop.w > 1 and self.state.crop.h > 1:
                return self.state.crop.w / max(1, self.state.crop.h)
            return self.original_rgba.shape[1] / max(1, self.original_rgba.shape[0])
        if ":" in text:
            a, b = text.split(":", 1)
            return float(a) / max(0.01, float(b))
        return 1.0

    def update_crop_lock(self):
        """Push the current crop-lock settings into the image viewer."""
        self.viewer.set_crop_lock(self.crop_lock_check.isChecked(), self.get_current_crop_ratio())
        if self.crop_mode_check.isChecked():
            self.refresh_staged_crop_aspect()

    def refresh_staged_crop_aspect(self):
        """Rebuild the staged crop box so it matches the currently selected aspect ratio."""
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
        """Convert a crop rectangle from geometry space into viewer display space."""
        ref_w, ref_h = self.get_crop_reference_size()
        disp_w, disp_h = fit_size_preserving_aspect(ref_w, ref_h, *self.get_display_target_size())
        sx = disp_w / max(1, ref_w)
        sy = disp_h / max(1, ref_h)
        return QRect(int(round(rect.x() * sx)), int(round(rect.y() * sy)), max(1, int(round(rect.width() * sx))), max(1, int(round(rect.height() * sy))))

    def display_rect_to_geometry_rect(self, rect: QRect) -> QRect:
        """Convert a crop rectangle from viewer display space back into geometry space."""
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
        """Synchronize the viewer crop overlay with the current crop state."""
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
        """Queue a debounced low-latency preview render for interactive editing."""
        if self.original_rgba is None:
            return
        self._pending_fast_render = (self.state.clone(), skip_tonal)
        self._fast_debounce.start(RENDER_DEBOUNCE_MS)

    def flush_fast_render_request(self):
        """Submit the pending fast render request to the background worker."""
        if self.original_rgba is None or self._pending_fast_render is None:
            return
        state, skip_tonal = self._pending_fast_render
        self._pending_fast_render = None
        self._render_generation += 1
        self.worker.submit(RenderRequest(self._render_generation, self.original_rgba, state, self.get_display_target_size(), False, skip_tonal))

    def request_full_render(self):
        """Submit a full-quality render request for the current state."""
        if self.original_rgba is None:
            return
        self._render_generation += 1
        self.worker.submit(RenderRequest(self._render_generation, self.original_rgba, self.state.clone(), self.get_display_target_size(), True, False))

    def update_viewer_pixmap(self, show_original: bool = False):
        """Refresh the viewer pixmap from either the current preview or the original image."""
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
        """Push histogram data into both the histogram widget and the curve editor overlay."""
        self._latest_histogram = hist
        self.histogram_widget.set_histogram(hist)
        self.curve_editor.set_histogram(hist)

    def confirm_discard_unsaved(self) -> bool:
        """Prompt the user about unsaved changes and return whether the operation may continue."""
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
        """Handle main-window resize events and keep render-dependent UI values synchronized."""
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
        work = ImageProcessor.apply_crop_rotate_flip(self.original_rgba, self.state)
        work = ImageProcessor.apply_resize(work, self.state, fast=False)
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
        """Update sliders, resize controls, and curve UI to match the current state object."""
        self._building_ui = True
        mappings = [("brightness", self.state.brightness), ("contrast", self.state.contrast), ("gamma", self.state.gamma), ("exposure", self.state.exposure), ("temperature", self.state.temperature), ("tint", self.state.tint), ("white_balance_strength", self.state.white_balance_strength), ("red_intensity", self.state.red_intensity), ("green_intensity", self.state.green_intensity), ("blue_intensity", self.state.blue_intensity)]
        for key, val in mappings:
            self.controls[key][0].setValue(int(round(val * 100)))
        for prefix in ("shadows", "midtones", "highlights"):
            tone = getattr(self.state, prefix)
            for ch in ("r", "g", "b"):
                self.controls[f"{prefix}_{ch}"][0].setValue(int(round(getattr(tone, ch) * 100)))
        if self.original_rgba is not None:
            temp_state = self.state.clone()
            temp_state.resize = ResizeState()
            base_img = ImageProcessor.apply_crop_rotate_flip(self.original_rgba, temp_state)
            base_h, base_w = base_img.shape[:2]
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
        """Update the curve editor to match the active curve channel and current histogram."""
        self.curve_editor.set_channel(self.current_curve_channel)
        self.curve_editor.set_points(getattr(self.state.curves, self.current_curve_channel))
        self.curve_editor.set_histogram(self._latest_histogram)

    def refresh_actions(self):
        """Refresh enabled states for export, undo, and redo actions."""
        self.act_export.setEnabled(self.original_rgba is not None)
        self.act_undo.setEnabled(self.history.can_undo())
        self.act_redo.setEnabled(self.history.can_redo())

    def on_slider_changed(self, key: str):
        """Handle live updates from the main grading sliders."""
        if self._building_ui:
            return
        self._interactive_drag = True
        val = self.controls[key][0].value()
        st = self.state.clone()
        setattr(st, key, max(0.1, val / 100.0) if key == "gamma" else val / 100.0)
        self.commit_state(st, push_history=False)

    def on_tone_slider_changed(self, key: str):
        """Handle live updates from shadows, midtones, and highlights RGB sliders."""
        if self._building_ui:
            return
        self._interactive_drag = True
        value = self.controls[key][0].value() / 100.0
        st = self.state.clone()
        prefix, channel = key.split("_")
        setattr(getattr(st, prefix), channel, value)
        self.commit_state(st, push_history=False)

    def on_curve_channel_changed(self, channel: str):
        """Switch the curve editor to a different target channel."""
        self.current_curve_channel = channel
        self.sync_curve_editor_from_state()

    def on_curve_points_changed(self, points):
        """Apply live curve point edits from the curve editor to the current state."""
        self._interactive_drag = True
        st = self.state.clone()
        setattr(st.curves, self.current_curve_channel, points)
        self.commit_state(st, push_history=False)

    def on_crop_preview_changed(self, rect: QRect):
        """Show live crop dimensions in the status bar while the crop box is being edited."""
        self.statusBar().showMessage(f"Crop preview: {rect.width()} × {rect.height()}")

    def apply_crop_from_view(self):
        """Commit the staged crop box from viewer space into the application state."""
        if self.original_rgba is None:
            return
        geom_rect = self.display_rect_to_geometry_rect(self.viewer.current_crop_rect())
        st = self.state.clone()
        st.crop = CropRect(geom_rect.x(), geom_rect.y(), geom_rect.width(), geom_rect.height(), True)
        self.commit_state(st, push_history=True)
        self.crop_mode_check.setChecked(False)

    def cancel_crop_edit(self):
        """Cancel the current crop edit and restore the previously committed crop box."""
        self.viewer.revert_staged_crop()
        self.crop_mode_check.setChecked(False)

    def on_resize_dimension_changed(self, changed_axis: str):
        """Handle width or height resize slider changes and preserve aspect ratio when locked."""
        if self._building_ui or self.original_rgba is None:
            return
        self._interactive_drag = True
        temp_state = self.state.clone()
        temp_state.resize = ResizeState()
        base_img = ImageProcessor.apply_crop_rotate_flip(self.original_rgba, temp_state)
        base_h, base_w = base_img.shape[:2]

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
        """Commit the current resize UI values into the adjustment state."""
        if self._building_ui:
            return
        st = self.state.clone()
        st.resize.enabled = self.resize_enable.isChecked()
        st.resize.width = max(1, int(self.resize_w_slider.value()))
        st.resize.height = max(1, int(self.resize_h_slider.value()))
        self.commit_state(st, push_history=False)

    def undo(self):
        """Restore the previous history state and refresh all dependent UI and preview output."""
        self.finalize_interaction()
        self.state = self.history.undo(self.state)
        self.sync_controls_from_state()
        self.sync_viewer_crop_rect()
        self.request_fast_render(skip_tonal=False)
        self._full_timer.start(FULL_IDLE_DELAY_MS)
        self.refresh_actions()

    def redo(self):
        """Restore the next redo state and refresh all dependent UI and preview output."""
        self.state = self.history.redo(self.state)
        self.sync_controls_from_state()
        self.sync_viewer_crop_rect()
        self.request_fast_render(skip_tonal=False)
        self._full_timer.start(FULL_IDLE_DELAY_MS)
        self.refresh_actions()

    def reset_all(self):
        """Reset the full adjustment state back to defaults."""
        if self.original_rgba is not None:
            self.commit_state(AdjustmentState(), push_history=True)

    def reset_current_tab(self):
        """Reset only the controls associated with the currently selected tab."""
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
        """Reset one RGB channel-intensity adjustment back to zero."""
        st = self.state.clone()
        setattr(st, f"{channel}_intensity", 0.0)
        self.commit_state(st, push_history=True)

    def reset_tone_group(self, prefix: str):
        """Reset one tonal RGB block such as shadows, midtones, or highlights."""
        st = self.state.clone()
        setattr(st, prefix, ToneRGB())
        self.commit_state(st, push_history=True)

    def reset_current_curve(self):
        """Reset the currently selected curve to the default identity line."""
        st = self.state.clone()
        setattr(st.curves, self.current_curve_channel, [(0.0, 0.0), (1.0, 1.0)])
        self.commit_state(st, push_history=True)

    def clear_crop(self):
        """Clear the active crop from both state and viewer overlay."""
        st = self.state.clone()
        st.crop = CropRect()
        self.commit_state(st, push_history=True)
        self.viewer.clear_crop_rect()

    def reset_resize(self):
        """Disable and clear the current resize settings."""
        st = self.state.clone()
        st.resize = ResizeState()
        self.commit_state(st, push_history=True)

    def remap_crop_for_new_transform(self, old_state: AdjustmentState, new_state: AdjustmentState):
        """Remap crop coordinates so crop still targets the same content after rotate or flip."""
        if self.original_rgba is None:
            return
        if not old_state.crop.enabled or old_state.crop.w <= 1 or old_state.crop.h <= 1:
            return

        orig_h, orig_w = self.original_rgba.shape[:2]
        old_crop_view = QRect(old_state.crop.x, old_state.crop.y, old_state.crop.w, old_state.crop.h)
        crop_in_original = rect_view_to_original(
            old_crop_view,
            orig_w,
            orig_h,
            old_state.rotation,
            old_state.flip_h,
            old_state.flip_v,
        )
        new_crop_view = rect_original_to_view(
            crop_in_original,
            orig_w,
            orig_h,
            new_state.rotation,
            new_state.flip_h,
            new_state.flip_v,
        )
        new_state.crop = CropRect(new_crop_view.x(), new_crop_view.y(), new_crop_view.width(), new_crop_view.height(), True)

    def remap_resize_for_new_transform(self, old_state: AdjustmentState, new_state: AdjustmentState, delta_rotation: int = 0):
        """Remap resize dimensions after orientation changes, swapping width and height for 90/270 rotations."""
        if not old_state.resize.enabled or old_state.resize.width <= 1 or old_state.resize.height <= 1:
            return
        if delta_rotation % 180 != 0:
            new_state.resize.width, new_state.resize.height = old_state.resize.height, old_state.resize.width
        else:
            new_state.resize.width, new_state.resize.height = old_state.resize.width, old_state.resize.height

    def rotate_image(self, delta: int):
        """Rotate the image state by 90-degree steps and remap dependent crop and resize values."""
        old_state = self.state.clone()
        st = self.state.clone()
        st.rotation = (st.rotation + delta) % 360
        self.remap_crop_for_new_transform(old_state, st)
        self.remap_resize_for_new_transform(old_state, st, delta_rotation=delta)
        self.commit_state(st, push_history=True)

    def toggle_flip(self, axis: str):
        """Toggle horizontal or vertical flip and remap dependent crop and resize values."""
        old_state = self.state.clone()
        st = self.state.clone()
        if axis == "h":
            st.flip_h = not st.flip_h
        else:
            st.flip_v = not st.flip_v
        self.remap_crop_for_new_transform(old_state, st)
        self.remap_resize_for_new_transform(old_state, st, delta_rotation=0)
        self.commit_state(st, push_history=True)

    def toggle_before_after(self):
        """Toggle the viewer between edited preview and original-image preview."""
        self.preview_original_while_held = not self.preview_original_while_held
        self.update_viewer_pixmap(show_original=self.preview_original_while_held)


def install_slider_commit_hooks(window: MainWindow):
    """Attach slider press and release hooks so interactive edits finalize cleanly into history."""
    sliders = [v[0] for v in window.controls.values()] + [window.resize_w_slider, window.resize_h_slider]
    seen = set()
    for slider in sliders:
        if id(slider) in seen:
            continue
        seen.add(id(slider))
        slider.sliderPressed.connect(lambda w=window: setattr(w, "_interactive_drag", True))
        slider.sliderReleased.connect(window.finalize_interaction)

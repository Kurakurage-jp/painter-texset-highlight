# TexSet Spotlight - Adobe Substance 3D Painter plugin
#
# Select a texture set in the Texture Set List and the meshes that use it flash in the 3D viewport
# (hold for 1 s, then fade out over 1 s). With "X-ray" enabled, every other texture set is drawn
# semi-transparent and dimmed while the flash runs, so parts hidden inside the model show through.
#
# How it works
#   Painter has no API to overlay colors in the viewport, so the plugin temporarily assigns a shader
#   instance ("TexSet Spotlight", texset_spotlight_shader.glsl = metal/rough PBR with an emissive
#   highlight) to the selected texture set and animates its highlight_mix parameter. Other texture
#   sets get a "ghost" instance of the same shader (alpha/brightness multipliers). Layer stacks are
#   never touched. The original shader assignments are restored when the flash ends, before the
#   project is saved or closed, and on the next project open if Painter crashed mid-flash.
#
# Install: copy texset_spotlight.py and texset_spotlight_shader.glsl into Painter's plugins folder
#   (Windows: Documents\Adobe\Adobe Substance 3D Painter\python\plugins). Painter loads it at startup;
#   click "Spotlight" in the menu bar to open the settings panel (a dockable window with every option).
# Tested with Substance 3D Painter 12.1.1 (Vulkan) on Windows 11: Python API 0.3.5, Python 3.13.9, PySide6 6.8.6,
#   JavaScript API 1.1.20. Other 12.x releases are expected to work but were not checked.
import json
import os
import tempfile
import time

import substance_painter.event as spevent
import substance_painter.js as spjs
import substance_painter.logging as splog
import substance_painter.project as spproj
import substance_painter.resource as spres
import substance_painter.textureset as spts
import substance_painter.ui as spui
from PySide6 import QtCore, QtGui, QtWidgets

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SHADER_NAME = "texset_spotlight_shader"
SHADER_FILE = os.path.join(PLUGIN_DIR, SHADER_NAME + ".glsl")
HL_LABEL = "TexSet Spotlight"
GHOST_LABEL = "TexSet Spotlight Ghost"
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "TexSetSpotlight")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
POLL_MS = 150
FADE_STEP_MS = 33

_defaults = {"enabled": True, "xray": True, "hold_ms": 1000, "fade_ms": 1000, "color": [0.55, 1.0, 0.25],
             "intensity": 1.2, "tint": 0.5, "ghost_alpha": 0.12, "ghost_dim": 0.35}
_plugin = None


def log(msg):
    splog.info("[texset_spotlight] " + str(msg))


def _settings():
    return QtCore.QSettings("KURAKUlab", "TexSetSpotlight")


def load_settings():
    s = _settings()
    out = dict(_defaults)
    for k, v in _defaults.items():
        val = s.value(k)
        if val is None:
            continue
        try:
            if isinstance(v, bool):
                out[k] = str(val).lower() in ("1", "true")
            elif isinstance(v, int):
                out[k] = int(val)
            elif isinstance(v, float):
                out[k] = float(val)
            elif isinstance(v, list):
                out[k] = [float(x) for x in (val if isinstance(val, (list, tuple)) else json.loads(val))]
        except Exception:
            pass
    return out


def save_settings(d):
    s = _settings()
    for k, v in d.items():
        s.setValue(k, json.dumps(v) if isinstance(v, list) else v)


def js(code):
    return spjs.evaluate(code)


def shader_object():
    """Shader instances and the texture set -> instance assignment, as a JSON-compatible dict."""
    return json.loads(js("JSON.stringify(alg.shaders.shaderInstancesToObject())"))


def apply_shader_object(obj):
    js("alg.shaders.shaderInstancesFromObject(%s)" % json.dumps(obj, ensure_ascii=False))


def instance_ids():
    # Instance ids change after shaderInstancesFromObject, so always look them up by label.
    return {i["label"]: i["id"] for i in json.loads(js("JSON.stringify(alg.shaders.instances())"))}


def set_params(label, params):
    sid = instance_ids().get(label)
    if sid is None:
        return
    js("alg.shaders.setParameters(%d, %s)" % (sid, json.dumps(params)))


def active_set_name():
    try:
        stack = spts.get_active_stack()
    except Exception:
        return None
    for getter in (lambda: stack.texture_set().name(), lambda: stack.name(), lambda: str(stack)):
        try:
            n = getter()
            if n:
                return n
        except Exception:
            continue
    return None


class Spotlight(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.cfg = load_settings()
        self.last_name = None
        # While a flash runs: {"set": name, "originals": {set: label}, "shaders_backup": {label: definition}, "project": path}
        self.active = None
        self.shader_url = None
        self.poll = QtCore.QTimer(self)
        self.poll.setInterval(POLL_MS)
        self.poll.timeout.connect(self.on_poll)
        self.hold = QtCore.QTimer(self)
        self.hold.setSingleShot(True)
        self.hold.timeout.connect(self.start_fade)
        self.fade = QtCore.QTimer(self)
        self.fade.setInterval(FADE_STEP_MS)
        self.fade.timeout.connect(self.on_fade)
        self.fade_t0 = 0.0
        self.menu = None
        self.dock = None
        self.panel = None
        self.menu_action = None
        spevent.DISPATCHER.connect(spevent.ProjectAboutToSave, self.on_about_to_save)
        spevent.DISPATCHER.connect(spevent.ProjectAboutToClose, self.on_about_to_close)
        spevent.DISPATCHER.connect(spevent.ProjectEditionEntered, self.on_edition_entered)
        self.poll.start()
        self.build_menu()
        self.recover()

    # ---- shader resource
    def ensure_shader(self):
        """Use the shader from a shelf if one is registered, otherwise import the file next to this plugin."""
        if self.shader_url:
            return self.shader_url
        for r in spres.search("u:shader"):
            i = r.identifier()
            if i.name == SHADER_NAME and not i.context.startswith("session"):
                self.shader_url = i.url()
                return self.shader_url
        if os.path.exists(SHADER_FILE):
            self.shader_url = spres.import_session_resource(SHADER_FILE, spres.Usage.SHADER).identifier().url()
            log("shader imported into the session: " + SHADER_FILE)
            return self.shader_url
        log("shader file not found: " + SHADER_FILE)
        return None

    # ---- selection polling (Painter has no texture-set-selection event)
    def on_poll(self):
        if not self.cfg["enabled"]:
            return
        try:
            if not (spproj.is_open() and spproj.is_in_edition_state()) or spproj.is_busy():
                return
        except Exception:
            return
        name = active_set_name()
        if name is None:
            return
        if self.last_name is None:
            self.last_name = name  # do not flash right after startup / project open
            return
        if name != self.last_name:
            self.last_name = name
            self.flash(name)

    # ---- flash
    def flash(self, name):
        try:
            url = self.ensure_shader()
            if not url:
                return
            self.hold.stop()
            self.fade.stop()
            obj = shader_object()
            sets = obj.get("texturesets", {})
            if name not in sets:
                return
            if self.active is None:
                originals = {s: v.get("shader") for s, v in sets.items()}
                # Keep the instance definitions too: Painter deletes an instance once no texture set uses it
                # (which happens to the original instance when every set is switched for the x-ray view).
                shaders_backup = {k: v for k, v in obj["shaders"].items() if k not in (HL_LABEL, GHOST_LABEL)}
            else:
                originals = self.active["originals"]
                shaders_backup = self.active["shaders_backup"]
            obj["shaders"][HL_LABEL] = {"shader": url, "shaderInstance": HL_LABEL, "parameters": {}}
            if self.cfg["xray"]:
                obj["shaders"][GHOST_LABEL] = {"shader": url, "shaderInstance": GHOST_LABEL, "parameters": {}}
            else:
                obj["shaders"].pop(GHOST_LABEL, None)
            for s in sets:
                if s == name:
                    obj["texturesets"][s] = {"shader": HL_LABEL}
                elif self.cfg["xray"]:
                    obj["texturesets"][s] = {"shader": GHOST_LABEL}
                else:
                    obj["texturesets"][s] = {"shader": originals.get(s) or sets[s].get("shader")}
            apply_shader_object(obj)
            self.active = {"set": name, "originals": originals, "shaders_backup": shaders_backup, "project": spproj.file_path() or ""}
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.active, f, ensure_ascii=False)
            self.set_mix(1.0)
            self.hold.start(int(self.cfg["hold_ms"]))
        except Exception as e:
            log("flash failed: %r" % (e,))
            self.restore()

    def set_mix(self, mix):
        c = self.cfg["color"]
        set_params(HL_LABEL, {"highlight_mix": mix, "highlight_color": c, "highlight_intensity": self.cfg["intensity"],
                              "highlight_tint": self.cfg["tint"], "ghost_alpha": 1.0, "ghost_dim": 1.0})
        if self.cfg["xray"]:
            ga, gd = self.cfg["ghost_alpha"], self.cfg["ghost_dim"]
            set_params(GHOST_LABEL, {"highlight_mix": 0.0, "ghost_alpha": ga + (1.0 - ga) * (1.0 - mix),
                                     "ghost_dim": gd + (1.0 - gd) * (1.0 - mix)})

    def start_fade(self):
        if self.active is None:
            return
        self.fade_t0 = time.time()
        self.fade.start()

    def on_fade(self):
        if self.active is None:
            self.fade.stop()
            return
        t = (time.time() - self.fade_t0) * 1000.0
        mix = 1.0 - t / max(1.0, float(self.cfg["fade_ms"]))
        if mix <= 0.0:
            self.fade.stop()
            self.restore()
            return
        try:
            self.set_mix(mix * mix)  # ease-in: fades faster toward the end
        except Exception as e:
            log("fade failed: %r" % (e,))
            self.fade.stop()
            self.restore()

    # ---- restore
    def restore(self):
        self.hold.stop()
        self.fade.stop()
        if self.active is None:
            return
        info, self.active = self.active, None
        try:
            obj = shader_object()
            originals = info.get("originals", {})
            for label, definition in info.get("shaders_backup", {}).items():
                if label not in obj["shaders"]:
                    obj["shaders"][label] = definition  # re-create instances Painter removed
            fallback = next((k for k in obj["shaders"] if k not in (HL_LABEL, GHOST_LABEL)), None)
            for s, v in obj.get("texturesets", {}).items():
                if v.get("shader") in (HL_LABEL, GHOST_LABEL):
                    target = originals.get(s) or fallback
                    if target in obj["shaders"]:
                        obj["texturesets"][s] = {"shader": target}
                    elif fallback:
                        obj["texturesets"][s] = {"shader": fallback}
            obj["shaders"].pop(HL_LABEL, None)
            obj["shaders"].pop(GHOST_LABEL, None)
            apply_shader_object(obj)
        except Exception as e:
            log("restore failed: %r" % (e,))
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except OSError:
            pass

    def recover(self):
        """If Painter died mid-flash, put the shader assignments back when that project is opened again."""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if spproj.is_open() and spproj.is_in_edition_state() and saved.get("project") in ("", spproj.file_path()):
                self.active = saved
                self.restore()
        except Exception as e:
            log("recover failed: %r" % (e,))
            self.active = None

    # ---- events
    def on_about_to_save(self, e):
        self.restore()

    def on_about_to_close(self, e):
        self.restore()
        self.last_name = None
        self.shader_url = None  # session resources belong to the project

    def on_edition_entered(self, e):
        self.last_name = None
        self.shader_url = None
        QtCore.QTimer.singleShot(500, self.recover)

    # ---- settings panel (one dockable window with every control) + "Spotlight" entry in the menu bar
    def build_menu(self):
        self.panel = SettingsPanel(self)
        self.dock = spui.add_dock_widget(self.panel)
        try:
            self.dock.hide()  # stays available in Painter's Window menu; shown from the menu bar entry
        except Exception:
            pass
        # Painter's menu bar only accepts menus, so "Spotlight" is a menu that opens the panel as soon as it is
        # clicked (aboutToShow) and closes itself again; its single item does the same for keyboard users.
        self.menu_action = QtGui.QAction("Open the Spotlight panel")
        self.menu_action.triggered.connect(self.show_panel)
        self.menu = QtWidgets.QMenu("Spotlight")
        self.menu.addAction(self.menu_action)
        self.menu.aboutToShow.connect(self.on_menu_clicked)
        spui.add_menu(self.menu)

    def on_menu_clicked(self):
        QtCore.QTimer.singleShot(0, self.menu.hide)
        QtCore.QTimer.singleShot(0, self.show_panel)

    def show_panel(self):
        self.panel.refresh()
        for w in (self.dock, self.panel):
            try:
                w.show()
                w.raise_()
            except Exception:
                pass

    def update_setting(self, key, value):
        self.cfg[key] = value
        save_settings(self.cfg)
        if key == "enabled":
            if not value:
                self.restore()
            self.last_name = None
        elif self.active is not None and key in ("color", "intensity", "tint", "ghost_alpha", "ghost_dim"):
            try:
                self.set_mix(1.0 if self.hold.isActive() else 0.5)  # preview while a flash is running
            except Exception:
                pass

    def flash_now(self):
        name = active_set_name()
        if name:
            self.last_name = name
            self.flash(name)

    def shutdown(self):
        self.poll.stop()
        self.restore()
        for ev, cb in ((spevent.ProjectAboutToSave, self.on_about_to_save), (spevent.ProjectAboutToClose, self.on_about_to_close),
                       (spevent.ProjectEditionEntered, self.on_edition_entered)):
            try:
                spevent.DISPATCHER.disconnect(ev, cb)
            except Exception:
                pass
        for el in (self.menu, self.dock):
            if el is not None:
                try:
                    spui.delete_ui_element(el)
                except Exception:
                    pass
        self.menu = None
        self.dock = None


class SettingsPanel(QtWidgets.QWidget):
    """All Spotlight settings in one place. Values apply immediately and are saved."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setWindowTitle("Spotlight")
        self.setObjectName("TexSetSpotlightPanel")
        self.controls = {}
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self.chk_enabled = QtWidgets.QCheckBox("Flash the selected texture set")
        self.chk_enabled.toggled.connect(lambda on: owner.update_setting("enabled", bool(on)))
        lay.addWidget(self.chk_enabled)
        self.chk_xray = QtWidgets.QCheckBox("X-ray: see through the other texture sets")
        self.chk_xray.toggled.connect(lambda on: owner.update_setting("xray", bool(on)))
        lay.addWidget(self.chk_xray)

        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        lay.addLayout(form)
        self.row = 0
        self.color_btn = QtWidgets.QPushButton()
        self.color_btn.setFixedWidth(90)
        self.color_btn.clicked.connect(self.pick_color)
        form.addWidget(QtWidgets.QLabel("Highlight color"), self.row, 0)
        form.addWidget(self.color_btn, self.row, 1)
        self.row += 1
        self.add_slider(form, "hold_ms", "Hold time (ms)", 100, 10000, 100, 1)
        self.add_slider(form, "fade_ms", "Fade time (ms)", 100, 10000, 100, 1)
        self.add_slider(form, "intensity", "Highlight intensity", 0.0, 8.0, 0.1, 100)
        self.add_slider(form, "tint", "Highlight tint", 0.0, 1.0, 0.05, 100)
        self.add_slider(form, "ghost_alpha", "X-ray opacity", 0.0, 1.0, 0.01, 100)
        self.add_slider(form, "ghost_dim", "X-ray brightness", 0.0, 1.0, 0.05, 100)

        btns = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("Flash now")
        b1.clicked.connect(owner.flash_now)
        b2 = QtWidgets.QPushButton("Reset to defaults")
        b2.clicked.connect(self.reset_defaults)
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addStretch(1)
        self.refresh()

    def add_slider(self, form, key, label, lo, hi, step, scale):
        """Slider + spin box pair. `scale` maps float values to slider integers (1 for int settings)."""
        is_int = scale == 1
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(int(lo * scale), int(hi * scale))
        slider.setSingleStep(max(1, int(step * scale)))
        if is_int:
            spin = QtWidgets.QSpinBox()
            spin.setRange(int(lo), int(hi))
            spin.setSingleStep(int(step))
        else:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setDecimals(2)
        spin.setFixedWidth(90)
        guard = {"busy": False}

        def from_slider(v):
            if guard["busy"]:
                return
            guard["busy"] = True
            val = int(v) if is_int else v / scale
            spin.setValue(val)
            self.owner.update_setting(key, val)
            guard["busy"] = False

        def from_spin(v):
            if guard["busy"]:
                return
            guard["busy"] = True
            slider.setValue(int(round(v * scale)))
            self.owner.update_setting(key, int(v) if is_int else float(v))
            guard["busy"] = False

        slider.valueChanged.connect(from_slider)
        spin.valueChanged.connect(from_spin)
        form.addWidget(QtWidgets.QLabel(label), self.row, 0)
        form.addWidget(slider, self.row, 1)
        form.addWidget(spin, self.row, 2)
        self.row += 1
        self.controls[key] = (slider, spin, scale, is_int)

    def refresh(self):
        cfg = self.owner.cfg
        for w, key in ((self.chk_enabled, "enabled"), (self.chk_xray, "xray")):
            w.blockSignals(True)
            w.setChecked(bool(cfg[key]))
            w.blockSignals(False)
        for key, (slider, spin, scale, is_int) in self.controls.items():
            for w in (slider, spin):
                w.blockSignals(True)
            slider.setValue(int(round(cfg[key] * scale)))
            spin.setValue(int(cfg[key]) if is_int else float(cfg[key]))
            for w in (slider, spin):
                w.blockSignals(False)
        self.update_color_button()

    def update_color_button(self):
        c = self.owner.cfg["color"]
        col = QtGui.QColor.fromRgbF(c[0], c[1], c[2])
        self.color_btn.setText(col.name())
        self.color_btn.setStyleSheet("background-color: %s; color: %s;" % (col.name(), "#000" if col.lightnessF() > 0.5 else "#fff"))

    def pick_color(self):
        c = self.owner.cfg["color"]
        col = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(c[0], c[1], c[2]), self, "Spotlight color")
        if col.isValid():
            self.owner.update_setting("color", [col.redF(), col.greenF(), col.blueF()])
            self.update_color_button()

    def reset_defaults(self):
        for k, v in _defaults.items():
            self.owner.cfg[k] = list(v) if isinstance(v, list) else v
        save_settings(self.owner.cfg)
        self.refresh()


def start_plugin():
    global _plugin
    _plugin = Spotlight()
    log("started")


def close_plugin():
    global _plugin
    if _plugin is not None:
        _plugin.shutdown()
        _plugin = None
    log("closed")


if __name__ == "__main__":
    start_plugin()

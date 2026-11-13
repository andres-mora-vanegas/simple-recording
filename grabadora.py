#!/usr/bin/env python3
"""Grabadora: grabador de pantalla sencillo al estilo de Kooha, para X11.

Captura con GStreamer (ximagesrc) y audio con PulseAudio/PipeWire (pulsesrc).
Uso:
    grabadora            abre la ventana
    grabadora --toggle   empieza o detiene una grabación (para un atajo de teclado)
"""

import json
import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

APP_ID = "org.local.Grabadora"
# Súbela en cada cambio: 1.0.0, 1.0.1, 1.0.2… (se muestra al pie de la ventana).
VERSION = "1.0.1"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "grabadora")
CONFIG_FILE = os.path.join(CONFIG_DIR, "ajustes.json")

FORMATS = {
    # nombre: (extensión, codificador de video, codificador de audio, muxer)
    "MP4": (
        "mp4",
        "x264enc speed-preset=superfast pass=quant quantizer=21 key-int-max=60 "
        "! video/x-h264,profile=high ! h264parse",
        "avenc_aac bitrate=192000 ! aacparse",
        "mp4mux",
    ),
    "MKV": (
        "mkv",
        "x264enc speed-preset=superfast pass=quant quantizer=21 key-int-max=60 "
        "! video/x-h264,profile=high ! h264parse",
        "opusenc bitrate=160000",
        "matroskamux",
    ),
    "WebM": (
        "webm",
        "vp8enc deadline=1 cpu-used=8 threads=8 end-usage=cbr "
        "target-bitrate=8000000 keyframe-max-dist=60",
        "opusenc bitrate=160000",
        "webmmux",
    ),
}

DEFAULTS = {
    "mode": "full",  # "full" o "area"
    "desktop_audio": True,
    "microphone": False,
    "microphone_device": "",  # "" = el micrófono predeterminado del sistema
    "desktop_device": "",  # "" = la salida de audio predeterminada del sistema
    "pointer": True,
    "format": "MP4",
    "framerate": 30,
    "delay": 0,
    "hide_window": True,
    "folder": GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    or os.path.expanduser("~"),
    "last_area": None,
}

CSS = b"""
.big-toggle { padding: 14px 10px; }
.big-toggle label { font-weight: bold; }
.record-button { padding: 10px 28px; border-radius: 999px; font-weight: bold; }
.round-button { border-radius: 999px; padding: 14px; min-width: 24px; min-height: 24px; }
.option-toggle:checked { background: @theme_selected_bg_color; color: @theme_selected_fg_color; }
.option-toggle:checked image { color: @theme_selected_fg_color; }
.timer { font-size: 40px; font-weight: bold; font-feature-settings: "tnum"; }
.countdown { font-size: 64px; font-weight: bold; }
.dim { opacity: 0.6; }
.version { font-size: smaller; opacity: 0.5; }
"""


def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass
    if settings["format"] not in FORMATS:
        settings["format"] = DEFAULTS["format"]
    return settings


def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def pactl(*args):
    try:
        return subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def list_microphones():
    """Entradas de audio reales (sin los "monitor" de las salidas): [(nombre, descripción)]."""
    try:
        sources = json.loads(pactl("-f", "json", "list", "sources") or "[]")
    except ValueError:
        return []
    return [
        (s["name"], s.get("description") or s["name"])
        for s in sources
        if not s["name"].endswith(".monitor") and not s.get("monitor_source")
    ]


def list_outputs():
    """Salidas de audio (altavoces, audífonos…): [(nombre, descripción)]."""
    try:
        sinks = json.loads(pactl("-f", "json", "list", "sinks") or "[]")
    except ValueError:
        return []
    return [(s["name"], s.get("description") or s["name"]) for s in sinks]


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_time(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)
    return "%02d:%02d" % (seconds // 60, seconds % 60)


class Recorder:
    """Arma y controla el pipeline de GStreamer de una grabación."""

    def __init__(self, settings, area, path, on_error, on_finished):
        self.path = path
        self.on_error = on_error
        self.on_finished = on_finished
        self.stopping = False
        # La pausa no detiene el pipeline: poner fuentes en vivo (pulsesrc) y el
        # audiomixer en PAUSED y volver a PLAYING desincroniza el mezclador y el
        # audio se pierde al reanudar. En su lugar el pipeline sigue en PLAYING,
        # se descartan los buffers capturados durante la pausa y se corren las
        # marcas de tiempo de lo que viene después para que no quede un hueco.
        self._lock = threading.Lock()
        self._pause_start = None  # running time en que empezó la pausa actual
        self._pauses = []  # pausas terminadas: [(inicio, fin)] en running time
        self.pipeline = Gst.parse_launch(self._describe(settings, area))
        for name in ("vgate", "agate"):
            gate = self.pipeline.get_by_name(name)
            if gate is not None:
                gate.get_static_pad("sink").add_probe(
                    Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    self._gate_probe,
                    {"src": gate.get_static_pad("src"), "segment": None, "shift": 0},
                )
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)

    def _describe(self, settings, area):
        ext, venc, aenc, mux = FORMATS[settings["format"]]
        fps = int(settings["framerate"])
        src = "ximagesrc use-damage=false show-pointer=%s" % (
            "true" if settings["pointer"] else "false"
        )
        if area:
            x, y, w, h = area
            src += " startx=%d starty=%d endx=%d endy=%d" % (x, y, x + w - 1, y + h - 1)
        parts = [
            "%s ! video/x-raw,framerate=%d/1 ! queue max-size-buffers=0 "
            "max-size-bytes=0 max-size-time=3000000000 leaky=downstream ! videoconvert "
            "! videoscale ! video/x-raw,format=I420 ! queue ! identity name=vgate "
            "! %s ! queue ! mux."
            % (src, fps, venc),
            "%s name=mux ! filesink location=%s" % (mux, quote(self.path)),
        ]

        sources = []
        if settings["desktop_audio"]:
            sink = settings.get("desktop_device")
            # Si la salida elegida ya no está conectada, usar la predeterminada.
            if not sink or sink not in dict(list_outputs()):
                sink = pactl("get-default-sink")
            if sink:
                sources.append(sink + ".monitor")
        if settings["microphone"]:
            source = settings.get("microphone_device")
            # Si el micrófono elegido ya no está conectado, usar el predeterminado.
            if not source or source not in dict(list_microphones()):
                source = pactl("get-default-source")
            sources.append(source or None)

        audio_caps = "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
        if len(sources) == 1:
            parts.append(
                "%s ! queue ! %s ! identity name=agate ! %s ! queue ! mux."
                % (self._pulsesrc(sources[0]), audio_caps, aenc)
            )
        elif len(sources) == 2:
            parts.append(
                "audiomixer name=amix ! audioconvert ! identity name=agate ! %s "
                "! queue ! mux." % aenc
            )
            for device in sources:
                parts.append(
                    "%s ! queue ! %s ! amix." % (self._pulsesrc(device), audio_caps)
                )
        return " ".join(parts)

    @staticmethod
    def _pulsesrc(device):
        src = "pulsesrc do-timestamp=true provide-clock=false"
        if device:
            src += " device=%s" % quote(device)
        return src

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("No se pudo iniciar la grabación.")

    def _running_time(self):
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, clock.get_time() - self.pipeline.get_base_time())

    def pause(self):
        with self._lock:
            if self._pause_start is None:
                self._pause_start = self._running_time()

    def resume(self):
        with self._lock:
            if self._pause_start is not None:
                self._pauses.append((self._pause_start, self._running_time()))
                self._pause_start = None

    def _gate_probe(self, pad, info, state):
        """Antes de cada codificador: descarta lo grabado en pausa y corre el resto."""
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            if event.type == Gst.EventType.SEGMENT:
                state["segment"] = event.parse_segment().copy()
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        segment = state["segment"]
        if buf.pts == Gst.CLOCK_TIME_NONE or segment is None:
            return Gst.PadProbeReturn.OK
        rt = segment.to_running_time(Gst.Format.TIME, buf.pts)
        if rt == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        shift = 0
        with self._lock:
            if self._pause_start is not None and rt >= self._pause_start:
                return Gst.PadProbeReturn.DROP
            for start, end in self._pauses:
                if rt >= end:
                    shift += end - start
                elif rt >= start:
                    return Gst.PadProbeReturn.DROP
        if shift != state["shift"]:
            # Se aplica a este mismo buffer: el src pad reenvía el segmento corrido.
            state["shift"] = shift
            state["src"].set_offset(-shift)
        return Gst.PadProbeReturn.OK

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        self.pipeline.send_event(Gst.Event.new_eos())
        # Si el EOS no llega (p. ej. un dispositivo trabado), cerrar igual.
        GLib.timeout_add_seconds(5, self._force_finish)

    def cancel(self):
        self.stopping = True
        self._teardown()

    def _force_finish(self):
        if self.pipeline is not None:
            self._teardown()
            self.on_finished(self.path)
        return False

    def _teardown(self):
        if self.pipeline is not None:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _on_eos(self, _bus, _msg):
        self._teardown()
        self.on_finished(self.path)

    def _on_error(self, _bus, msg):
        err, debug = msg.parse_error()
        self._teardown()
        self.on_error(err.message, debug)


class AreaSelector(Gtk.Window):
    """Capa a pantalla completa para dibujar con el mouse el área a grabar."""

    def __init__(self, on_done):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.on_done = on_done
        self.start = None
        self.rect = None
        self.finished = False

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.move(0, 0)
        self.set_default_size(screen.get_width(), screen.get_height())
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_press)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("button-release-event", self._on_release)
        self.connect("key-press-event", self._on_key)
        self.connect("realize", self._on_realize)
        self.connect("delete-event", lambda *_: self._finish(None) or True)
        self.fullscreen()

    def _on_realize(self, _widget):
        cursor = Gdk.Cursor.new_from_name(self.get_display(), "crosshair")
        self.get_window().set_cursor(cursor)

    def _current_rect(self, x, y):
        sx, sy = self.start
        return (min(sx, x), min(sy, y), abs(x - sx), abs(y - sy))

    def _on_press(self, _w, event):
        if event.button == 1:
            self.start = (event.x, event.y)
            self.rect = (event.x, event.y, 0, 0)
        elif event.button == 3:
            self._finish(None)

    def _on_motion(self, _w, event):
        if self.start:
            self.rect = self._current_rect(event.x, event.y)
            self.queue_draw()

    def _on_release(self, _w, event):
        if event.button != 1 or not self.start:
            return
        x, y, w, h = self._current_rect(event.x, event.y)
        scale = self.get_scale_factor()
        x, y, w, h = (int(round(v * scale)) for v in (x, y, w, h))
        w -= w % 2  # el codificador necesita medidas pares
        h -= h % 2
        if w < 16 or h < 16:
            self.start = None
            self.rect = None
            self.queue_draw()
            return
        self._finish((x, y, w, h))

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self._finish(None)

    def _finish(self, rect):
        if self.finished:
            return
        self.finished = True
        self.destroy()
        self.on_done(rect)

    def _on_draw(self, _w, cr):
        width, height = self.get_allocated_width(), self.get_allocated_height()
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.paint()
        if self.rect and self.rect[2] > 0 and self.rect[3] > 0:
            x, y, w, h = self.rect
            cr.set_operator(0)  # CLEAR: deja ver lo que hay debajo
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_operator(2)  # OVER
            cr.set_source_rgba(0.21, 0.52, 0.89, 1)
            cr.set_line_width(2)
            cr.rectangle(x + 1, y + 1, w - 2, h - 2)
            cr.stroke()
            label = "%d × %d" % (w, h)
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(14)
            ext = cr.text_extents(label)
            lx, ly = x + 6, (y - 8 if y > 28 else y + h + 20)
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.rectangle(lx - 4, ly - ext.height - 5, ext.width + 8, ext.height + 10)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(lx, ly)
            cr.show_text(label)
        else:
            text = "Arrastra para elegir el área · Esc para cancelar"
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(22)
            ext = cr.text_extents(text)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to((width - ext.width) / 2, height / 2)
            cr.show_text(text)
        return False


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Grabadora")
        self.app = app
        self.settings = app.settings
        self.recorder = None
        self.paused = False
        self.elapsed = 0.0
        self.resumed_at = None
        self.tick_id = None
        self.countdown_id = None
        self.countdown_left = 0
        self.pending_area = None

        self.set_default_size(380, -1)
        self.set_resizable(False)
        self.set_icon_name("media-record")

        header = Gtk.HeaderBar(show_close_button=True, title="Grabadora")
        self.set_titlebar(header)
        self.menu_button = Gtk.Button(
            image=Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON),
            tooltip_text="Ajustes",
        )
        self.settings_window = self._build_settings_window()
        self.menu_button.connect("clicked", lambda *_: self.settings_window.present())
        header.pack_end(self.menu_button)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150
        )
        self.stack.add_named(self._build_main_view(), "main")
        self.stack.add_named(self._build_countdown_view(), "countdown")
        self.stack.add_named(self._build_recording_view(), "recording")
        self.stack.add_named(self._build_saving_view(), "saving")
        version = Gtk.Label(label="v" + VERSION, margin_bottom=6)
        version.get_style_context().add_class("version")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.pack_start(self.stack, True, True, 0)
        content.pack_end(version, False, False, 0)
        self.add(content)
        self.connect("delete-event", self._on_delete)

    # ---------- Construcción de la interfaz ----------

    def _build_main_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin=18)

        modes = Gtk.Box(spacing=0, homogeneous=True)
        modes.get_style_context().add_class("linked")
        self.full_button = self._mode_button("view-fullscreen-symbolic", "Pantalla completa")
        self.area_button = self._mode_button("edit-select-all-symbolic", "Área")
        self.area_button.join_group(self.full_button)
        (self.area_button if self.settings["mode"] == "area" else self.full_button).set_active(True)
        self.full_button.connect("toggled", self._on_mode_toggled)
        modes.pack_start(self.full_button, True, True, 0)
        modes.pack_start(self.area_button, True, True, 0)
        box.pack_start(modes, False, False, 0)

        toggles = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.desktop_toggle = self._option_toggle(
            "audio-speakers-symbolic", "Grabar audio del sistema", "desktop_audio"
        )
        self.mic_toggle = self._option_toggle(
            "audio-input-microphone-symbolic", "Grabar micrófono", "microphone"
        )
        self.pointer_toggle = self._option_toggle(
            "input-mouse-symbolic", "Mostrar el puntero", "pointer"
        )
        for t in (self.desktop_toggle, self.mic_toggle, self.pointer_toggle):
            toggles.pack_start(t, False, False, 0)
        box.pack_start(toggles, False, False, 0)

        # Dispositivos de audio visibles directamente en la ventana principal.
        devices = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.desktop_combo, self.desktop_combo_handler = self._device_row(
            devices, 0, "audio-speakers-symbolic", "Audio del sistema", "desktop_device"
        )
        self.mic_combo, self.mic_combo_handler = self._device_row(
            devices, 1, "audio-input-microphone-symbolic", "Micrófono", "microphone_device"
        )
        box.pack_start(devices, False, False, 0)
        self._refresh_devices()
        self._update_device_sensitivity()
        # Volver a leer los dispositivos al regresar a la ventana (por si conectas otro).
        self.connect("focus-in-event", lambda *_: self._refresh_devices() or False)

        self.record_button = Gtk.Button(halign=Gtk.Align.CENTER)
        record_box = Gtk.Box(spacing=8)
        record_box.pack_start(
            Gtk.Image.new_from_icon_name("media-record-symbolic", Gtk.IconSize.BUTTON),
            False, False, 0,
        )
        record_box.pack_start(Gtk.Label(label="Grabar"), False, False, 0)
        self.record_button.add(record_box)
        ctx = self.record_button.get_style_context()
        ctx.add_class("suggested-action")
        ctx.add_class("record-button")
        self.record_button.connect("clicked", lambda *_: self.start_flow())
        box.pack_start(self.record_button, False, False, 0)

        self.summary = Gtk.Label()
        self.summary.get_style_context().add_class("dim")
        box.pack_start(self.summary, False, False, 0)
        self._update_summary()
        return box

    def _mode_button(self, icon, text):
        button = Gtk.RadioButton(draw_indicator=False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DND), False, False, 0)
        inner.pack_start(Gtk.Label(label=text), False, False, 0)
        button.add(inner)
        button.get_style_context().add_class("big-toggle")
        return button

    def _option_toggle(self, icon, tooltip, key):
        button = Gtk.ToggleButton(
            image=Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR),
            tooltip_text=tooltip,
            active=bool(self.settings[key]),
        )
        button.get_style_context().add_class("round-button")
        button.get_style_context().add_class("option-toggle")
        button.connect("toggled", self._on_option_toggled, key)
        return button

    def _device_row(self, grid, row, icon, tooltip, key):
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        image.set_tooltip_text(tooltip)
        combo = Gtk.ComboBoxText(hexpand=True, tooltip_text=tooltip)
        cell = combo.get_cells()[0]
        cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        cell.set_property("width-chars", 20)
        handler = combo.connect("changed", self._on_device_chosen, key)
        grid.attach(image, 0, row, 1, 1)
        grid.attach(combo, 1, row, 1, 1)
        return combo, handler

    def _build_settings_window(self):
        # Ventana propia (no un popover): en GTK3 los popovers no pueden salir
        # de la ventana principal y se recortaban.
        dialog = Gtk.Window(
            title="Ajustes", transient_for=self, destroy_with_parent=True,
            resizable=False, skip_taskbar_hint=True,
            type_hint=Gdk.WindowTypeHint.DIALOG,
            window_position=Gtk.WindowPosition.CENTER_ON_PARENT,
        )
        dialog.connect("delete-event", lambda w, _e: w.hide_on_delete())
        dialog.connect(
            "key-press-event",
            lambda w, e: e.keyval == Gdk.KEY_Escape and (w.hide() or True),
        )
        grid = Gtk.Grid(row_spacing=4, margin=18, width_request=320)

        def row(i, text, widget):
            label = Gtk.Label(label=text, xalign=0)
            if i > 0:
                label.set_margin_top(8)
            widget.set_hexpand(True)
            grid.attach(label, 0, i * 2, 1, 1)
            grid.attach(widget, 0, i * 2 + 1, 1, 1)

        fmt = Gtk.ComboBoxText()
        for name in FORMATS:
            fmt.append(name, name)
        fmt.set_active_id(self.settings["format"])
        fmt.connect("changed", lambda c: self._set("format", c.get_active_id()))
        row(0, "Formato", fmt)

        fps = Gtk.ComboBoxText()
        for value in (24, 30, 60):
            fps.append(str(value), "%d fps" % value)
        fps.set_active_id(str(self.settings["framerate"]))
        fps.connect("changed", lambda c: self._set("framerate", int(c.get_active_id())))
        row(1, "Cuadros por segundo", fps)

        delay = Gtk.ComboBoxText()
        for value in (0, 3, 5, 10):
            delay.append(str(value), "Sin espera" if value == 0 else "%d segundos" % value)
        delay.set_active_id(str(self.settings["delay"]))
        delay.connect("changed", lambda c: self._set("delay", int(c.get_active_id())))
        row(2, "Espera antes de grabar", delay)

        folder = Gtk.FileChooserButton(
            title="Carpeta para las grabaciones", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        folder.set_filename(self.settings["folder"])
        folder.connect("file-set", lambda b: self._set("folder", b.get_filename()))
        row(3, "Guardar en", folder)

        hide = Gtk.Switch(active=self.settings["hide_window"], valign=Gtk.Align.CENTER)
        hide.connect("notify::active", lambda s, _p: self._set("hide_window", s.get_active()))
        hide_row = Gtk.Box(spacing=12, margin_top=12)
        hide_row.pack_start(Gtk.Label(label="Minimizar al grabar", xalign=0), True, True, 0)
        hide_row.pack_end(hide, False, False, 0)
        grid.attach(hide_row, 0, 10, 1, 1)

        hint = Gtk.Label(
            label="Para un atajo de teclado usa el comando\n<b>grabadora --toggle</b> (empieza o detiene).",
            use_markup=True, xalign=0,
        )
        hint.get_style_context().add_class("dim")
        hint.set_margin_top(8)
        grid.attach(hint, 0, 11, 1, 1)

        grid.show_all()
        dialog.add(grid)
        return dialog

    def _build_countdown_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=24)
        self.countdown_label = Gtk.Label()
        self.countdown_label.get_style_context().add_class("countdown")
        box.pack_start(Gtk.Label(label="La grabación empieza en"), False, False, 0)
        box.pack_start(self.countdown_label, False, False, 0)
        cancel = Gtk.Button(label="Cancelar", halign=Gtk.Align.CENTER)
        cancel.get_style_context().add_class("record-button")
        cancel.connect("clicked", lambda *_: self.cancel_countdown())
        box.pack_start(cancel, False, False, 0)
        return box

    def _build_recording_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=24)
        self.status_label = Gtk.Label(label="Grabando")
        self.status_label.get_style_context().add_class("dim")
        self.timer_label = Gtk.Label(label="00:00")
        self.timer_label.get_style_context().add_class("timer")
        box.pack_start(self.status_label, False, False, 0)
        box.pack_start(self.timer_label, False, False, 0)

        buttons = Gtk.Box(spacing=16, halign=Gtk.Align.CENTER)
        self.pause_icon = Gtk.Image.new_from_icon_name(
            "media-playback-pause-symbolic", Gtk.IconSize.LARGE_TOOLBAR
        )
        self.pause_button = Gtk.Button(image=self.pause_icon, tooltip_text="Pausar")
        self.pause_button.get_style_context().add_class("round-button")
        self.pause_button.connect("clicked", lambda *_: self.toggle_pause())
        stop = Gtk.Button(
            image=Gtk.Image.new_from_icon_name(
                "media-playback-stop-symbolic", Gtk.IconSize.LARGE_TOOLBAR
            ),
            tooltip_text="Detener y guardar",
        )
        ctx = stop.get_style_context()
        ctx.add_class("round-button")
        ctx.add_class("destructive-action")
        stop.connect("clicked", lambda *_: self.stop_recording())
        buttons.pack_start(self.pause_button, False, False, 0)
        buttons.pack_start(stop, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        return box

    def _build_saving_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=32)
        spinner = Gtk.Spinner(active=True)
        spinner.set_size_request(32, 32)
        box.pack_start(spinner, False, False, 0)
        box.pack_start(Gtk.Label(label="Guardando…"), False, False, 0)
        return box

    # ---------- Ajustes ----------

    def _set(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)
        self._update_summary()

    def _on_mode_toggled(self, button):
        self._set("mode", "full" if button.get_active() else "area")

    def _on_option_toggled(self, button, key):
        self._set(key, button.get_active())
        self._update_device_sensitivity()

    def _update_device_sensitivity(self):
        self.desktop_combo.set_sensitive(self.settings["desktop_audio"])
        self.mic_combo.set_sensitive(self.settings["microphone"])

    def _refresh_devices(self):
        self._fill_device_combo(
            self.desktop_combo, self.desktop_combo_handler, "desktop_device",
            list_outputs(), pactl("get-default-sink"),
        )
        self._fill_device_combo(
            self.mic_combo, self.mic_combo_handler, "microphone_device",
            list_microphones(), pactl("get-default-source"),
        )

    def _fill_device_combo(self, combo, handler, key, devices, default):
        if combo.get_property("popup-shown"):
            return  # no tocar la lista mientras está abierta
        combo.handler_block(handler)
        combo.remove_all()
        default_desc = dict(devices).get(default)
        combo.append(
            "", "Predeterminado: %s" % default_desc if default_desc else "Predeterminado del sistema"
        )
        chosen = self.settings[key]
        found = False
        for name, description in devices:
            combo.append(name, description)
            found = found or name == chosen
        if chosen and not found:
            combo.append(chosen, "No conectado: " + chosen)
        combo.set_active_id(chosen)
        combo.handler_unblock(handler)

    def _on_device_chosen(self, combo, key):
        device = combo.get_active_id()
        if device is None:
            return
        self._set(key, device)

    def _update_summary(self):
        parts = [self.settings["format"], "%d fps" % self.settings["framerate"]]
        if self.settings["delay"]:
            parts.append("espera de %d s" % self.settings["delay"])
        self.summary.set_text(" · ".join(parts))

    # ---------- Flujo de grabación ----------

    @property
    def busy(self):
        return self.recorder is not None or self.countdown_id is not None

    def toggle(self):
        if self.recorder is not None:
            self.stop_recording()
        elif self.countdown_id is not None:
            self.cancel_countdown()
        else:
            self.start_flow()

    def start_flow(self):
        if self.busy:
            return
        if self.settings["mode"] == "area":
            self.hide()
            # Esperar a que la ventana se oculte antes de mostrar la capa.
            GLib.timeout_add(200, self._open_selector)
        else:
            self._begin_countdown(None)

    def _open_selector(self):
        AreaSelector(self._on_area_selected).show_all()
        return False

    def _on_area_selected(self, rect):
        self.show()
        if rect is None:
            self.present()
            return
        self.settings["last_area"] = list(rect)
        save_settings(self.settings)
        self._begin_countdown(rect)

    def _begin_countdown(self, area):
        self.pending_area = area
        delay = int(self.settings["delay"])
        if delay <= 0:
            self._hide_then_record()
            return
        self.countdown_left = delay
        self.countdown_label.set_text(str(delay))
        self.stack.set_visible_child_name("countdown")
        self.present()
        self.countdown_id = GLib.timeout_add_seconds(1, self._countdown_tick)

    def _countdown_tick(self):
        self.countdown_left -= 1
        if self.countdown_left > 0:
            self.countdown_label.set_text(str(self.countdown_left))
            return True
        self.countdown_id = None
        self._hide_then_record()
        return False

    def cancel_countdown(self):
        if self.countdown_id is not None:
            GLib.source_remove(self.countdown_id)
            self.countdown_id = None
        self.stack.set_visible_child_name("main")

    def _hide_then_record(self):
        if self.settings["hide_window"]:
            self.iconify()
            # Dar tiempo a la animación de minimizar para que no salga en el video.
            GLib.timeout_add(450, self._start_recording)
        else:
            self._start_recording()

    def _start_recording(self):
        folder = self.settings["folder"]
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._show_error("No se puede usar la carpeta %s" % folder, str(exc))
            return False
        ext = FORMATS[self.settings["format"]][0]
        name = time.strftime("Grabación_%Y-%m-%d_%H-%M-%S") + "." + ext
        path = os.path.join(folder, name)
        try:
            self.recorder = Recorder(
                self.settings, self.pending_area, path,
                self._on_recorder_error, self._on_recorder_finished,
            )
            self.recorder.start()
        except (GLib.Error, RuntimeError) as exc:
            self.recorder = None
            self._show_error("No se pudo iniciar la grabación", str(exc))
            return False

        self.paused = False
        self.elapsed = 0.0
        self.resumed_at = time.monotonic()
        self.status_label.set_text("Grabando")
        self.pause_icon.set_from_icon_name(
            "media-playback-pause-symbolic", Gtk.IconSize.LARGE_TOOLBAR
        )
        self.pause_button.set_tooltip_text("Pausar")
        self.timer_label.set_text("00:00")
        self.stack.set_visible_child_name("recording")
        self.tick_id = GLib.timeout_add(250, self._tick)
        self._tick()
        return False

    def _current_elapsed(self):
        if self.paused or self.resumed_at is None:
            return self.elapsed
        return self.elapsed + time.monotonic() - self.resumed_at

    def _tick(self):
        text = format_time(self._current_elapsed())
        self.timer_label.set_text(text)
        self.set_title(("⏸ " if self.paused else "● ") + text + " — Grabadora")
        return True

    def toggle_pause(self):
        if self.recorder is None:
            return
        if self.paused:
            self.recorder.resume()
            self.paused = False
            self.resumed_at = time.monotonic()
            self.status_label.set_text("Grabando")
            self.pause_icon.set_from_icon_name(
                "media-playback-pause-symbolic", Gtk.IconSize.LARGE_TOOLBAR
            )
            self.pause_button.set_tooltip_text("Pausar")
        else:
            self.elapsed = self._current_elapsed()
            self.recorder.pause()
            self.paused = True
            self.status_label.set_text("En pausa")
            self.pause_icon.set_from_icon_name(
                "media-playback-start-symbolic", Gtk.IconSize.LARGE_TOOLBAR
            )
            self.pause_button.set_tooltip_text("Continuar")
        self._tick()

    def stop_recording(self):
        if self.recorder is None:
            return
        self._stop_ticking()
        self.stack.set_visible_child_name("saving")
        self.recorder.stop()

    def _stop_ticking(self):
        if self.tick_id is not None:
            GLib.source_remove(self.tick_id)
            self.tick_id = None
        self.set_title("Grabadora")

    def _back_to_main(self):
        self._stop_ticking()
        self.recorder = None
        self.stack.set_visible_child_name("main")
        self.deiconify()
        self.present()

    def _on_recorder_finished(self, path):
        self._back_to_main()
        notify_saved(path)

    def _on_recorder_error(self, message, debug):
        self._back_to_main()
        self._show_error("Error durante la grabación", message, debug)

    def _show_error(self, title, message, debug=None):
        self.deiconify()
        self.present()
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE, text=title,
        )
        dialog.format_secondary_text(message)
        if debug:
            print(debug, file=sys.stderr)
        dialog.run()
        dialog.destroy()

    def _on_delete(self, *_):
        if self.recorder is not None:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE, text="Hay una grabación en curso",
            )
            dialog.format_secondary_text("¿Quieres guardarla antes de cerrar?")
            dialog.add_buttons(
                "Descartar", Gtk.ResponseType.REJECT,
                "Cancelar", Gtk.ResponseType.CANCEL,
                "Guardar", Gtk.ResponseType.ACCEPT,
            )
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.ACCEPT:
                self.recorder.on_finished = lambda path: (notify_saved(path), self.app.quit())
                self.stop_recording()
                return True
            if response == Gtk.ResponseType.REJECT:
                path = self.recorder.path
                self.recorder.cancel()
                try:
                    os.remove(path)
                except OSError:
                    pass
                return False
            return True
        return False


def notify_saved(path):
    """Notificación con botones para abrir el video o mostrarlo en la carpeta."""

    def run():
        try:
            result = subprocess.run(
                [
                    "notify-send", "--app-name=Grabadora", "--icon=media-record",
                    "--action=abrir=Abrir", "--action=carpeta=Mostrar en carpeta",
                    "Grabación guardada", os.path.basename(path),
                ],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return
        choice = result.stdout.strip()
        if choice == "abrir":
            subprocess.Popen(["xdg-open", path])
        elif choice == "carpeta":
            uri = Gio.File.new_for_path(path).get_uri()
            shown = subprocess.run(
                [
                    "dbus-send", "--session", "--type=method_call",
                    "--dest=org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1.ShowItems",
                    "array:string:" + uri, "string:",
                ],
                capture_output=True,
            )
            if shown.returncode != 0:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])

    threading.Thread(target=run, daemon=True).start()


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = load_settings()
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gst.init(None)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _ensure_window(self):
        if self.window is None:
            self.window = MainWindow(self)
            self.window.show_all()
        return self.window

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        first_run = self.window is None
        window = self._ensure_window()
        if "--toggle" in args:
            if first_run:
                # Recién abierta: esperar a que la ventana se dibuje.
                GLib.timeout_add(300, lambda: window.toggle() and False)
            else:
                window.toggle()
        else:
            window.deiconify()
            window.present()
        return 0


if __name__ == "__main__":
    # Hace que la ventana coincida con su lanzador del menú (StartupWMClass).
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Grabadora")
    Gdk.set_program_class(APP_ID)
    sys.exit(Application().run(sys.argv))

# Simple Recording

A small, no-frills screen recorder for Linux (X11), inspired by Kooha.
Record the whole screen or a region, with system audio and/or microphone, in one click.

The app itself is called **Grabadora** and its interface is in Spanish.

![Main window](docs/captura.png)

## Platform support

| Platform | Supported |
|---|---|
| Linux, X11 session (Linux Mint Cinnamon, Ubuntu on Xorg, …) | ✅ |
| Linux, Wayland session | ❌ |
| Windows / macOS | ❌ |

It relies on Linux-specific pieces: X11 screen capture (`ximagesrc`), PulseAudio/PipeWire audio (`pulsesrc`, `pactl`), `notify-send`, `xdg-open` and D-Bus.

## Features

- **Full screen or area**: in *Área* mode, drag a rectangle with the mouse (Esc or right-click cancels).
- **System audio** and **microphone**, together or separately (mixed into a single track).
- **Audio devices shown right in the main window**:
  - 🔊 *System audio*: the output (speakers, headphones…) whose sound is recorded.
  - 🎤 *Microphone*: the input that is recorded.
  - The *Predeterminado* (default) entry shows the real name of the device that will be used.
  - If the chosen device is unplugged, the system default is used instead.
  - The lists refresh when you return to the window, so newly plugged devices show up.
  - Each list is greyed out when its audio toggle is off.
- **Show or hide the mouse pointer**.
- **Pause / resume** and **stop & save**, with an on-screen timer.
- Desktop notification when saved, with *Open* and *Show in folder* buttons.
- `--toggle` command line flag to start/stop from a keyboard shortcut.

## Settings (☰ menu)

| Setting | Options |
|---|---|
| Format | MP4 (H.264 + AAC), MKV (H.264 + Opus), WebM (VP8 + Opus) |
| Frame rate | 24, 30, 60 fps |
| Delay before recording | none, 3, 5, 10 seconds |
| Save to | Output folder (defaults to `~/Videos`) |
| Minimize while recording | Hides the window so it doesn't appear in the video |

Settings are stored in `~/.config/grabadora/ajustes.json`.

## Requirements

- An **X11** session.
- Python 3 with PyGObject (GTK 3).
- GStreamer 1.0 with the *good*, *bad*, *ugly* and *libav* plugins.
- PulseAudio or PipeWire (with `pactl`).
- `notify-send` (libnotify).

On Linux Mint / Ubuntu / Debian:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav gstreamer1.0-pulseaudio pulseaudio-utils libnotify-bin
```

## Installation

For the current user, no sudo needed:

```bash
./instalar.sh
```

This installs the app in `~/.local/share/grabadora`, a `grabadora` command in `~/.local/bin`, and a **Grabadora** launcher in the applications menu.

Uninstall:

```bash
./instalar.sh --desinstalar
```

Or run it without installing:

```bash
python3 grabadora.py
```

## Usage

```bash
grabadora            # open the window
grabadora --toggle   # start or stop a recording
```

Bind `grabadora --toggle` to a **keyboard shortcut** (System Settings → Keyboard → Shortcuts → Custom). If a countdown is running, it cancels it.

Recordings are saved as `Grabación_YYYY-MM-DD_HH-MM-SS.<ext>`.

## How it works

- Video is captured with GStreamer's `ximagesrc` and encoded according to the chosen format.
- System audio is recorded from the *monitor* of the selected output (`<sink>.monitor`), and the microphone from the selected input, both via `pulsesrc`. When both are enabled they are mixed with `audiomixer`.
- Audio devices are listed with `pactl -f json list sinks|sources`.
- Everything lives in a single file, [`grabadora.py`](grabadora.py).

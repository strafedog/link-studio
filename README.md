# Link Studio

Link Studio is an unofficial, open-source Linux control application for Insta360 Link webcams. It
is hardware-tested with the **Insta360 Link 2** (`2e1a:4c04`) on Arch Linux and combines direct,
cooperative V4L2/UVC control with a GTK4/libadwaita interface. Video, audio, AI inference, remote
control, and configuration remain local unless you explicitly open an Insta360 download link.

Version 1.0 completes the safe Linux application scope. It implements every official-client area
that can be reproduced through validated Link 2 controls or a local Linux equivalent. The few
vendor-only operations that cannot safely be cloned are identified in the
[parity contract](docs/PARITY.md); they are external protocol or service boundaries, not unfinished
buttons hidden in the application.

Project documentation lives at <https://jweaver60.github.io/link-studio/>. The canonical public
source repository is [`jweaver60/link-studio`](https://github.com/jweaver60/link-studio).

## Features

- Live 720p, 1080p, 1440p, and 4K preview at every frame rate currently advertised by the camera
- PNG screenshots and H.264/AAC MP4 recording, with selectable destinations and microphone
- Pan, tilt, center, 1–4× zoom, autofocus/manual focus, mirror, privacy, and output rotation
- Hardware AI Tracking, Whiteboard, Overhead, and DeskView modes
- Hardware framing, tracking speed, gesture zoom, HDR, exposure, white balance, anti-flicker, and
  complete color controls
- Local single-person and group tracking with a tracking boundary and up to six pause areas
- Local background blur, bokeh, background replacement, green screen, beautify, makeup, and Smart
  Whiteboard, powered by bundled MediaPipe models
- Link 2 Voice Focus, Voice Suppression, and Music Balance microphone modes; PipeWire mute and volume
- Processed virtual-camera output through an optional `v4l2loopback` device
- Ten full scene presets with rename/update/delete/default-on-startup lifecycle
- Twenty independent color templates
- Token-authenticated LAN phone remote with QR pairing
- Compact toolbar and 14 compositor-managed global actions through the XDG GlobalShortcuts portal
- Teleprompter scripts with import/editing, speed, size, colors, opacity, guide, loop, and countdown
- Local voice-note sessions with pause, timestamped notes and screenshot markers, optional offline
  Whisper transcription, and Markdown summaries
- Firmware/build reporting, JSON diagnostics, rotating logs, and privacy-conscious support bundles
- Automatic light/dark appearance, with optional live Omarchy palette support

## Run from the repository

On Arch Linux, install the native stack if it is not already present: `gtk4`, `libadwaita`,
`python-gobject`, `gstreamer`, `gst-plugins-base`, `gst-plugins-good`, `gst-plugins-bad`, `gst-libav`,
and `v4l-utils`. The launcher creates a local environment anchored to the system Python so
Arch's GTK bindings and Link Studio's Python dependencies are both available.

```bash
./scripts/run-dev
```

Useful diagnostics:

```bash
./scripts/run-dev --diagnose
./scripts/run-dev --version
./scripts/run-dev --device /dev/video2
./scripts/run-dev --no-preview
```

`--diagnose` is read-only and prints detected cameras, camera state, firmware information, and the
active appearance palette as JSON.

## Install for the current user

```bash
./scripts/install-user
```

This installs an isolated environment below `${XDG_DATA_HOME:-~/.local/share}/link-studio`, creates
`link-studio` in `${XDG_BIN_HOME:-~/.local/bin}`, and installs desktop/AppStream entries. It does
not require root or edit desktop configuration.

The maintained Arch package recipe lives under `packaging/aur`. New AUR account registration is
currently closed upstream, so its initial AUR submission is pending. Build the same package locally
in the meantime:

```bash
yay -S python-mediapipe
cd packaging/aur
makepkg -si
```

Every package source is publicly downloadable from the GitHub releases.

Optional local integrations:

```bash
link-studio-setup-virtual-camera
link-studio-setup-local-ai
```

The helpers use `pacman` for the required Arch packages. The virtual-camera helper loads a temporary
`/dev/video20` device. The AI helper installs `whisper-cpp` and verifies the downloaded multilingual
base model against a pinned SHA-256 checksum.

## Scripting and automation

`link-studio ctl` changes the camera from a shell, a compositor binding or a script. With the window
open it goes through the window (over `$XDG_RUNTIME_DIR/link-studio/ctl.sock`) so the controls stay
in step; otherwise it talks to the camera directly.

```bash
link-studio ctl presets                 # list scene presets, * = default
link-studio ctl preset "Pete CloudHound"   # by name or number
link-studio ctl mode tracking           # normal, tracking, whiteboard, deskview, ...
link-studio ctl framing head
link-studio ctl zoom +20                # or an absolute 100-400
link-studio ctl move left 10
link-studio ctl hdr toggle
link-studio ctl state                   # JSON
```

`link-studio daemon`, run as the `link-studio-daemon` systemd user service, does three things:

- **Restore on connect.** When the camera appears (plug in, dock, login) it applies the default
  scene preset, the starred one. A 2C Pro without a default preset gets its remembered crop back.
  AI modes need a live stream, so the mode itself is left to the call preset.
- **Call presets.** When another program (a browser call, OBS) holds the camera, or a
  v4l2loopback virtual camera fed by Link Studio, for 3 seconds, it applies the preset chosen
  under Presets → Calls → When a call starts. Eight seconds after the camera is released it
  applies "When a call ends". The choices live in `~/.config/link-studio/automation.json`.
- **Headless virtual camera.** With a v4l2loopback device loaded (`exclusive_caps=1`), the daemon
  keeps it listed as a camera by sending a placeholder (the effect's background image) at 2 fps.
  When a call app opens it, the physical camera starts at 720p30 with the software effects of the
  call preset (else the default preset), e.g. background replacement with Robust Video Matting,
  and stops six seconds after the app lets go. It stays off the device while a Link Studio window
  is open, so the window's own Virtual camera switch still works. Turn it off with
  `"virtual_camera": false` in `automation.json`.

```bash
systemctl --user enable --now link-studio-daemon
journalctl --user -u link-studio-daemon -f
```

## Desktop appearance

Link Studio follows the normal libadwaita light/dark appearance. On Omarchy it also respects the
active color palette automatically; no theme hook or user configuration is required.

Other theming tools (Noctalia, matugen, pywal templates) can write an Omarchy-style
`~/.config/link-studio/colors.toml` (`mode`, `accent`, `background`, `foreground`, ...). It takes
priority over Omarchy and is reloaded live when it changes.

Global actions use the desktop portal rather than editing Hyprland bindings. Enable them under
Device → Application. Desktops with a portal configuration dialog can assign keys there. On
Hyprland, list the registered action names with `hyprctl globalshortcuts`, then use its `global`
dispatcher in `~/.config/hypr/hyprland.lua`, for example:

```lua
hl.bind("SUPER + SHIFT + R", hl.dsp.global("io.github.linkstudio.LinkStudio:record"))
hl.bind("SUPER + SHIFT + S", hl.dsp.global("io.github.linkstudio.LinkStudio:screenshot"))
```

## Safety model

- The backend never detaches `uvcvideo`; this avoids a known Link 2 failure mode where detach/rebind
  cycles can leave USB present without a working video node.
- Hardware mode writes are serialized, paced, and read back where the protocol supports it.
- Unknown firmware-update, factory-reset, and compatibility-mode writes are never guessed. The
  Device page reports firmware and links to Insta360's recovery-safe U-Disk flow.
- Privacy mode is the camera's gimbal-down mode, not a physical shutter.
- Phone control is LAN-only, uses an unguessable per-session token, and is off by default.
- MediaPipe effects and Whisper transcription run locally.

## Architecture

```text
GTK4 / libadwaita application
 ├─ V4L2 standard controls ─────────── gimbal, zoom, image, focus
 ├─ validated UVC extension controls ─ AI modes, HDR, exposure, privacy, audio
 ├─ GStreamer pipelines ───────────── preview, screenshots, MP4, virtual camera
 ├─ MediaPipe + OpenCV ────────────── effects, face tracking, Smart Whiteboard
 ├─ local tools ───────────────────── phone remote, teleprompter, meetings
 ├─ XDG desktop portals ───────────── compositor-managed global actions
 └─ XDG JSON stores ───────────────── scenes, scripts, output locations, settings
```

## Development and verification

```bash
.venv/bin/python -m unittest discover -v
uvx ruff check src tests scripts
.venv/bin/python -m compileall -q src tests
./scripts/run-dev --diagnose
```

Link Studio is MIT-licensed. Third-party model and research attribution is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Insta360 and Insta360 Link are trademarks of their
respective owner; this project is not affiliated with or endorsed by Insta360.

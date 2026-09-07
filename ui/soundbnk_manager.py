# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------------------------
#   Reflex Package Tool — A tool for working with game archives for MX vs ATV Reflex in the .package format.
#   Copyright (C) 2026  Daniil Korochansky
#
#   This file is part of Reflex Package Tool.
#
#   Reflex Package Tool is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   Reflex Package Tool is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with Reflex Package Tool.  If not, see <https://www.gnu.org/licenses/>.
# -------------------------------------------------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import os
import sys
import shutil
import tempfile
import time
import wave
import ctypes
import threading
from pathlib import Path

import wx

from core.reflex_soundbnk import build_fsb, extract_wav, load_soundbnk, safe_name


APP_NAME = "Sound Bank Manager"
SESSION_ROOT = Path(tempfile.gettempdir()) / "ReflexSoundBNK"

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

icons_folder = resource_path("icons")
sm_open = os.path.join(icons_folder, "open.png")
sm_build = os.path.join(icons_folder, "build.png")
sm_close = os.path.join(icons_folder, "close.png")
sm_replace = os.path.join(icons_folder, "replace.png")
sm_play = os.path.join(icons_folder, "play.png")
sm_stop = os.path.join(icons_folder, "stop.png")
sm_extract = os.path.join(icons_folder, "extract.png")

def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cleanup_old_sessions() -> None:
    """Remove sessions left by crashed/dead editor processes without prompting."""
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)

    for path in SESSION_ROOT.glob("session_*"):
        if not path.is_dir():
            continue

        keep = False
        meta = path / "session.json"
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                keep = _pid_is_alive(int(data.get("pid", -1)))
            except Exception:
                keep = False

        if not keep:
            shutil.rmtree(path, ignore_errors=True)


def create_session(source: Path) -> Path:
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="session_", dir=SESSION_ROOT))
    (path / "original").mkdir()
    (path / "working").mkdir()

    meta = {
        "pid": os.getpid(),
        "created": time.time(),
        "source": str(source),
    }
    (path / "session.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        frames = w.getnframes()
    return frames / rate if rate else 0.0


def channels_text(channels: int) -> str:
    return "Mono" if channels == 1 else "Stereo" if channels == 2 else str(channels)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class SoundBankManager(wx.Dialog):
    def __init__(self, parent, soundbnk_path: str | Path | None = None):
        super().__init__(
            parent,
            title=APP_NAME,
            size=(900, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        
        self.SetSizeHints(wx.Size(800, 300), wx.DefaultSize)
        self.parent = parent
        self.source_path: Path | None = None
        self.session_dir: Path | None = None
        self.entries: list[dict] = []
        self.original_hashes: dict[str, str] = {}
        self.selected_index: int | None = None

        self.playing_index: int | None = None
        self.playing_path: Path | None = None
        self.playing_started_at = 0.0
        self.playing_duration = 0.0
        self._seeking = False
        self._opening = False
        self._closing = False

        # Windows MCI is used for playback because it supports both
        # position reporting and seeking reliably for WAV files.
        self._mci_alias = f"reflex_sbnk_{id(self):x}"

        self._build_ui()
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        if soundbnk_path:
            wx.CallAfter(self.open_soundbnk, Path(soundbnk_path))

    # ----------------------- UI---------------------------

    def _build_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)

        self.toolbar = wx.ToolBar(self, style=wx.TB_HORIZONTAL | wx.TB_TEXT | wx.TB_FLAT | wx.TB_NODIVIDER)
        self.tb_open = self.toolbar.AddTool(
            wx.ID_OPEN, "Open...", wx.Bitmap(sm_open,wx.BITMAP_TYPE_PNG),
            shortHelp="Open the sound bank file",
        )

        self.tb_rebuild = self.toolbar.AddTool(
            wx.ID_SAVE, "Build...", wx.Bitmap(sm_build,wx.BITMAP_TYPE_PNG),
            shortHelp="Build a new sound bank",
        )
        self.toolbar.AddSeparator()
        self.extract_tool_id = wx.NewIdRef()
        self.tb_extract = self.toolbar.AddTool(
            self.extract_tool_id, "Extract", wx.Bitmap(sm_extract,wx.BITMAP_TYPE_PNG),
            shortHelp="Extract the selected audio file",
        )
        self.tb_extract.Enable(False)
        
        self.replace_tool_id = wx.NewIdRef()
        self.tb_replace = self.toolbar.AddTool(
            self.replace_tool_id, "Replace...", wx.Bitmap(sm_replace,wx.BITMAP_TYPE_PNG),
            shortHelp="Replace the selected audio file",
        )
        
        self.toolbar.AddSeparator()
        self.tb_play = self.toolbar.AddTool(
            wx.ID_ANY, "Play", wx.Bitmap(sm_play,wx.BITMAP_TYPE_PNG),
        )
        self.toolbar.AddSeparator()
        self.tb_close = self.toolbar.AddTool(
            wx.ID_CLOSE, "Close", wx.Bitmap(sm_close,wx.BITMAP_TYPE_PNG),
            shortHelp="Close sound bank manager",
        )
        self.toolbar.Realize()
        root.Add(self.toolbar, 0, wx.EXPAND)

        self.list_ctrl = wx.ListCtrl(
            self,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.AppendColumn("Name", wx.LIST_FORMAT_LEFT, 280)
        self.list_ctrl.AppendColumn("Format", wx.LIST_FORMAT_LEFT, 150)
        self.list_ctrl.AppendColumn("Rate", wx.LIST_FORMAT_RIGHT, 90)
        self.list_ctrl.AppendColumn("Channels", wx.LIST_FORMAT_LEFT, 100)
        self.list_ctrl.AppendColumn("Status", wx.LIST_FORMAT_LEFT, 110)
        root.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Bottom area.  In normal state only the soundbank name is visible.
        self.bottom_panel = wx.Panel(self)
        self.bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.soundbnk_label = wx.StaticText(self.bottom_panel, label="No sound bank opened")
        self.playing_label = wx.StaticText(self.bottom_panel, label="")
        self.current_time_label = wx.StaticText(self.bottom_panel, label="0:00")
        self.duration_label = wx.StaticText(self.bottom_panel, label="0:00")
        self.slider = wx.Slider(
            self.bottom_panel,
            value=0,
            minValue=0,
            maxValue=1000,
            size=wx.Size( 280,-1 ),
            style=wx.SL_HORIZONTAL,
        )

        self.bottom_sizer.Add(self.soundbnk_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM | wx.TOP, 8)
        self.bottom_sizer.Add(self.playing_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM | wx.TOP, 8)
        self.bottom_sizer.AddStretchSpacer(1)
        self.bottom_sizer.Add(self.current_time_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.bottom_sizer.Add(self.slider, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 5)
        self.bottom_sizer.Add(self.duration_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.bottom_panel.SetSizer(self.bottom_sizer)
        root.Add(self.bottom_panel, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 5)

        self.SetSizer(root)

        self.Bind(wx.EVT_TOOL, self._on_open, id=wx.ID_OPEN)
        self.Bind(wx.EVT_TOOL, self._on_replace, id=self.replace_tool_id)
        self.Bind(wx.EVT_TOOL, self._on_extract, id=self.extract_tool_id)
        self.Bind(wx.EVT_TOOL, self._on_play_stop, id=self.tb_play.GetId())
        self.Bind(wx.EVT_TOOL, self._on_rebuild, id=wx.ID_SAVE)
        self.Bind(wx.EVT_TOOL, self._on_close_button, id=wx.ID_CLOSE)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_item_selected)
        self.slider.Bind(wx.EVT_SLIDER, self._on_slider)

        self._set_playback_ui(False)
        self._update_enabled_state()

    # -------------------------------------------------------------- opening

    def _create_open_progress(self):
        """Create the buttonless progress window used while opening a bank."""
        dlg = wx.Dialog(
            self,
            title=APP_NAME,
            size=(430, 125),
            style=wx.CAPTION | wx.FRAME_NO_TASKBAR,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(dlg, label="Opening the sound bank...")
        gauge = wx.Gauge(dlg, range=100)
        sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 18)
        sizer.Add(gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 18)
        dlg.SetSizer(sizer)
        dlg.CentreOnParent()
        dlg.Show()
        return dlg, gauge

    def open_soundbnk(self, path: Path):
        path = Path(path)
        if not path.exists():
            self._error(f"File not found:\n{path}")
            return
        if self._opening:
            return

        # Stop the previous bank on the UI thread before starting background work.
        self.stop_playback()
        self._destroy_session()

        progress_dlg, progress_gauge = self._create_open_progress()
        self._opening = True
        self._open_progress = progress_dlg
        self._update_enabled_state()

        def worker():
            session = None
            try:
                if self._closing:
                    return
                data, fsb, info = load_soundbnk(path)
                session = create_session(path)
                original_dir = session / "original"
                working_dir = session / "working"

                # Decode all existing samples and prepare editable working copies.
                extract_wav(fsb, info, original_dir)
                for wav in original_dir.glob("*.wav"):
                    shutil.copy2(wav, working_dir / wav.name)

                original_hashes = {
                    p.name: file_hash(p) for p in original_dir.glob("*.wav")
                }

                if self._closing:
                    if session is not None:
                        shutil.rmtree(session, ignore_errors=True)
                    return
                wx.CallAfter(self._finish_open_soundbnk,path, session, info, original_hashes, progress_dlg)
            except Exception as exc:
                if session is not None:
                    shutil.rmtree(session, ignore_errors=True)

                if not self._closing:
                    wx.CallAfter(self._open_soundbnk_failed, str(exc), progress_dlg)

        threading.Thread(target=worker, name="SoundBNKOpen", daemon=True).start()

        # Extraction uses FFmpeg for MPEG samples, so its duration is not known
        # in advance. An indeterminate gauge gives continuous visual feedback.
        def pulse(_event):
            if progress_dlg.IsShown():
                progress_gauge.Pulse()

        timer = wx.Timer(progress_dlg)
        progress_dlg._pulse_timer = timer
        progress_dlg.Bind(wx.EVT_TIMER, pulse, timer)
        timer.Start(80)

    def _finish_open_soundbnk(self, path, session, info, original_hashes, progress_dlg):
        if self._closing:
            if session is not None:
                shutil.rmtree(session, ignore_errors=True)
            return
        
        if progress_dlg.IsShown():
            progress_dlg._pulse_timer.Stop()
            progress_dlg.Destroy()

        self.source_path = path.resolve()
        self.session_dir = session
        self.entries = info["entries"]
        self.original_hashes = original_hashes
        self.selected_index = None
        self._opening = False

        self.list_ctrl.DeleteAllItems()
        for e in self.entries:
            wav_name = safe_name(e["name"], f"sound_{e['index']:03d}.wav")
            if not wav_name.lower().endswith(".wav"):
                wav_name += ".wav"
            codec = "MPEG Layer III" if e["is_mpeg"] else "PCM"
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(), wav_name)
            self.list_ctrl.SetItem(idx, 1, codec)
            self.list_ctrl.SetItem(idx, 2, f"{e['frequency']} Hz")
            self.list_ctrl.SetItem(idx, 3, channels_text(e["channels"]))
            self.list_ctrl.SetItem(idx, 4, "Original")

        self.soundbnk_label.SetLabel(path.name)
        self.playing_label.SetLabel("")
        self.Layout()
        self._update_enabled_state()

    def _open_soundbnk_failed(self, message, progress_dlg):
        if self._closing:
            return
        
        if progress_dlg.IsShown():
            progress_dlg._pulse_timer.Stop()
            progress_dlg.Destroy()
        self._opening = False
        self._destroy_session()
        self.source_path = None
        self.session_dir = None
        self.entries = []
        self._update_enabled_state()
        self._error(message)

    # --------------------------------------------------------------- list

    def _on_item_selected(self, event):
        self.selected_index = event.GetIndex()
        self._update_enabled_state()
        event.Skip()

    def _on_item_activated(self, event):
        index = event.GetIndex()
        if self.playing_index == index:
            self.stop_playback()
        else:
            self.start_playback(index)
        event.Skip()

    # ------------------------------------------------------------ playback

    def _mci(self, command: str) -> str:
        """Send a Windows MCI command and return its textual result."""
        if os.name != "nt":
            raise RuntimeError("Audio playback requires Windows MCI.")
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p
        ]
        winmm.mciSendStringW.restype = ctypes.c_uint
        result = ctypes.create_unicode_buffer(256)
        error = winmm.mciSendStringW(command, result, 256, 0)
        if error:
            # Ask MCI for a readable error string.
            errbuf = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(error, errbuf, 256)
            raise RuntimeError(errbuf.value or f"MCI error {error}")
        return result.value

    def _mci_close(self):
        if os.name != "nt":
            return
        try:
            self._mci(f"close {self._mci_alias}")
        except Exception:
            pass

    def start_playback(self, index: int):
        if not self.session_dir or not (0 <= index < len(self.entries)):
            return

        self.stop_playback()

        entry = self.entries[index]
        wav_name = safe_name(entry["name"], f"sound_{entry['index']:03d}.wav")
        if not wav_name.lower().endswith(".wav"):
            wav_name += ".wav"
        wav_path = self.session_dir / "working" / wav_name

        if not wav_path.exists():
            self._error(f"Missing WAV:\n{wav_path.name}")
            return

        try:
            duration = wav_duration(wav_path)
        except Exception as exc:
            self._error(str(exc))
            return

        try:
            # MCI paths must be quoted.
            escaped = str(wav_path).replace('"', '\\"')
            self._mci(f'open "{escaped}" type waveaudio alias {self._mci_alias}')
            length_ms = int(self._mci(f"status {self._mci_alias} length"))
            if length_ms > 0:
                duration = length_ms / 1000.0
            self._mci(f"play {self._mci_alias}")
        except Exception as exc:
            self._mci_close()
            self._error(f"Unable to play WAV:\n{wav_path.name}\n\n{exc}")
            return

        self.selected_index = index
        self.playing_index = index
        self.playing_path = wav_path
        self.playing_started_at = time.monotonic()
        self.playing_duration = duration

        max_value = max(1, int(round(duration * 1000)))
        self.slider.SetRange(0, max_value)
        self.slider.SetValue(0)
        self.current_time_label.SetLabel("0:00")
        self.duration_label.SetLabel(format_time(duration))

        self._set_playback_ui(True)
        self._set_play_button_state(True)
        self._timer.Start(50)

    def stop_playback(self):
        if hasattr(self, "_timer"):
            self._timer.Stop()

        self._mci_close()

        self.playing_index = None
        self.playing_path = None
        self.playing_started_at = 0.0
        self.playing_duration = 0.0
        self._seeking = False

        if hasattr(self, "slider"):
            self.slider.SetRange(0, 1)
            self.slider.SetValue(0)
        if hasattr(self, "current_time_label"):
            self.current_time_label.SetLabel("0:00")
        if hasattr(self, "duration_label"):
            self.duration_label.SetLabel("0:00")

        if hasattr(self, "toolbar"):
            self._set_play_button_state(False)
        if hasattr(self, "bottom_panel"):
            self._set_playback_ui(False)

    def _set_play_button_state(self, playing: bool):
        """Update the toolbar playback button between Play and Stop."""
        if not hasattr(self, "tb_play") or not hasattr(self, "toolbar"):
            return

        label = "Stop" if playing else "Play"
        self.tb_play.SetLabel(label)
        self.tb_play.SetShortHelp(label)

        if playing:
            self.toolbar.SetToolNormalBitmap(self.tb_play.Id, wx.Bitmap(sm_stop,wx.BITMAP_TYPE_PNG))
        else:
            self.toolbar.SetToolNormalBitmap(self.tb_play.Id, wx.Bitmap(sm_play,wx.BITMAP_TYPE_PNG))
            
        self.toolbar.Realize()
        self._update_enabled_state()

    def _set_playback_ui(self, playing: bool):
        """Show the normal bank label or the playback controls."""
        self.soundbnk_label.Show(not playing)
        self.playing_label.Show(playing)
        self.current_time_label.Show(playing)
        self.slider.Show(playing)
        self.duration_label.Show(playing)

        if playing and self.playing_index is not None:
            name = self.list_ctrl.GetItemText(self.playing_index)
            self.playing_label.SetLabel(f"Playing: {name}")
            self.duration_label.SetLabel(format_time(self.playing_duration))

        self.bottom_panel.Layout()
        self.Layout()

    def _on_play_stop(self, event):
        if self.playing_index is not None:
            self.stop_playback()
        elif self.selected_index is not None:
            self.start_playback(self.selected_index)

    def _on_timer(self, event):
        if self.playing_index is None:
            return

        try:
            position_ms = int(self._mci(f"status {self._mci_alias} position"))
            mode = self._mci(f"status {self._mci_alias} mode").strip().lower()
        except Exception:
            self.stop_playback()
            return

        length_ms = max(1, int(round(self.playing_duration * 1000)))

        # MCI reports stopped at the natural end.
        if mode == "stopped" or position_ms >= length_ms:
            self.stop_playback()
            return

        if not self._seeking:
            self.slider.SetValue(max(0, min(self.slider.GetMax(), position_ms)))
        self.current_time_label.SetLabel(format_time(position_ms / 1000.0))

    def _on_slider(self, event):
        if self.playing_index is None:
            event.Skip()
            return

        position_ms = self.slider.GetValue()
        self._seeking = True
        try:
            # Restart from the requested position. MCI's play command with
            # 'from' gives sample-accurate-enough millisecond seeking for UI use.
            self._mci(f"stop {self._mci_alias}")
            self._mci(f"play {self._mci_alias} from {position_ms}")
            self.current_time_label.SetLabel(format_time(position_ms / 1000.0))
        except Exception as exc:
            self._error(f"Unable to seek playback:\n{exc}")
        finally:
            self._seeking = False
        event.Skip()

    # -------------------------------------------------------------- extract

    def _on_extract(self, event):
        if self.selected_index is None or not self.session_dir:
            return

        entry = self.entries[self.selected_index]

        wav_name = safe_name(
            entry["name"],
            f"sound_{entry['index']:03d}.wav"
        )
        if not wav_name.lower().endswith(".wav"):
            wav_name += ".wav"

        source = self.session_dir / "working" / wav_name

        if not source.exists():
            self._error(
                f"Audio file is not available:\n\n{wav_name}"
            )
            return

        with wx.FileDialog(
            self,
            "Extract WAV",
            defaultFile=wav_name,
            wildcard="WAV audio (*.wav)|*.wav|All files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return

            destination = Path(dlg.GetPath())

        try:
            shutil.copy2(source, destination)

            wx.MessageBox(
                f"Audio file extracted successfully:\n\n{destination}",
                "Extract WAV",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )

        except Exception as exc:
            self._error(
                f"Unable to extract WAV:\n\n{exc}"
            )

    # -------------------------------------------------------------- replace

    def _on_replace(self, event):
        if self.selected_index is None or not self.session_dir:
            return

        wildcard = "WAV audio (*.wav)|*.wav"
        with wx.FileDialog(
            self,
            "Replace WAV",
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            source = Path(dlg.GetPath())

        entry = self.entries[self.selected_index]
        wav_name = safe_name(entry["name"], f"sound_{entry['index']:03d}.wav")
        if not wav_name.lower().endswith(".wav"):
            wav_name += ".wav"
        target = self.session_dir / "working" / wav_name

        try:
            # Validate that the selected file is a supported 16-bit WAV before replacing it.
            with wave.open(str(source), "rb") as w:
                if w.getsampwidth() != 2:
                    raise ValueError("Only 16-bit WAV files are supported.")
                if w.getnchannels() < 1 or w.getframerate() <= 0:
                    raise ValueError("Invalid WAV format.")

            self.stop_playback()
            shutil.copy2(source, target)
            self._refresh_statuses()
        except Exception as exc:
            self._error(str(exc))

    def _refresh_statuses(self):
        if not self.session_dir:
            return

        working = self.session_dir / "working"
        for i, e in enumerate(self.entries):
            name = safe_name(e["name"], f"sound_{e['index']:03d}.wav")
            if not name.lower().endswith(".wav"):
                name += ".wav"
            p = working / name
            status = "Missing"
            if p.exists():
                try:
                    status = "Original" if file_hash(p) == self.original_hashes.get(name) else "Modified"
                except Exception:
                    status = "Modified"
            self.list_ctrl.SetItem(i, 4, status)
        self.Layout()

    # --------------------------------------------------------------- rebuild

    def _on_rebuild(self, event):
        if not self.source_path or not self.session_dir:
            return

        self.stop_playback()
        self._refresh_statuses()

        working = self.session_dir / "working"
        missing = []
        for e in self.entries:
            name = safe_name(e["name"], f"sound_{e['index']:03d}.wav")
            if not name.lower().endswith(".wav"):
                name += ".wav"
            if not (working / name).exists():
                missing.append(name)

        if missing:
            self._error(
                "The sound bank structure cannot be changed.\n\n"
                "Required sample(s) are missing:\n" + "\n".join(missing)
            )
            return

        with wx.FileDialog(
            self,
            "Save rebuilt Sound Bank",
            defaultFile=self.source_path.stem + ".soundbnk",
            wildcard="Sound Bank files (*.soundbnk)|*.soundbnk|All files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            output = Path(dlg.GetPath())

        try:
            original = self.source_path.read_bytes()
            fsb_offset = original.find(b"FSB4")
            if fsb_offset < 0:
                raise ValueError("FSB4 signature not found")
            original_fsb = original[fsb_offset:]
            new_fsb = build_fsb(original_fsb, working)
            output.write_bytes(original[:fsb_offset] + new_fsb)
            wx.MessageBox(
                f"Rebuilt Sound Bank saved successfully:\n\n{output}",
                APP_NAME,
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
        except Exception as exc:
            self._error(f"Rebuild failed:\n{exc}")

    # ---------------------------------------------------------------- files

    def _on_open(self, event):
        with wx.FileDialog(
            self,
            "Open Sound Bank",
            wildcard="Sound Bank files (*.soundbnk)|*.soundbnk|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.open_soundbnk(Path(dlg.GetPath()))

    def _on_close_button(self, event):
        self.Close()

    # -------------------------------------------------------------- lifecycle

    def _destroy_session(self):
        if self.session_dir and self.session_dir.exists():
            shutil.rmtree(self.session_dir, ignore_errors=True)
        self.session_dir = None

    def _on_close(self, event):
        if self._opening:
            return
            
        self.stop_playback()
        self._destroy_session()
        if hasattr(self, "_open_progress") and self._open_progress:
            try:
                if self._open_progress.IsShown():
                    self._open_progress.Destroy()
            except Exception:
                pass
            self._open_progress = None
            
        self.Destroy()

    def _update_enabled_state(self):
        has_file = bool(self.session_dir and self.entries) and not self._opening
        self.toolbar.EnableTool(self.tb_open.GetId(), not self._opening)
        self.toolbar.EnableTool(
            self.tb_replace.GetId(),
            has_file and self.selected_index is not None,
        )
        self.toolbar.EnableTool(
            self.tb_play.GetId(),
            not self._opening and ((has_file and self.selected_index is not None) or self.playing_index is not None),
        )
        self.toolbar.EnableTool(self.tb_rebuild.GetId(), has_file)

        self.tb_extract.Enable(self.selected_index is not None and bool(self.session_dir) and not self._opening)

        self.toolbar.Realize()

    def _error(self, message: str):
        wx.MessageBox(message, APP_NAME, wx.OK | wx.ICON_ERROR, self)

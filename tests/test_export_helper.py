from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export_helper


class FakeSubtitleItem:
    def __init__(self, start: int, end: int, text: str) -> None:
        self._start = start
        self._end = end
        self._text = text

    def GetStart(self):
        return self._start

    def GetEnd(self):
        return self._end

    def GetName(self):
        return self._text


class FakeTimeline:
    def __init__(
        self, tracks, start_frame: int = 0, enabled=None, name: str = "Timeline"
    ) -> None:
        self._tracks = tracks
        self._start_frame = start_frame
        self._enabled = enabled or {}
        self._name = name

    def GetTrackCount(self, track_type):
        return len(self._tracks) if track_type == "subtitle" else 0

    def GetIsTrackEnabled(self, track_type, track_index):
        return self._enabled.get(track_index, True)

    def GetItemListInTrack(self, track_type, track_index):
        return self._tracks[track_index - 1]

    def GetStartFrame(self):
        return self._start_frame

    def GetName(self):
        return self._name


class ExportHelperTests(unittest.TestCase):
    def test_orientation_matches_portrait_and_landscape(self):
        self.assertEqual(
            export_helper.orient_dimensions_to_timeline(1920, 1080, 1080, 1920),
            (1080, 1920, True),
        )
        self.assertEqual(
            export_helper.orient_dimensions_to_timeline(1080, 1920, 1920, 1080),
            (1920, 1080, True),
        )
        self.assertEqual(
            export_helper.orient_dimensions_to_timeline(1920, 1080, 1920, 1080),
            (1920, 1080, False),
        )

    def test_srt_is_timeline_relative_and_preserves_multiline_text(self):
        timeline = FakeTimeline(
            [[FakeSubtitleItem(86_424, 86_448, "第一行\n第二行")]],
            start_frame=86_400,
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Timeline.srt"
            count = export_helper.export_timeline_srt(timeline, path, 24.0)
            content = path.read_text(encoding="utf-8-sig")

        self.assertEqual(count, 1)
        self.assertEqual(
            content,
            "1\n00:00:01,000 --> 00:00:02,000\n第一行\n第二行\n",
        )

    def test_srt_ignores_disabled_tracks_and_sorts_enabled_cues(self):
        timeline = FakeTimeline(
            [
                [FakeSubtitleItem(48, 72, "later")],
                [FakeSubtitleItem(0, 24, "disabled")],
                [FakeSubtitleItem(24, 48, "first")],
            ],
            enabled={2: False},
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Timeline.srt"
            count = export_helper.export_timeline_srt(timeline, path, 24.0)
            content = path.read_text(encoding="utf-8-sig")

        self.assertEqual(count, 2)
        self.assertNotIn("disabled", content)
        self.assertLess(content.index("first"), content.index("later"))

    def test_srt_rejects_empty_enabled_tracks(self):
        timeline = FakeTimeline([[]])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Timeline.srt"
            with self.assertRaisesRegex(
                export_helper.ExportHelperError, "no readable subtitle items"
            ):
                export_helper.export_timeline_srt(timeline, path, 24.0)

    def test_srt_only_export_creates_timeline_named_folder(self):
        timeline = FakeTimeline(
            [[FakeSubtitleItem(0, 24, "字幕")]], name="直式訪談"
        )
        state = {
            "config": {
                "export_xml": True,
                "export_aaf": True,
                "export_mp4": True,
                "export_srt": False,
                "mp4_width": 1920,
                "mp4_height": 1080,
                "mp4_bitrate_mbps": 25.0,
                "mp4_audio_sample_rate": 48_000,
                "require_media_same_package_tree": False,
                "validate_media_under_selected_root": False,
            },
            "project": object(),
            "project_manager": mock.Mock(SaveProject=mock.Mock(return_value=True)),
            "timeline": timeline,
            "resolve": object(),
            "media_paths": [],
            "recovered_from": None,
            "mark_out": None,
            "timeline_fps": 24.0,
            "timeline_width": 1080,
            "timeline_height": 1920,
        }
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(
                export_helper, "current_timeline_state", return_value=state
            ):
                result = export_helper.export_current_timeline(
                    Path(folder),
                    lambda _message: None,
                    export_xml=False,
                    export_aaf=False,
                    export_mp4=False,
                    export_srt=True,
                )
            expected = Path(folder) / "直式訪談"
            self.assertEqual(result, expected)
            self.assertTrue((expected / "直式訪談.srt").is_file())

    def test_gui_constructs_with_srt_and_swap_controls(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtCore import QTimer
            from PyQt6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PyQt6 is not installed")

        app = QApplication.instance() or QApplication([])
        with (
            mock.patch.object(QTimer, "singleShot", return_value=None),
            mock.patch.object(QApplication, "exec", return_value=0),
            mock.patch.object(export_helper, "apply_windows_taskbar_icon"),
        ):
            export_helper.launch_gui()

        windows = [window for window in app.topLevelWidgets() if window.isVisible()]
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window.toggle_srt.property("export_name"), "SRT")
        self.assertEqual(window.swap_dimensions_btn.text(), "↔")
        before = (window.width_edit.text(), window.height_edit.text())
        window.swap_dimensions_btn.click()
        self.assertEqual(
            (window.width_edit.text(), window.height_edit.text()),
            (before[1], before[0]),
        )
        self.assertTrue(window._manual_orientation_override)
        window.close()


if __name__ == "__main__":
    unittest.main()

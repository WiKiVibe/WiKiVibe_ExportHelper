from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


TOOL_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_ROOT / "config.json"
RESOLVE_MODULES = Path(
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"
)


class ExportHelperError(RuntimeError):
    pass


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    required = {
        "validate_media_under_selected_root",
        "mp4_width",
        "mp4_height",
        "mp4_bitrate_mbps",
        "mp4_audio_sample_rate",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise ExportHelperError(f"config.json is missing fields: {', '.join(missing)}")
    # Optional toggles; default all on for older configs.
    data.setdefault("export_xml", True)
    data.setdefault("export_aaf", True)
    data.setdefault("export_mp4", True)
    # Media package tree: require shared parent deeper than drive root (default ON).
    data.setdefault("require_media_same_package_tree", True)
    # Optional: also require media under Output folder (default OFF — output is free).
    data.setdefault("validate_media_under_selected_root", False)
    # Background dot field (edit these in config.json to taste).
    # step: larger = sparser; radius: circle size; alpha: 0-255 brightness.
    data.setdefault("ui_dot_step", 14)
    data.setdefault("ui_dot_radius", 1)
    data.setdefault("ui_dot_alpha", 52)
    # Local-only key used on some machines; never required for shipping defaults.
    data.pop("verified_resolve_version", None)
    # Legacy: used to create a DELIVERABLES subfolder; exports now write into Output folder.
    data.pop("output_subfolder", None)
    return data


def save_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enable_high_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_windows_app_user_model_id(app_id: str = "WiKiVibe.ExportHelper") -> None:
    """Tell Windows this is our app, not python.exe — required for custom taskbar icons."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def resolve_app_icon_path() -> Path | None:
    for name in ("app.ico", "packager.ico"):
        path = (TOOL_ROOT / "assets" / name).resolve()
        if path.is_file():
            return path
    return None


def load_app_icon() -> "object | None":
    """Load app.ico for Qt window / taskbar."""
    icon_path = resolve_app_icon_path()
    if icon_path is None:
        return None
    try:
        from PyQt6.QtGui import QIcon
    except ImportError:
        return None
    icon = QIcon(str(icon_path))
    return None if icon.isNull() else icon


def apply_windows_taskbar_icon(window, icon_path: Path | None = None) -> None:
    """Force taskbar / alt-tab icon via Win32 WM_SETICON (pythonw default is the .py icon)."""
    if sys.platform != "win32":
        return
    path = icon_path or resolve_app_icon_path()
    if path is None or not path.is_file():
        return
    try:
        hwnd = int(window.winId())
        user32 = ctypes.windll.user32
        # 64-bit safe: without restype, HICON handles get truncated.
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        image_icon = 1
        lr_loadfromfile = 0x0010
        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1
        icon_file = str(path.resolve())
        hicon_big = user32.LoadImageW(None, icon_file, image_icon, 32, 32, lr_loadfromfile)
        hicon_small = user32.LoadImageW(None, icon_file, image_icon, 16, 16, lr_loadfromfile)
        if hicon_big:
            user32.SendMessageW(hwnd, wm_seticon, icon_big, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, wm_seticon, icon_small, hicon_small)
    except Exception:
        pass


def connect_resolve():
    if str(RESOLVE_MODULES) not in sys.path:
        sys.path.insert(0, str(RESOLVE_MODULES))
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except Exception as exc:
        raise ExportHelperError(f"Failed to load Resolve Scripting module: {exc}") from exc
    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        raise ExportHelperError("Cannot connect to DaVinci Resolve. Please open Resolve Studio first.")
    for constant in ("EXPORT_FCP_7_XML", "EXPORT_AAF", "EXPORT_AAF_NEW", "EXPORT_NONE"):
        if not hasattr(resolve, constant):
            raise ExportHelperError(f"Current Resolve API is missing required constant: {constant}")
    return resolve


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:80] or "Resolve_Timeline"


def iter_timeline_items(timeline) -> Iterable[tuple[str, int, object]]:
    for track_type in ("video", "audio", "subtitle"):
        for track_index in range(1, int(timeline.GetTrackCount(track_type)) + 1):
            for item in timeline.GetItemListInTrack(track_type, track_index) or []:
                yield track_type, track_index, item


def path_is_under(path: str | Path, parent: str | Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(parent).resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def collect_media_paths(timeline) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    generated: list[str] = []
    seen = set()
    for track_type, track_index, item in iter_timeline_items(timeline):
        pool_item = item.GetMediaPoolItem()
        if not pool_item:
            generated.append(f"{track_type}{track_index}:{item.GetName()}")
            continue
        props = pool_item.GetClipProperty() or {}
        file_path = str(props.get("File Path", "")).strip()
        if file_path and file_path not in seen:
            seen.add(file_path)
            paths.append(file_path)
        elif not file_path:
            generated.append(f"{track_type}{track_index}:{item.GetName()}")
    return paths, generated


def count_items(timeline, track_type: str) -> int:
    return sum(
        len(timeline.GetItemListInTrack(track_type, index) or [])
        for index in range(1, int(timeline.GetTrackCount(track_type)) + 1)
    )


def common_media_folder(media_paths: list[str]) -> Path | None:
    """Folder that contains every media file's parent (best auto root)."""
    if not media_paths:
        return None
    parents = [str(Path(value).resolve(strict=False).parent) for value in media_paths]
    try:
        return Path(os.path.commonpath(parents))
    except ValueError:
        return None


def folder_covers_all_media(folder: Path, media_paths: list[str]) -> bool:
    return all(path_is_under(path, folder) for path in media_paths)


def infer_media_root(media_paths: list[str]) -> tuple[Path | None, str | None]:
    """
    Best-effort media root for the Output folder field.

    Returns (root_or_None, warning_or_None). Does not raise for multi-drive
    timelines — the UI should let the user pick a folder manually.
    """
    if not media_paths:
        return None, "Current timeline has no identifiable file-based media."

    media_parents = [Path(value).resolve(strict=False).parent for value in media_paths]
    media_common = common_media_folder(media_paths)

    # Prefer a nearby .drt folder ONLY if it actually contains all media.
    # Otherwise a DRT sitting in Desktop\Output while clips stay on Desktop
    # would wrongly force Output as the root and block export.
    drt_candidates: list[tuple[int, float, Path]] = []
    for media_parent in media_parents:
        for distance, candidate in enumerate((media_parent, *list(media_parent.parents)[:4])):
            try:
                drts = list(candidate.glob("*.drt"))
            except OSError:
                continue
            for drt in drts:
                try:
                    drt_candidates.append((distance, -drt.stat().st_mtime, candidate))
                except OSError:
                    pass
    for _distance, _mtime, candidate in sorted(drt_candidates, key=lambda value: (value[0], value[1])):
        if folder_covers_all_media(candidate, media_paths):
            return candidate, None

    if media_common is not None:
        warning = None
        if drt_candidates:
            best_drt = sorted(drt_candidates, key=lambda value: (value[0], value[1]))[0][2]
            if not folder_covers_all_media(best_drt, media_paths):
                warning = (
                    f"Found a .drt under {best_drt}, but some timeline media is outside it. "
                    f"Using media common folder instead: {media_common}. "
                    "If this is wrong, Browse… to the real Media Management root."
                )
        return media_common, warning

    # Multi-drive: prefer the drive that holds the most media files, and use
    # that drive's common parent (often MM media vs leftover generator paths).
    by_drive: dict[str, list[Path]] = {}
    for parent in media_parents:
        drive = parent.drive or parent.anchor or str(parent)[:2]
        by_drive.setdefault(drive.upper(), []).append(parent)

    ranked = sorted(by_drive.items(), key=lambda item: len(item[1]), reverse=True)
    primary_drive, primary_parents = ranked[0]
    try:
        common = Path(os.path.commonpath([str(path) for path in primary_parents]))
    except ValueError:
        common = primary_parents[0]

    def drive_label(drive: str) -> str:
        return drive.rstrip("\\/") or drive

    other_drives = [drive for drive, _ in ranked[1:]]
    drive_list = ", ".join(drive_label(d) for d in by_drive)
    primary_label = drive_label(primary_drive)
    warning = (
        f"Timeline media spans multiple drives ({drive_list}). "
        f"Auto-selected {primary_label} drive folder — "
        "confirm Output folder, or Browse… if wrong. "
        "Project rename does not affect media paths."
    )
    if other_drives:
        other_list = ", ".join(drive_label(d) for d in other_drives)
        warning += f" Other drives with media: {other_list}."
    return common, warning


def current_timeline_state() -> dict:
    config = load_config()
    resolve = connect_resolve()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if not project:
        raise ExportHelperError("No Resolve project is open.")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise ExportHelperError("No timeline selected. Open the imported DRT timeline first.")
    recovered_from = None
    candidate_name = timeline.GetName()
    base_name = candidate_name
    suffix_pattern = re.compile(r"__(?:VIDEO|AUDIO)_ONLY(?:_\d{6})?$")
    while suffix_pattern.search(base_name):
        base_name = suffix_pattern.sub("", base_name)
    if base_name != candidate_name:
        matches = [
            project.GetTimelineByIndex(index)
            for index in range(1, int(project.GetTimelineCount()) + 1)
            if project.GetTimelineByIndex(index)
            and project.GetTimelineByIndex(index).GetName() == base_name
        ]
        if len(matches) == 1:
            recovered_from = candidate_name
            timeline = matches[0]
            if not project.SetCurrentTimeline(timeline):
                raise ExportHelperError(f"Found a previous export copy but could not switch back to the source timeline: {base_name}")
    if int(timeline.GetTrackCount("video")) == 0:
        raise ExportHelperError("Current timeline has no video track; cannot export video XML.")
    if int(timeline.GetTrackCount("audio")) == 0:
        raise ExportHelperError("Current timeline has no audio track; cannot export audio AAF.")
    media_paths, generated = collect_media_paths(timeline)
    if not media_paths:
        raise ExportHelperError("Current timeline has no verifiable file-based media.")
    missing = [value for value in media_paths if not Path(value).is_file()]
    if missing:
        raise ExportHelperError("Current timeline has offline/missing media:\n" + "\n".join(missing[:30]))
    # Re-assert current timeline so Resolve refreshes mark cache (stale after load/DRT).
    try:
        project.SetCurrentTimeline(timeline)
    except Exception:
        pass
    mark_in, mark_out, mark_source = get_timeline_mark_in_out(
        timeline, project=project, resolve=resolve
    )
    fps = get_timeline_fps(timeline, project)
    start_frame = get_timeline_start_frame(timeline)
    start_tc = get_timeline_start_timecode(timeline)
    # No In mark → default to frame 0 / 00:00:00:00 (00000000).
    mark_in_defaulted = mark_in is None
    if mark_in is None:
        mark_in = 0
        mark_in_tc = "00:00:00:00"
        if mark_source in (None, "", "none"):
            mark_source = "default_in_0"
    else:
        mark_in_tc = format_mark_timecode(mark_in, fps, start_frame, start_tc)
    mark_out_tc = format_mark_timecode(mark_out, fps, start_frame, start_tc)
    inferred_root, media_root_warning = infer_media_root(media_paths)
    return {
        "config": config,
        "resolve": resolve,
        "project_manager": pm,
        "project": project,
        "timeline": timeline,
        "version": resolve.GetVersionString(),
        "media_paths": media_paths,
        "generated_items": generated,
        "inferred_root": inferred_root,
        "media_root_warning": media_root_warning,
        "recovered_from": recovered_from,
        "mark_in": mark_in,
        "mark_out": mark_out,
        "mark_in_tc": mark_in_tc,
        "mark_out_tc": mark_out_tc,
        "mark_in_defaulted": mark_in_defaulted,
        "timeline_fps": fps,
        "mark_source": mark_source,
    }


def _as_frame(value) -> int | None:
    """Normalize Resolve mark values (int/float/str) to frame int."""
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def get_timeline_fps(timeline, project=None) -> float:
    """Best-effort timeline frame rate for timecode display."""
    candidates: list[object] = []
    for getter in (
        lambda: timeline.GetSetting("timelineFrameRate"),
        lambda: timeline.GetSetting("timelinePlaybackFrameRate"),
        lambda: project.GetSetting("timelineFrameRate") if project else None,
    ):
        try:
            value = getter()
        except Exception:
            value = None
        if value is not None and str(value).strip() != "":
            candidates.append(value)
    for raw in candidates:
        text = str(raw).strip().upper().replace(" ", "")
        text = text.replace("DF", "").replace("NDF", "")
        try:
            fps = float(text)
            if fps > 0:
                return fps
        except ValueError:
            continue
    return 24.0


def get_timeline_start_frame(timeline) -> int | None:
    try:
        return _as_frame(timeline.GetStartFrame())
    except Exception:
        return None


def get_timeline_start_timecode(timeline) -> str | None:
    try:
        value = timeline.GetStartTimecode()
    except Exception:
        return None
    text = str(value or "").strip()
    return text or None


def _fps_base(fps: float) -> int:
    """Nominal frames-per-second used for the FF field (24 for 23.976, etc.)."""
    if abs(fps - 23.976) < 0.02 or abs(fps - 23.98) < 0.02:
        return 24
    if abs(fps - 29.97) < 0.02:
        return 30
    if abs(fps - 59.94) < 0.02:
        return 60
    rounded = int(round(fps))
    return rounded if rounded > 0 else 24


def frames_to_timecode(frame: int, fps: float) -> str:
    """Convert a 0-based frame index to HH:MM:SS:FF (non-drop)."""
    base = _fps_base(fps)
    total = max(0, int(frame))
    ff = total % base
    total_sec = total // base
    ss = total_sec % 60
    total_min = total_sec // 60
    mm = total_min % 60
    hh = total_min // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def parse_timecode_to_frames(timecode: str, fps: float) -> int | None:
    """Parse HH:MM:SS:FF (or ; for DF) into a 0-based frame count."""
    text = str(timecode or "").strip().replace(";", ":")
    parts = text.split(":")
    if len(parts) != 4:
        return None
    try:
        hh, mm, ss, ff = (int(part) for part in parts)
    except ValueError:
        return None
    base = _fps_base(fps)
    return ((hh * 60 + mm) * 60 + ss) * base + ff


def format_mark_timecode(
    frame: int | None,
    fps: float,
    start_frame: int | None = None,
    start_tc: str | None = None,
) -> str | None:
    """
    Display Resolve mark frames as timeline timecode.

    GetMarkInOut() may return either:
    - absolute frames in the same space as GetStartFrame(), or
    - 0-based offsets from the timeline start (API examples often use 0..N).
    """
    if frame is None:
        return None
    sf = start_frame if start_frame is not None else 0
    # Relative offsets are usually small compared to a 01:00:00:00 start frame.
    if sf > 0 and int(frame) < int(sf):
        base = parse_timecode_to_frames(start_tc or "00:00:00:00", fps)
        if base is None:
            base = 0
        return frames_to_timecode(base + int(frame), fps)
    return frames_to_timecode(int(frame), fps)


def _extract_in_out_from_mark_dict(marks) -> tuple[int | None, int | None]:
    """Parse GetMarkInOut() payloads across Resolve versions / key casings."""
    if not isinstance(marks, dict) or not marks:
        return None, None

    def pick_pair(section: dict) -> tuple[int | None, int | None]:
        if not isinstance(section, dict):
            return None, None
        # Resolve usually uses lowercase; some builds / wrappers differ.
        mi = _as_frame(
            section.get("in", section.get("In", section.get("IN", section.get("markIn"))))
        )
        mo = _as_frame(
            section.get("out", section.get("Out", section.get("OUT", section.get("markOut"))))
        )
        return mi, mo

    # Nested: { video: {in,out}, audio: {in,out}, all: {...} }
    nested_in: int | None = None
    nested_out: int | None = None
    for key in ("video", "Video", "VIDEO", "audio", "Audio", "AUDIO", "all", "All", "ALL"):
        if key not in marks:
            continue
        mi, mo = pick_pair(marks.get(key) or {})
        if mi is not None:
            nested_in = mi
        if mo is not None:
            nested_out = mo
        if nested_in is not None and nested_out is not None:
            return nested_in, nested_out

    # Flat: { in, out } (some API wrappers / older returns)
    flat_in, flat_out = pick_pair(marks)
    if flat_in is not None or flat_out is not None:
        # Avoid treating nested-only dicts as flat when they only have type keys.
        type_keys = {"video", "Video", "audio", "Audio", "all", "All"}
        if not (type_keys & set(marks.keys())):
            return flat_in, flat_out

    return nested_in, nested_out


def get_timeline_mark_in_out(
    timeline,
    project=None,
    resolve=None,
) -> tuple[int | None, int | None, str]:
    """
    Read timeline In/Out more tolerantly than a single GetMarkInOut() call.

    Resolve sometimes returns empty marks right after loading a DRT / switching
    timelines until the mark cache is refreshed, or returns only video/audio.
    """
    payloads: list[tuple[str, object]] = []

    def collect(label: str, getter) -> None:
        try:
            value = getter()
        except TypeError:
            return
        except Exception:
            return
        # Keep empty dicts out, but allow zero frame numbers later in extract.
        if value is None or value is False:
            return
        if isinstance(value, dict) and not value:
            return
        payloads.append((label, value))

    previous_page = None
    if resolve is not None:
        try:
            previous_page = resolve.GetCurrentPage()
            # Marks are most reliable on timeline pages; briefly ensure one.
            if previous_page not in {"edit", "cut", "fairlight", "deliver", "color"}:
                resolve.OpenPage("edit")
        except Exception:
            previous_page = None

    try:
        # Preferred: timeline marks (what the UI I/O keys set).
        collect("timeline", lambda: timeline.GetMarkInOut())
        for kind in ("all", "video", "audio"):
            collect(f"timeline:{kind}", lambda k=kind: timeline.GetMarkInOut(k))

        # Fallback: project-level marks (some pages / builds surface them here).
        if project is not None:
            collect("project", lambda: project.GetMarkInOut())
            for kind in ("all", "video", "audio"):
                collect(f"project:{kind}", lambda k=kind: project.GetMarkInOut(k))
    finally:
        if resolve is not None and previous_page:
            try:
                resolve.OpenPage(previous_page)
            except Exception:
                pass

    # Prefer first complete pair; keep best partial for diagnostics.
    partial: tuple[int | None, int | None, str] | None = None
    for label, raw in payloads:
        mi, mo = _extract_in_out_from_mark_dict(raw)
        if mi is not None and mo is not None:
            return mi, mo, label
        if partial is None and (mi is not None or mo is not None):
            partial = (mi, mo, label)

    if partial is not None:
        return partial[0], partial[1], partial[2]
    return None, None, "none"


def media_outside_folder(media_paths: list[str], folder: Path) -> list[str]:
    return [value for value in media_paths if not path_is_under(value, folder)]


def is_drive_root(path: Path) -> bool:
    """True for 'D:\\' / 'D:/' style roots (not a real package folder)."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    anchor = Path(resolved.anchor) if resolved.anchor else None
    if anchor is None:
        return False
    left = str(resolved).rstrip("\\/").casefold()
    right = str(anchor).rstrip("\\/").casefold()
    return left == right or resolved == anchor


def check_media_same_package_tree(
    media_paths: list[str],
) -> tuple[bool, Path | None, str | None]:
    """
    Require all timeline media to share one package folder tree.

    OK:  D:/test/Video/...  and  D:/test/audio/...   → common D:/test
    BAD: D:/test/video/...  and  D:/test222/audio/... → common is only D:/

    Output folder is unrelated — only timeline media paths are checked.
    """
    if not media_paths:
        return False, None, "No file-based media paths to check."

    resolved = [str(Path(p).resolve(strict=False)) for p in media_paths]
    try:
        common = Path(os.path.commonpath(resolved))
    except ValueError:
        return (
            False,
            None,
            "Timeline media spans multiple drives. "
            "Keep all package media under one drive/folder tree "
            "(e.g. D:/JobName/Video + D:/JobName/Audio).",
        )

    # commonpath of files may land on a file if only one path; normalize to folder
    if common.is_file():
        common = common.parent

    if is_drive_root(common):
        # Group by first folder under the drive for a clearer error.
        tops: dict[str, int] = {}
        for path in resolved:
            p = Path(path)
            parts = p.parts
            # ('D:\\', 'test', 'Video', 'a.mov') → 'D:\\test'
            if len(parts) >= 2:
                top = str(Path(parts[0]) / parts[1])
            else:
                top = str(p.anchor or p)
            tops[top] = tops.get(top, 0) + 1
        top_list = ", ".join(f"{k} ({v})" for k, v in sorted(tops.items(), key=lambda x: -x[1]))
        return (
            False,
            common,
            "Timeline media is not under the same package folder.\n\n"
            "Allowed example:\n"
            "  D:/test/Video/...\n"
            "  D:/test/Audio/...\n"
            "  → same parent D:/test\n\n"
            "Blocked example:\n"
            "  D:/test/Video/...\n"
            "  D:/test222/Audio/...\n"
            "  → only share the drive root\n\n"
            f"Detected top-level folders: {top_list}",
        )

    return True, common, None


def verify_media_package_tree(media_paths: list[str]) -> Path:
    """Hard-stop unless media shares a non-drive package root. Returns that root."""
    ok, common, message = check_media_same_package_tree(media_paths)
    if not ok:
        raise ExportHelperError(message or "Media package tree check failed.")
    assert common is not None
    return common


def verify_media_root(media_paths: list[str], media_root: Path) -> None:
    """
    Optional strict mode: require every timeline media file under media_root.

    Output folder is normally free (where deliverables are written). Only when
    config validate_media_under_selected_root is true do we hard-stop.
    """
    outside = media_outside_folder(media_paths, media_root)
    if not outside:
        return
    suggested = common_media_folder(media_paths)
    lines = [
        "Strict package check is ON: timeline media must sit under the selected folder.",
        "Stopped because some files are outside that folder.",
        "",
        f"Selected folder:\n{media_root}",
        "",
    ]
    if suggested is not None:
        lines.extend(
            [
                "Folder that contains all timeline media:",
                str(suggested),
                "",
            ]
        )
    lines.extend(
        [
            "Outside files (first 30):",
            *outside[:30],
            "",
            "Output folder can be any path you want for deliverables.",
            "Strict check is only for 'everything must live under this folder' packaging.",
            "Turn it off in config.json: validate_media_under_selected_root = false",
            "(default is false).",
        ]
    )
    raise ExportHelperError("\n".join(lines))


def delete_all_tracks(timeline, track_type: str) -> list[int]:
    retained_empty_tracks: list[int] = []
    for index in range(int(timeline.GetTrackCount(track_type)), 0, -1):
        if timeline.GetIsTrackLocked(track_type, index):
            timeline.SetTrackLock(track_type, index, False)
        timeline.SetTrackEnable(track_type, index, True)
        items = list(timeline.GetItemListInTrack(track_type, index) or [])
        if items:
            timeline.SetClipsLinked(items, False)
            if not timeline.DeleteClips(items, False):
                raise ExportHelperError(f"Could not clear {track_type} track {index} clips")
        if not timeline.DeleteTrack(track_type, index):
            remaining = list(timeline.GetItemListInTrack(track_type, index) or [])
            if remaining:
                raise ExportHelperError(f"{track_type} track {index} still has {len(remaining)} clips")
            retained_empty_tracks.append(index)
    remaining_items = []
    for index in range(1, int(timeline.GetTrackCount(track_type)) + 1):
        remaining_items.extend(timeline.GetItemListInTrack(track_type, index) or [])
    if remaining_items:
        raise ExportHelperError(f"{track_type} track still has {len(remaining_items)} clips; export aborted")
    return retained_empty_tracks


def unique_timeline_name(project, base_name: str) -> str:
    existing = {
        project.GetTimelineByIndex(index).GetName()
        for index in range(1, int(project.GetTimelineCount()) + 1)
        if project.GetTimelineByIndex(index)
    }
    if base_name not in existing:
        return base_name
    stamp = datetime.now().strftime("%H%M%S")
    return f"{base_name}_{stamp}"


def get_timeline_end_frame(timeline) -> int | None:
    try:
        return _as_frame(timeline.GetEndFrame())
    except Exception:
        return None


def render_mark_range_candidates(
    timeline,
    mark_in: int,
    mark_out: int,
) -> list[tuple[int, int, str]]:
    """
    Build possible MarkIn/MarkOut pairs for Deliver.

    GetMarkInOut() may return 0-based offsets or absolute frames. Wrong pairs
    make every SetRenderSettings call return False (as seen with MarkIn=0).
    """
    mi0, mo0 = int(mark_in), int(mark_out)
    if mo0 < mi0:
        raise ExportHelperError("Timeline Out is earlier than In.")

    start = get_timeline_start_frame(timeline)
    end = get_timeline_end_frame(timeline)
    candidates: list[tuple[int, int, str]] = []

    def add(mi: int, mo: int, note: str) -> None:
        if mo < mi:
            return
        # Prefer ranges that fit inside known timeline bounds when available.
        if start is not None and end is not None and end >= start:
            if mo < start or mi > end:
                return
            mi2 = max(mi, start)
            mo2 = min(mo, end)
            if mo2 < mi2:
                return
            candidates.append((mi2, mo2, note))
        else:
            candidates.append((mi, mo, note))

    add(mi0, mo0, "as-read")
    if start is not None:
        add(start + mi0, start + mo0, f"start+offset (start={start})")
        # Sometimes Out is a duration/length rather than end frame.
        add(start + mi0, start + mi0 + max(0, mo0 - mi0), f"start+duration (start={start})")
    if start is not None and end is not None:
        add(start, end, "full-timeline")
        # In at start, out as read if out already absolute-looking
        if mo0 >= (start or 0):
            add(start, mo0, "start-to-markout")

    # De-dupe while preserving order
    seen: set[tuple[int, int]] = set()
    unique: list[tuple[int, int, str]] = []
    for mi, mo, note in candidates:
        key = (mi, mo)
        if key in seen:
            continue
        seen.add(key)
        unique.append((mi, mo, note))
    if not unique:
        raise ExportHelperError(
            f"Could not build a valid MP4 frame range from In={mi0}, Out={mo0}, "
            f"timeline start={start}, end={end}."
        )
    return unique


def build_mp4_render_settings(
    delivery: Path,
    custom_name: str,
    mark_in: int,
    mark_out: int,
    width: int,
    height: int,
    bitrate_mbps: float,
    audio_sample_rate: int,
    *,
    video_quality: int | str | None = None,
    encoding_profile: str | None = None,
    include_audio_details: bool = True,
    include_format_size: bool = True,
    frame_rate: float | None = None,
    select_all_frames: bool = False,
) -> dict:
    """
    Build Deliver-compatible render settings for H.264.

    Quality default = Restrict to (bitrate_mbps × 1000 kbps), matching the UI Mbps
    field — same idea as Deliver "Restrict to … Kb/s". EncodingProfile is omitted
    by default (Deliver "Auto").
    """
    if (not select_all_frames) and mark_out < mark_in:
        raise ExportHelperError("Timeline Out is earlier than In.")
    if width < 320 or height < 240:
        raise ExportHelperError("Video resolution is too small.")
    if bitrate_mbps <= 0 or bitrate_mbps > 500:
        raise ExportHelperError("Video bitrate must be between 0 and 500 Mbps.")
    if video_quality is None:
        # Docs: int bitrate (kbps), or 0 = automatic / quality strings.
        # Prefer explicit bitrate (UI Restrict to); auto is a fallback variant only.
        video_quality = int(round(float(bitrate_mbps) * 1000))
    target = str(delivery.resolve(strict=False))
    settings: dict = {
        "SelectAllFrames": bool(select_all_frames),
        "TargetDir": target,
        "CustomName": custom_name,
        "ExportVideo": True,
        "ExportAudio": True,
        "VideoQuality": video_quality,
    }
    if not select_all_frames:
        settings["MarkIn"] = int(mark_in)
        settings["MarkOut"] = int(mark_out)
    if include_format_size:
        settings["FormatWidth"] = int(width)
        settings["FormatHeight"] = int(height)
        settings["PixelAspectRatio"] = "square"
    if encoding_profile:
        settings["EncodingProfile"] = encoding_profile
    if include_audio_details:
        settings["AudioCodec"] = "aac"
        settings["AudioSampleRate"] = int(audio_sample_rate)
    if frame_rate is not None and frame_rate > 0:
        settings["FrameRate"] = float(frame_rate)
    return settings


def _mp4_settings_variants(
    delivery: Path,
    custom_name: str,
    mark_in: int,
    mark_out: int,
    width: int,
    height: int,
    bitrate_mbps: float,
    audio_sample_rate: int,
    frame_rate: float | None,
    *,
    select_all_frames: bool = False,
) -> list[tuple[str, dict]]:
    bitrate_kbps = int(round(float(bitrate_mbps) * 1000))
    prefix = "allframes-" if select_all_frames else ""
    # Order matters: Restrict-to bitrate first (UI Mbps), then auto quality,
    # then slim / bare fallbacks. Do NOT put ExportSubtitle / NetworkOptimization
    # in the base dict — unknown keys can make SetRenderSettings return False.
    return [
        (
            f"{prefix}restrict-bitrate",
            build_mp4_render_settings(
                delivery,
                custom_name,
                mark_in,
                mark_out,
                width,
                height,
                bitrate_mbps,
                audio_sample_rate,
                video_quality=bitrate_kbps,
                encoding_profile=None,
                include_audio_details=True,
                include_format_size=True,
                frame_rate=frame_rate,
                select_all_frames=select_all_frames,
            ),
        ),
        (
            f"{prefix}restrict-main",
            build_mp4_render_settings(
                delivery,
                custom_name,
                mark_in,
                mark_out,
                width,
                height,
                bitrate_mbps,
                audio_sample_rate,
                video_quality=bitrate_kbps,
                encoding_profile="Main",
                include_audio_details=True,
                include_format_size=True,
                frame_rate=frame_rate,
                select_all_frames=select_all_frames,
            ),
        ),
        (
            f"{prefix}auto-quality",
            build_mp4_render_settings(
                delivery,
                custom_name,
                mark_in,
                mark_out,
                width,
                height,
                bitrate_mbps,
                audio_sample_rate,
                video_quality=0,
                encoding_profile=None,
                include_audio_details=True,
                include_format_size=True,
                frame_rate=None,
                select_all_frames=select_all_frames,
            ),
        ),
        (
            f"{prefix}no-size",
            build_mp4_render_settings(
                delivery,
                custom_name,
                mark_in,
                mark_out,
                width,
                height,
                bitrate_mbps,
                audio_sample_rate,
                video_quality=0,
                encoding_profile=None,
                include_audio_details=False,
                include_format_size=False,
                frame_rate=None,
                select_all_frames=select_all_frames,
            ),
        ),
        (
            f"{prefix}bare",
            {
                "SelectAllFrames": bool(select_all_frames),
                **(
                    {}
                    if select_all_frames
                    else {"MarkIn": int(mark_in), "MarkOut": int(mark_out)}
                ),
                "TargetDir": str(delivery.resolve(strict=False)),
                "CustomName": custom_name,
                "ExportVideo": True,
                "ExportAudio": True,
            },
        ),
    ]


def _try_soft_render_tweaks(
    project, settings: dict, log: Callable[[str], None]
) -> dict:
    """
    Best-effort extras after a working settings dict is accepted.

    Applied one key at a time so a single unknown key never nukes the job.
    If a soft pass fails, re-apply the last good settings.
    """
    current = dict(settings)
    soft_keys = (
        ("ExportSubtitle", False, "ExportSubtitle=False"),
        ("NetworkOptimization", False, "NetworkOptimization=False"),
        ("MultiPassEncode", False, "MultiPassEncode=False"),
    )
    for key, value, note in soft_keys:
        try:
            trial = dict(current)
            trial[key] = value
            if project.SetRenderSettings(trial):
                current = trial
                log(f"MOV: {note} applied")
            else:
                project.SetRenderSettings(current)
        except Exception:
            try:
                project.SetRenderSettings(current)
            except Exception:
                pass
    return current


def apply_mp4_render_settings(
    project,
    timeline,
    delivery: Path,
    custom_name: str,
    mark_in: int,
    mark_out: int,
    width: int,
    height: int,
    bitrate_mbps: float,
    audio_sample_rate: int,
    frame_rate: float | None,
    log: Callable[[str], None],
) -> tuple[dict, int, int]:
    """
    Try mark-range interpretations × settings variants until Resolve accepts one.
    Returns (settings, used_mark_in, used_mark_out).
    """
    ranges = render_mark_range_candidates(timeline, mark_in, mark_out)
    errors: list[str] = []

    for mi, mo, range_note in ranges:
        # Push marks onto the timeline so Deliver UI / API share the same range.
        try:
            timeline.SetMarkInOut(int(mi), int(mo), "all")
        except TypeError:
            try:
                timeline.SetMarkInOut(int(mi), int(mo))
            except Exception:
                pass
        except Exception:
            pass

        for label, settings in _mp4_settings_variants(
            delivery,
            custom_name,
            mi,
            mo,
            width,
            height,
            bitrate_mbps,
            audio_sample_rate,
            frame_rate,
            select_all_frames=False,
        ):
            try:
                ok = project.SetRenderSettings(settings)
            except Exception as exc:
                errors.append(f"{range_note}/{label}: exception {exc}")
                continue
            if ok:
                settings = _try_soft_render_tweaks(project, settings, log)
                log(
                    f"MOV render settings accepted ({range_note} / {label}): "
                    f"MarkIn={mi}, MarkOut={mo}"
                )
                return settings, mi, mo
            errors.append(f"{range_note}/{label}: rejected")

    # Last resort: whole timeline (still better than hard-fail with no file).
    start = get_timeline_start_frame(timeline)
    end = get_timeline_end_frame(timeline)
    if start is not None and end is not None and end >= start:
        try:
            timeline.SetMarkInOut(int(start), int(end), "all")
        except Exception:
            pass
        for label, settings in _mp4_settings_variants(
            delivery,
            custom_name,
            int(start),
            int(end),
            width,
            height,
            bitrate_mbps,
            audio_sample_rate,
            frame_rate,
            select_all_frames=True,
        ):
            try:
                ok = project.SetRenderSettings(settings)
            except Exception as exc:
                errors.append(f"select-all/{label}: exception {exc}")
                continue
            if ok:
                settings = _try_soft_render_tweaks(project, settings, log)
                log(
                    f"MOV render settings accepted via SelectAllFrames fallback "
                    f"({label}). In/Out marks were not applied to the render range."
                )
                return settings, int(start), int(end)
            errors.append(f"select-all/{label}: rejected")

    detail = "; ".join(errors[:20])
    if len(errors) > 20:
        detail += f"; …(+{len(errors) - 20} more)"
    start = get_timeline_start_frame(timeline)
    end = get_timeline_end_frame(timeline)
    raise ExportHelperError(
        "Resolve rejected MOV render settings.\n"
        f"API marks In={mark_in}, Out={mark_out}; "
        f"timeline start={start}, end={end}; "
        f"{width}x{height}; target={delivery}\n"
        f"Attempts: {detail}"
    )


def _is_quicktime_format_token(token: str) -> bool:
    t = str(token).strip().casefold()
    return t in {"mov", "qt", "quicktime"} or "quicktime" in t or t.endswith(".mov")


def _is_mp4_format_token(token: str) -> bool:
    t = str(token).strip().casefold()
    return t == "mp4" or "mp4" in t


def _is_h264_codec_token(token: str) -> bool:
    t = str(token).strip().casefold()
    return "h.264" in t or "h264" in t or t in {"avc", "avc1"}


def render_container_extension(render_format: str) -> str:
    """Map Resolve format id/name to a file extension (.mov preferred)."""
    if _is_quicktime_format_token(render_format):
        return ".mov"
    if _is_mp4_format_token(render_format):
        return ".mp4"
    return ".mov"


def set_h264_video(project) -> tuple[str, str]:
    """
    Prefer QuickTime + H.264 (Deliver screenshot default), fall back to MP4 + H.264.
    Returns (render_format, codec) as accepted by SetCurrentRenderFormatAndCodec.
    """
    formats = project.GetRenderFormats() or {}
    qt_tokens: list[str] = []
    mp4_tokens: list[str] = []
    for key, value in formats.items():
        for token in (key, value):
            if token is None:
                continue
            s = str(token)
            if _is_quicktime_format_token(s):
                qt_tokens.append(s)
            elif _is_mp4_format_token(s):
                mp4_tokens.append(s)

    # Explicit seeds first (API key names vary by Resolve build / locale).
    format_order = list(
        dict.fromkeys(
            [
                *["mov", "Mov", "MOV", "quicktime", "QuickTime", "Quicktime"],
                *qt_tokens,
                *["mp4", "Mp4", "MP4"],
                *mp4_tokens,
            ]
        )
    )

    candidates: list[tuple[str, str]] = []
    for render_format in format_order:
        try:
            codecs = project.GetRenderCodecs(render_format) or {}
        except Exception:
            codecs = {}
        codec_tokens: list[str] = []
        for key, value in codecs.items():
            for token in (value, key):
                if token is not None and _is_h264_codec_token(str(token)):
                    codec_tokens.append(str(token))
        for codec in dict.fromkeys(["H264", "H.264", "h264", *codec_tokens]):
            candidates.append((render_format, codec))

    # Last-ditch pairs if GetRenderFormats was empty / incomplete.
    for pair in (
        ("mov", "H264"),
        ("mov", "H.264"),
        ("QuickTime", "H.264"),
        ("QuickTime", "H264"),
        ("mp4", "H264"),
        ("mp4", "H.264"),
    ):
        candidates.append(pair)

    tried: set[tuple[str, str]] = set()
    for render_format, codec in candidates:
        key = (str(render_format), str(codec))
        if key in tried:
            continue
        tried.add(key)
        try:
            if project.SetCurrentRenderFormatAndCodec(render_format, codec):
                return str(render_format), str(codec)
        except Exception:
            pass
    raise ExportHelperError(
        "Resolve could not set QuickTime/MP4 + H.264 render format.\n"
        "Open Deliver and confirm MOV (H.264) is available for mov or mp4."
    )


# Back-compat alias used by older call sites / mental model.
set_h264_mp4 = set_h264_video


def _find_rendered_video(delivery: Path, custom_name: str, preferred_ext: str) -> Path:
    """Locate the file Resolve wrote after a completed render job."""
    preferred = preferred_ext if preferred_ext.startswith(".") else f".{preferred_ext}"
    primary = delivery / f"{custom_name}{preferred}"
    if primary.is_file() and primary.stat().st_size > 0:
        return primary

    patterns = [
        f"{custom_name}*{preferred}",
        f"{custom_name}*.mov",
        f"{custom_name}*.mp4",
        f"{custom_name}*.MOV",
        f"{custom_name}*.MP4",
    ]
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(delivery.glob(pattern))
    matches = [p for p in matches if p.is_file() and p.stat().st_size > 0]
    if not matches:
        raise ExportHelperError(
            "Resolve reported render complete, but the MOV video file was not found.\n"
            f"Looked under: {delivery} for name {custom_name}*"
        )
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def render_h264_mp4(
    state: dict,
    delivery: Path,
    custom_name: str,
    width: int,
    height: int,
    bitrate_mbps: float,
    log: Callable[[str], None],
) -> tuple[Path, dict]:
    """
    Export H.264 Single Clip: prefer QuickTime (.mov), fall back to MP4.
    Quality uses UI Mbps as Restrict-to bitrate (kbps).
    """
    mark_in = state["mark_in"]
    mark_out = state["mark_out"]
    # In defaults to 0 (00:00:00:00) when unset; Out is still required.
    if mark_in is None:
        mark_in = 0
    if mark_out is None:
        raise ExportHelperError(
            "Current timeline has no Out mark; set Out (O) before exporting MOV. "
            "In defaults to 00:00:00:00 when unset."
        )
    project = state["project"]
    source = state["timeline"]
    resolve = state.get("resolve")
    previous_format_codec = project.GetCurrentRenderFormatAndCodec() or {}
    previous_mode = project.GetCurrentRenderMode()
    previous_page = None
    job_id = None
    try:
        if not project.SetCurrentTimeline(source):
            raise ExportHelperError(
                "Could not switch back to source timeline before MOV export."
            )
        # Deliver page is the most reliable context for render queue APIs.
        if resolve is not None:
            try:
                previous_page = resolve.GetCurrentPage()
                resolve.OpenPage("deliver")
            except Exception:
                previous_page = None

        render_format, codec = set_h264_video(project)
        container_ext = render_container_extension(render_format)
        log(f"Render format/codec: {render_format} / {codec} → *{container_ext}")
        if not project.SetCurrentRenderMode(1):
            raise ExportHelperError("Could not set Resolve Render Mode to Single Clip.")

        delivery.mkdir(parents=True, exist_ok=True)
        # ASCII-only custom name avoids rare Deliver rejections on some locales.
        safe_custom = safe_name(custom_name)
        settings, mark_in, mark_out = apply_mp4_render_settings(
            project=project,
            timeline=source,
            delivery=delivery,
            custom_name=safe_custom,
            mark_in=int(mark_in),
            mark_out=int(mark_out),
            width=width,
            height=height,
            bitrate_mbps=bitrate_mbps,
            audio_sample_rate=int(state["config"]["mp4_audio_sample_rate"]),
            frame_rate=state.get("timeline_fps"),
            log=log,
        )
        custom_name = safe_custom
        job_id = project.AddRenderJob()
        if not job_id:
            raise ExportHelperError("Could not add MOV job to the Render Queue.")
        in_tc = format_mark_timecode(
            mark_in,
            float(state.get("timeline_fps") or 24),
            get_timeline_start_frame(source),
            get_timeline_start_timecode(source),
        )
        out_tc = format_mark_timecode(
            mark_out,
            float(state.get("timeline_fps") or 24),
            get_timeline_start_frame(source),
            get_timeline_start_timecode(source),
        )
        range_label = (
            f"{in_tc} → {out_tc} (frames {mark_in}–{mark_out})"
            if in_tc and out_tc
            else f"frames {mark_in} → {mark_out}"
        )
        vq = settings.get("VideoQuality")
        encode_label = (
            "auto"
            if vq == 0 or vq in ("Best", "High", "Medium", "Low", "Least")
            else f"Restrict to {bitrate_mbps:g} Mbps"
        )
        log(
            f"Starting MOV export ({container_ext}): {width}×{height} / "
            f"{encode_label} / In/Out {range_label}"
        )
        if not project.StartRendering([job_id], False):
            raise ExportHelperError("Resolve could not start the MOV render job.")
        last_percent = -1
        while project.IsRenderingInProgress():
            status = project.GetRenderJobStatus(job_id) or {}
            percent = int(float(status.get("CompletionPercentage", 0) or 0))
            if percent >= last_percent + 5:
                log(f"MOV render: {percent}%")
                last_percent = percent
            time.sleep(1)
        status = project.GetRenderJobStatus(job_id) or {}
        job_status = str(status.get("JobStatus", ""))
        if job_status.casefold() not in {"complete", "completed"}:
            raise ExportHelperError(
                f"MOV render did not complete: {job_status or status}"
            )
        output_path = _find_rendered_video(delivery, custom_name, container_ext)
        project.DeleteRenderJob(job_id)
        job_id = None
        return output_path, {
            "format": render_format,
            "codec": codec,
            "extension": output_path.suffix,
            "mark_in": mark_in,
            "mark_out": mark_out,
            "width": width,
            "height": height,
            "bitrate_mbps": bitrate_mbps,
        }
    finally:
        if job_id and not project.IsRenderingInProgress():
            # Leave failed jobs in the queue for inspection.
            log(f"MOV render job left in Render Queue: {job_id}")
        try:
            old_format = previous_format_codec.get("format")
            old_codec = previous_format_codec.get("codec")
            if old_format and old_codec:
                project.SetCurrentRenderFormatAndCodec(old_format, old_codec)
            project.SetCurrentRenderMode(previous_mode)
            project.SetCurrentTimeline(source)
            if resolve is not None and previous_page:
                resolve.OpenPage(previous_page)
        except Exception:
            pass


def export_current_timeline(
    media_root: Path,
    log: Callable[[str], None],
    mp4_width: int | None = None,
    mp4_height: int | None = None,
    mp4_bitrate_mbps: float | None = None,
    export_xml: bool | None = None,
    export_aaf: bool | None = None,
    export_mp4: bool | None = None,
) -> Path:
    state = current_timeline_state()
    config = state["config"]
    do_xml = bool(config.get("export_xml", True) if export_xml is None else export_xml)
    do_aaf = bool(config.get("export_aaf", True) if export_aaf is None else export_aaf)
    do_mp4 = bool(config.get("export_mp4", True) if export_mp4 is None else export_mp4)
    if not (do_xml or do_aaf or do_mp4):
        raise ExportHelperError("Enable at least one export: XML, AAF, or MOV.")

    width = int(mp4_width if mp4_width is not None else config["mp4_width"])
    height = int(mp4_height if mp4_height is not None else config["mp4_height"])
    bitrate_mbps = float(
        mp4_bitrate_mbps if mp4_bitrate_mbps is not None else config["mp4_bitrate_mbps"]
    )
    if do_mp4 and state["mark_out"] is None:
        raise ExportHelperError(
            "Set an Out mark on the current timeline. "
            "MOV exports In→Out; In defaults to 00:00:00:00 when unset."
        )
    project = state["project"]
    source = state["timeline"]
    media_root = media_root.resolve(strict=False)
    if not media_root.is_dir():
        raise ExportHelperError("Selected output folder does not exist.")

    # 1) Package tree: media must share one job folder (siblings OK), not only the drive.
    if bool(config.get("require_media_same_package_tree", True)):
        package_root = verify_media_package_tree(state["media_paths"])
        log(f"Media package tree OK: {package_root}")
    else:
        ok, package_root, message = check_media_same_package_tree(state["media_paths"])
        if ok and package_root is not None:
            log(f"Media package tree: {package_root} (check disabled)")
        elif message:
            log(f"Media package tree warning (check disabled): {message.splitlines()[0]}")

    # 2) Output folder is free — deliverables are written directly here (no subfolder).
    # Optional strict mode can also require media under that Output folder.
    outside = media_outside_folder(state["media_paths"], media_root)
    if outside:
        if bool(config.get("validate_media_under_selected_root", False)):
            verify_media_root(state["media_paths"], media_root)
        else:
            sample = "; ".join(Path(p).name for p in outside[:5])
            more = f" (+{len(outside) - 5} more)" if len(outside) > 5 else ""
            log(
                f"Note: {len(outside)} media path(s) are outside the Output folder "
                f"(OK — Output is only the deliverable destination). e.g. {sample}{more}"
            )

    delivery = media_root
    log(f"Current timeline: {source.GetName()}")
    log(f"Output folder: {media_root}")
    log(
        "Export selection: "
        f"XML={'ON' if do_xml else 'OFF'} / "
        f"AAF={'ON' if do_aaf else 'OFF'} / "
        f"MP4={'ON' if do_mp4 else 'OFF'}"
    )
    if state["recovered_from"]:
        log(f"Recovered from a previous failed copy; switched back to source timeline: {source.GetName()}")
    log(f"Media path check passed: {len(state['media_paths'])} files")
    source_video_items = count_items(source, "video")
    source_audio_items = count_items(source, "audio")

    base_name = safe_name(source.GetName())
    xml_path: Path | None = None
    aaf_path: Path | None = None
    mp4_path: Path | None = None
    mp4_settings: dict | None = None
    video_name: str | None = None
    audio_name: str | None = None
    retained_empty_tracks: dict[str, list[int]] = {}
    resolve = state["resolve"]

    if do_xml or do_aaf:
        try:
            if do_xml:
                video_name = unique_timeline_name(project, f"{source.GetName()}__VIDEO_ONLY")
                video_only = source.DuplicateTimeline(video_name)
                if not video_only:
                    raise ExportHelperError("Could not create VIDEO_ONLY timeline copy.")
                if not project.SetCurrentTimeline(video_only):
                    raise ExportHelperError("Could not switch to VIDEO_ONLY timeline copy.")
                log("Creating video-only timeline copy…")
                retained_empty_tracks["video_only_audio"] = delete_all_tracks(video_only, "audio")
                retained_empty_tracks["video_only_subtitle"] = delete_all_tracks(
                    video_only, "subtitle"
                )
                if count_items(video_only, "video") != source_video_items:
                    raise ExportHelperError("Video clip count changed while clearing audio. Export stopped.")
                xml_path = delivery / f"{base_name}_VIDEO.xml"
                if not project.SetCurrentTimeline(video_only):
                    raise ExportHelperError("Could not switch to VIDEO_ONLY timeline before XML export.")
                log("Exporting video XML…")
                if not video_only.Export(
                    str(xml_path), resolve.EXPORT_FCP_7_XML, resolve.EXPORT_NONE
                ):
                    raise ExportHelperError("Video-only XML export failed.")

            if do_aaf:
                if not project.SetCurrentTimeline(source):
                    raise ExportHelperError("Could not switch back to source timeline before AUDIO_ONLY creation.")
                audio_name = unique_timeline_name(project, f"{source.GetName()}__AUDIO_ONLY")
                audio_only = source.DuplicateTimeline(audio_name)
                if not audio_only:
                    raise ExportHelperError("Could not create AUDIO_ONLY timeline copy.")
                if not project.SetCurrentTimeline(audio_only):
                    raise ExportHelperError("Could not switch to AUDIO_ONLY timeline copy.")
                log("Creating audio-only timeline copy…")
                retained_empty_tracks["audio_only_video"] = delete_all_tracks(audio_only, "video")
                retained_empty_tracks["audio_only_subtitle"] = delete_all_tracks(
                    audio_only, "subtitle"
                )
                if count_items(audio_only, "audio") != source_audio_items:
                    raise ExportHelperError("Audio clip count changed while clearing video. Export stopped.")
                aaf_path = delivery / f"{base_name}_AUDIO.aaf"
                if not project.SetCurrentTimeline(audio_only):
                    raise ExportHelperError("Could not switch to AUDIO_ONLY timeline before AAF export.")
                log("Exporting audio AAF…")
                if not audio_only.Export(
                    str(aaf_path), resolve.EXPORT_AAF, resolve.EXPORT_AAF_NEW
                ):
                    raise ExportHelperError("Audio-only AAF export failed.")
        finally:
            if not project.SetCurrentTimeline(source):
                log(f"Warning: could not switch back to source timeline: {source.GetName()}")

    for label, artifact in (("XML", xml_path), ("AAF", aaf_path)):
        if artifact is None:
            continue
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise ExportHelperError(f"Output file missing or empty: {artifact}")

    if do_mp4:
        mp4_name = f"{base_name}_MOV"
        mp4_path, mp4_settings = render_h264_mp4(
            state=state,
            delivery=delivery,
            custom_name=mp4_name,
            width=width,
            height=height,
            bitrate_mbps=bitrate_mbps,
            log=log,
        )

    done_parts = [
        name
        for enabled, name in (
            (do_xml, "XML"),
            (do_aaf, "AAF"),
            (do_mp4, "MOV"),
        )
        if enabled
    ]
    done_label = "、".join(done_parts)
    if not state["project_manager"].SaveProject():
        raise ExportHelperError(f"{done_label} exported, but saving the Resolve project failed.")

    if xml_path:
        log(f"XML: {xml_path}")
    if aaf_path:
        log(f"AAF: {aaf_path}")
    if mp4_path:
        log(f"MOV: {mp4_path}")
    if retained_empty_tracks:
        log(f"Cleared empty tracks on export copies: {retained_empty_tracks}")
    log(f"Done: {done_label} written to {delivery}")
    return delivery


def cli_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--export", dest="export_root")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--bitrate", type=float)
    parser.add_argument("--skip-xml", action="store_true", help="Do not export video XML")
    parser.add_argument("--skip-aaf", action="store_true", help="Do not export audio AAF")
    parser.add_argument(
        "--skip-mp4",
        action="store_true",
        help="Do not export MOV video (QuickTime H.264 / MP4 fallback)",
    )
    args = parser.parse_args()
    if args.self_test:
        config = load_config()
        assert "output_subfolder" not in config
        assert path_is_under(r"D:\PKG\clip.mov", r"D:\PKG")
        assert not path_is_under(r"D:\SOURCE\clip.mov", r"D:\PKG")
        assert config.get("export_xml") is True
        settings = build_mp4_render_settings(
            Path(r"D:\out"), "clip", 0, 100, 1920, 1080, 25, 48000
        )
        # Restrict-to bitrate (UI Mbps → kbps); no soft keys in base dict.
        assert settings.get("VideoQuality") == 25000
        assert "ExportSubtitle" not in settings
        assert "EncodingProfile" not in settings
        variants = _mp4_settings_variants(
            Path(r"D:\out"), "clip", 0, 100, 1920, 1080, 25, 48000, 24.0
        )
        assert variants[0][0] == "restrict-bitrate"
        assert variants[0][1].get("VideoQuality") == 25000
        assert variants[-1][0] == "bare"
        assert "VideoQuality" not in variants[-1][1]
        assert render_container_extension("QuickTime") == ".mov"
        assert render_container_extension("mov") == ".mov"
        assert render_container_extension("mp4") == ".mp4"
        print("SELF_TEST_PASS")
        return 0
    if args.preflight:
        state = current_timeline_state()
        print(json.dumps({
            "result": "PASS",
            "version": state["version"],
            "project": state["project"].GetName(),
            "timeline": state["timeline"].GetName(),
            "video_tracks": state["timeline"].GetTrackCount("video"),
            "audio_tracks": state["timeline"].GetTrackCount("audio"),
            "media_paths": len(state["media_paths"]),
            "inferred_root": str(state["inferred_root"] or ""),
            "media_root_warning": state.get("media_root_warning"),
            "mark_in": state["mark_in"],
            "mark_out": state["mark_out"],
            "mark_in_tc": state.get("mark_in_tc"),
            "mark_out_tc": state.get("mark_out_tc"),
            "timeline_fps": state.get("timeline_fps"),
            "mark_source": state.get("mark_source"),
        }, ensure_ascii=False))
        return 0
    if args.export_root:
        print(export_current_timeline(
            Path(args.export_root),
            print,
            args.width,
            args.height,
            args.bitrate,
            export_xml=not args.skip_xml,
            export_aaf=not args.skip_aaf,
            export_mp4=not args.skip_mp4,
        ))
        return 0
    launch_gui()
    return 0


def launch_gui() -> None:
    try:
        from PyQt6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, pyqtSignal
        from PyQt6.QtGui import (
            QColor,
            QFont,
            QIcon,
            QMouseEvent,
            QPainter,
            QPen,
            QTextCursor,
        )
        from PyQt6.QtWidgets import (
            QApplication,
            QFileDialog,
            QFrame,
            QGraphicsDropShadowEffect,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise ExportHelperError(
            "PyQt6 is missing. Run: python -m pip install PyQt6"
        ) from exc

    # Must run before QApplication so Windows taskbar uses our icon, not python.exe.
    set_windows_app_user_model_id()
    enable_high_dpi()
    config = load_config()

    # Dark panels + 01 dotted stage + 02 white glow language (no blue, square corners).
    BG = "#1D1D1D"
    CARD = "#202020"
    INPUT_BG = "#1D1D1D"
    TEXT = "#e6e6e6"
    MUTED = "#9a9a9a"
    WARN_ORANGE = "#ff6a35"  # incomplete In/Out hint
    ON_LIGHT = "#202020"
    KEY_OFF = "#c8c8c8"
    KEY_ON = "#f4f4f4"
    KEY_HOVER = "#ffffff"
    FONT = "Microsoft JhengHei UI"
    GLOW = QColor(235, 238, 245, 150)
    GLOW_STRONG = QColor(255, 255, 255, 200)

    APP_STYLESHEET = f"""
    QMainWindow, QWidget#root {{
        background-color: {BG};
        color: {TEXT};
        font-family: "{FONT}";
        font-size: 13px;
    }}
    QWidget#root {{
        border: 1px solid #2c2c2c;
        border-radius: 0px;
    }}
    QLabel#sectionTitle {{
        color: {TEXT};
        font-size: 13px;
        font-weight: 600;
    }}
    QLabel#fieldLabel {{
        color: {MUTED};
        font-size: 11px;
        font-weight: 600;
    }}
    QLabel#status {{
        color: {MUTED};
        font-size: 12px;
    }}
    QLabel#credit, QPushButton#credit {{
        color: {MUTED};
        background: transparent;
        border: none;
        font-size: 11px;
        font-weight: 400;
        padding: 0px;
        text-align: right;
    }}
    QPushButton#credit:hover {{
        color: #d8d8d8;
        background: transparent;
        border: none;
    }}
    QPushButton#credit:pressed {{
        color: #ffffff;
        background: transparent;
        border: none;
    }}
    QFrame#card {{
        background-color: {CARD};
        border: 1px solid #434343;
        border-radius: 0px;
    }}
    QFrame#card:disabled {{
        background-color: #1a1a1a;
        border: 1px solid #343434;
    }}
    QFrame#card:disabled QLabel#sectionTitle,
    QFrame#card:disabled QLabel#fieldLabel {{
        color: #666666;
    }}
    QFrame#pill {{
        background-color: {CARD};
        border: 1px solid #434343;
        border-radius: 0px;
    }}
    QLabel#pillText {{
        color: {TEXT};
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel#pillDot {{
        color: #f0f0f0;
        font-size: 20px;
        font-weight: 700;
        min-width: 18px;
    }}
    QLineEdit {{
        background-color: {INPUT_BG};
        color: {TEXT};
        border: none;
        border-radius: 0px;
        padding: 9px 12px;
        selection-background-color: #d8d8d8;
        selection-color: {ON_LIGHT};
    }}
    QLineEdit:focus {{
        background-color: #1c1c1c;
        border: 1px solid #4a4a4a;
    }}
    QLineEdit:disabled {{
        color: #666666;
        background-color: #1D1D1D;
    }}
    QTextEdit#log {{
        background-color: #1D1D1D;
        color: #cfcfcf;
        border: none;
        border-radius: 0px;
        padding: 8px;
        font-family: Consolas, "Cascadia Mono", "Microsoft JhengHei UI";
        font-size: 12px;
    }}
    QPushButton {{
        background-color: #2a2a2a;
        color: {TEXT};
        border: none;
        border-radius: 0px;
        padding: 10px 16px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background-color: #333333;
    }}
    QPushButton:pressed {{
        background-color: #242424;
    }}
    QPushButton:disabled {{
        color: #666666;
        background-color: #1c1c1c;
    }}
    QPushButton#ghost {{
        background-color: #2a2a2a;
        color: {TEXT};
        border: none;
        border-radius: 0px;
    }}
    QPushButton#ghost:hover {{
        background-color: #353535;
    }}
    QPushButton#accent {{
        background-color: {KEY_ON};
        color: {ON_LIGHT};
        border: none;
        border-radius: 0px;
        padding: 11px 28px;
        font-size: 14px;
        font-weight: 700;
        letter-spacing: 4px;
    }}
    QPushButton#accent:hover {{
        background-color: {KEY_HOVER};
        color: {ON_LIGHT};
    }}
    QPushButton#accent:pressed {{
        background-color: #e0e0e0;
        color: {ON_LIGHT};
    }}
    QPushButton#accent:disabled {{
        background-color: #3a3a3a;
        color: #777777;
    }}
    QPushButton#exportToggle {{
        background-color: {KEY_OFF};
        color: {ON_LIGHT};
        border: none;
        border-radius: 0px;
        padding: 10px 16px;
        font-size: 13px;
        font-weight: 700;
        min-width: 104px;
        text-align: left;
    }}
    QPushButton#exportToggle:hover {{
        background-color: {KEY_HOVER};
        color: {ON_LIGHT};
    }}
    QPushButton#exportToggle:checked {{
        background-color: {KEY_ON};
        color: {ON_LIGHT};
    }}
    QPushButton#exportToggle:checked:hover {{
        background-color: {KEY_HOVER};
        color: {ON_LIGHT};
    }}
    QPushButton#exportToggle:disabled {{
        color: #555555;
        background-color: #8a8a8a;
    }}
    QPushButton#winClose {{
        background-color: transparent;
        color: #9a9a9a;
        border: none;
        border-radius: 0px;
        padding: 4px 10px;
        font-size: 14px;
        font-weight: 700;
        min-width: 0;
    }}
    QPushButton#winClose:hover {{
        background-color: #3a2222;
        color: #ffb0b0;
    }}
    QToolTip {{
        background-color: #2a2a2a;
        color: {TEXT};
        border: 1px solid #3a3a3a;
        padding: 6px 8px;
        font-size: 12px;
    }}
    """

    def make_white_glow(
        widget: QWidget,
        blur: int = 28,
        alpha: int = 140,
        strong: bool = False,
    ) -> None:
        effect = QGraphicsDropShadowEffect(widget)
        effect.setBlurRadius(blur)
        effect.setOffset(0, 0)
        color = QColor(GLOW_STRONG if strong else GLOW)
        color.setAlpha(alpha)
        effect.setColor(color)
        widget.setGraphicsEffect(effect)

    def clear_glow(widget: QWidget) -> None:
        widget.setGraphicsEffect(None)

    class DotBackground(QWidget):
        """Dotted dark backdrop. Density is controlled from config.json."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            # Read once at construct so user edits apply next launch.
            self._dot_step = max(6, int(config.get("ui_dot_step", 14)))
            self._dot_radius = max(1, int(config.get("ui_dot_radius", 1)))
            self._dot_alpha = max(0, min(255, int(config.get("ui_dot_alpha", 52))))

        def paintEvent(self, event) -> None:  # noqa: N802
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(BG))
            step = self._dot_step
            radius = self._dot_radius
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, self._dot_alpha))
            for y in range(step // 2, self.height() + step, step):
                for x in range(step // 2, self.width() + step, step):
                    painter.drawEllipse(QPoint(x, y), radius, radius)
            painter.end()

    class ExportWorker(QThread):
        log_line = pyqtSignal(str)
        finished_ok = pyqtSignal(str)
        finished_err = pyqtSignal(str)

        def __init__(
            self,
            media_root: Path,
            width: int,
            height: int,
            bitrate: float,
            export_xml: bool,
            export_aaf: bool,
            export_mp4: bool,
            parent=None,
        ):
            super().__init__(parent)
            self.media_root = media_root
            self.width = width
            self.height = height
            self.bitrate = bitrate
            self.export_xml = export_xml
            self.export_aaf = export_aaf
            self.export_mp4 = export_mp4

        def run(self) -> None:
            try:
                result = export_current_timeline(
                    self.media_root,
                    lambda message: self.log_line.emit(message),
                    self.width,
                    self.height,
                    self.bitrate,
                    export_xml=self.export_xml,
                    export_aaf=self.export_aaf,
                    export_mp4=self.export_mp4,
                )
                self.finished_ok.emit(str(result))
            except Exception as exc:
                traceback.print_exc()
                self.finished_err.emit(str(exc))

    class ExportHelperWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("WiKiVibe ExportHelper")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.setMinimumSize(800, 640)
            self.resize(900, 720)
            self._config = dict(config)
            self._worker: ExportWorker | None = None
            self._drag_pos: QPoint | None = None
            self._timeline_summary = "Timeline not loaded"

            root = DotBackground()
            root.setObjectName("root")
            self.setCentralWidget(root)
            outer = QVBoxLayout(root)
            outer.setContentsMargins(16, 10, 16, 10)
            outer.setSpacing(10)

            # Minimal chrome: drag area + close only (no product title block).
            chrome = QHBoxLayout()
            chrome.setContentsMargins(2, 0, 0, 0)
            chrome.addStretch(1)
            close_btn = QPushButton("×")
            close_btn.setObjectName("winClose")
            close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_btn.setFixedSize(32, 28)
            close_btn.clicked.connect(self.close)
            chrome.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)
            outer.addLayout(chrome)

            pill = QFrame()
            pill.setObjectName("pill")
            pill_layout = QHBoxLayout(pill)
            pill_layout.setContentsMargins(14, 10, 14, 10)
            pill_layout.setSpacing(10)
            self.pill_dot = QLabel("●")
            self.pill_dot.setObjectName("pillDot")
            self.timeline_label = QLabel("Resolve timeline not loaded")
            self.timeline_label.setObjectName("pillText")
            self.timeline_label.setWordWrap(True)
            pill_layout.addWidget(self.pill_dot, 0, Qt.AlignmentFlag.AlignVCenter)
            pill_layout.addWidget(self.timeline_label, 1)
            outer.addWidget(pill)

            # Output path card
            path_card = QFrame()
            path_card.setObjectName("card")
            path_layout = QVBoxLayout(path_card)
            path_layout.setContentsMargins(14, 12, 14, 12)
            path_layout.setSpacing(8)
            path_layout.addWidget(self._section_title("Output folder"))
            path_row = QHBoxLayout()
            path_row.setSpacing(8)
            self.media_root_edit = QLineEdit()
            self.media_root_edit.setPlaceholderText(
                "Where to write XML / AAF / MOV video (any folder you like)…"
            )
            browse_btn = QPushButton("Browse…")
            browse_btn.setObjectName("ghost")
            browse_btn.clicked.connect(self.choose_root)
            path_row.addWidget(self.media_root_edit, 1)
            path_row.addWidget(browse_btn, 0)
            path_layout.addLayout(path_row)
            outer.addWidget(path_card)

            # Export type toggles
            export_card = QFrame()
            export_card.setObjectName("card")
            export_layout = QVBoxLayout(export_card)
            export_layout.setContentsMargins(14, 12, 14, 12)
            export_layout.setSpacing(8)
            export_layout.addWidget(self._section_title("Export items"))
            toggle_row = QHBoxLayout()
            toggle_row.setSpacing(10)
            self.toggle_xml = self._make_export_toggle(
                "XML", bool(self._config.get("export_xml", True))
            )
            self.toggle_aaf = self._make_export_toggle(
                "AAF", bool(self._config.get("export_aaf", True))
            )
            self.toggle_mp4 = self._make_export_toggle(
                "MOV", bool(self._config.get("export_mp4", True))
            )
            for btn in (self.toggle_xml, self.toggle_aaf, self.toggle_mp4):
                btn.toggled.connect(self._on_export_toggles_changed)
                btn.installEventFilter(self)
                toggle_row.addWidget(btn)
            toggle_row.addStretch(1)
            export_layout.addLayout(toggle_row)
            outer.addWidget(export_card)

            # Video export settings
            mp4_card = QFrame()
            mp4_card.setObjectName("card")
            self.mp4_card = mp4_card
            mp4_layout = QVBoxLayout(mp4_card)
            mp4_layout.setContentsMargins(14, 12, 14, 12)
            mp4_layout.setSpacing(8)
            mp4_layout.addWidget(self._section_title("Video export"))
            fields = QHBoxLayout()
            fields.setSpacing(12)
            self.width_edit = QLineEdit(str(self._config["mp4_width"]))
            self.height_edit = QLineEdit(str(self._config["mp4_height"]))
            self.bitrate_edit = QLineEdit(f"{float(self._config['mp4_bitrate_mbps']):g}")
            for edit in (self.width_edit, self.height_edit, self.bitrate_edit):
                edit.setMaximumWidth(140)
            fields.addLayout(self._field_column("Width", self.width_edit))
            fields.addLayout(self._field_column("Height", self.height_edit))
            fields.addLayout(
                self._field_column("Restrict to（Mbps）", self.bitrate_edit)
            )
            fields.addStretch(1)
            mp4_layout.addLayout(fields)
            outer.addWidget(mp4_card)

            # Log card — keep compact so bottom actions stay tight.
            log_card = QFrame()
            log_card.setObjectName("card")
            log_layout = QVBoxLayout(log_card)
            log_layout.setContentsMargins(14, 10, 14, 10)
            log_layout.setSpacing(6)
            log_layout.addWidget(self._section_title("Log"))
            self.log_box = QTextEdit()
            self.log_box.setObjectName("log")
            self.log_box.setReadOnly(True)
            self.log_box.setMinimumHeight(120)
            self.log_box.setMaximumHeight(180)
            log_layout.addWidget(self.log_box, 1)
            outer.addWidget(log_card, 1)

            # Actions
            actions = QHBoxLayout()
            actions.setContentsMargins(0, 2, 0, 0)
            actions.setSpacing(10)
            self.refresh_btn = QPushButton("Refresh")
            self.refresh_btn.setObjectName("ghost")
            self.refresh_btn.clicked.connect(lambda: self.refresh_state(show_error=True))
            self.export_btn = QPushButton("Export")
            self.export_btn.setObjectName("accent")
            self.export_btn.clicked.connect(self.export_clicked)
            self.export_btn.installEventFilter(self)
            make_white_glow(self.export_btn, blur=30, alpha=120, strong=False)
            actions.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignLeft)
            actions.addStretch(1)
            actions.addWidget(self.export_btn, 0, Qt.AlignmentFlag.AlignRight)
            outer.addLayout(actions)

            footer = QHBoxLayout()
            footer.setContentsMargins(0, 0, 0, 0)
            footer.setSpacing(8)
            self.status_label = QLabel(self._timeline_summary)
            self.status_label.setObjectName("status")
            self.status_label.setWordWrap(True)
            # Flat text button — avoids Qt/system orange link + underline styling.
            self.credit_btn = QPushButton("by WiKiVibe")
            self.credit_btn.setObjectName("credit")
            self.credit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.credit_btn.setFlat(True)
            self.credit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.credit_btn.setToolTip("https://github.com/WiKiVibe")
            self.credit_btn.clicked.connect(self._open_credit_link)
            footer.addWidget(self.status_label, 1)
            footer.addWidget(self.credit_btn, 0, Qt.AlignmentFlag.AlignRight)
            outer.addLayout(footer)

            self._on_export_toggles_changed()
            QTimer.singleShot(220, lambda: self.refresh_state(show_error=False))

        @staticmethod
        def _open_credit_link() -> None:
            import webbrowser

            webbrowser.open("https://github.com/WiKiVibe")

        def eventFilter(self, obj, event):  # noqa: N802
            toggles = (self.toggle_xml, self.toggle_aaf, self.toggle_mp4)
            if obj in toggles:
                if event.type() == QEvent.Type.Enter:
                    # Hover brightness matches an active (glowing) key.
                    make_white_glow(obj, blur=32, alpha=180, strong=True)
                elif event.type() == QEvent.Type.Leave:
                    self._apply_toggle_glow(obj)
                return False
            if obj is self.export_btn:
                if event.type() == QEvent.Type.Enter and obj.isEnabled():
                    make_white_glow(obj, blur=40, alpha=210, strong=True)
                elif event.type() == QEvent.Type.Leave:
                    if obj.isEnabled():
                        make_white_glow(obj, blur=30, alpha=120, strong=False)
                    else:
                        clear_glow(obj)
                return False
            return super().eventFilter(obj, event)

        def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
            if event.button() == Qt.MouseButton.LeftButton:
                # Don't start window-drag when clicking the credit control.
                hit = self.childAt(event.position().toPoint())
                if hit is not None and (
                    hit is getattr(self, "credit_btn", None)
                    or hit.objectName() == "credit"
                ):
                    super().mousePressEvent(event)
                    return
                self._drag_pos = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
            if (
                self._drag_pos is not None
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
            self._drag_pos = None
            super().mouseReleaseEvent(event)

        @staticmethod
        def _section_title(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("sectionTitle")
            return label

        @staticmethod
        def _field_column(caption: str, widget: QWidget) -> QVBoxLayout:
            col = QVBoxLayout()
            col.setSpacing(6)
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            col.addWidget(label)
            col.addWidget(widget)
            return col

        @staticmethod
        def _make_export_toggle(name: str, checked: bool) -> QPushButton:
            btn = QPushButton()
            btn.setObjectName("exportToggle")
            btn.setCheckable(True)
            btn.setChecked(checked)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("export_name", name)
            # Larger marker dots for readability on light keys.
            btn.setText(f"{'●' if checked else '○'}  {name}")
            return btn

        def _selected_exports(self) -> tuple[bool, bool, bool]:
            return (
                self.toggle_xml.isChecked(),
                self.toggle_aaf.isChecked(),
                self.toggle_mp4.isChecked(),
            )

        def _apply_toggle_glow(self, btn: QPushButton) -> None:
            if btn.isChecked():
                make_white_glow(btn, blur=28, alpha=150, strong=True)
            else:
                clear_glow(btn)

        def _sync_toggle_labels(self) -> None:
            for btn in (self.toggle_xml, self.toggle_aaf, self.toggle_mp4):
                name = btn.property("export_name")
                btn.setText(f"{'●' if btn.isChecked() else '○'}  {name}")
                self._apply_toggle_glow(btn)

        def _export_summary(self) -> str:
            do_xml, do_aaf, do_mp4 = self._selected_exports()
            parts = [
                name
                for enabled, name in (
                    (do_xml, "XML"),
                    (do_aaf, "AAF"),
                    (do_mp4, "MOV"),
                )
                if enabled
            ]
            return " + ".join(parts) if parts else "(none)"

        def _set_timeline_status(self, text: str) -> None:
            self._timeline_summary = text
            self.status_label.setText(text)

        def _on_export_toggles_changed(self, *_args) -> None:
            self._sync_toggle_labels()
            do_xml, do_aaf, do_mp4 = self._selected_exports()
            mp4_on = do_mp4
            self.width_edit.setEnabled(mp4_on)
            self.height_edit.setEnabled(mp4_on)
            self.bitrate_edit.setEnabled(mp4_on)
            self.mp4_card.setEnabled(mp4_on)
            self.export_btn.setText("Export")
            enabled = bool(do_xml or do_aaf or do_mp4)
            self.export_btn.setEnabled(enabled)
            if enabled:
                make_white_glow(self.export_btn, blur=30, alpha=120, strong=False)
            else:
                clear_glow(self.export_btn)

        def append_log(self, message: str) -> None:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.log_box.append(f"[{stamp}] {message}")
            # Keep bottom status for timeline summary only.
            cursor = self.log_box.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.log_box.setTextCursor(cursor)

        def set_busy(self, busy: bool) -> None:
            self.refresh_btn.setEnabled(not busy)
            self.media_root_edit.setEnabled(not busy)
            self.toggle_xml.setEnabled(not busy)
            self.toggle_aaf.setEnabled(not busy)
            self.toggle_mp4.setEnabled(not busy)
            if busy:
                self.export_btn.setEnabled(False)
                self.width_edit.setEnabled(False)
                self.height_edit.setEnabled(False)
                self.bitrate_edit.setEnabled(False)
                clear_glow(self.export_btn)
            else:
                self._on_export_toggles_changed()

        def choose_root(self) -> None:
            selected = QFileDialog.getExistingDirectory(
                self,
                "Select output folder (files are written here directly)",
                self.media_root_edit.text().strip() or str(Path.home()),
            )
            if selected:
                self.media_root_edit.setText(selected)

        def refresh_state(self, show_error: bool = True) -> bool:
            try:
                state = current_timeline_state()
                timeline_name = state["timeline"].GetName()
                v_count = int(state["timeline"].GetTrackCount("video"))
                a_count = int(state["timeline"].GetTrackCount("audio"))
                media_count = len(state["media_paths"])
                in_tc = state.get("mark_in_tc") or "00:00:00:00"
                out_tc = state.get("mark_out_tc")
                marks_warn = False
                if state["mark_out"] is not None:
                    if out_tc:
                        marks = f"In {in_tc} / Out {out_tc}"
                    else:
                        marks = f"In {state['mark_in']} / Out {state['mark_out']}"
                    if state.get("mark_in_defaulted"):
                        marks = f"{marks} (In default 00:00:00:00)"
                else:
                    # Missing Out (or both marks unreadable): warn in orange-red.
                    right = out_tc if out_tc else "?"
                    marks = f"In {in_tc} / Out {right} (incomplete)"
                    marks_warn = True
                project_name = state["project"].GetName()
                if marks_warn:
                    self.timeline_label.setTextFormat(Qt.TextFormat.RichText)
                    self.timeline_label.setText(
                        f"{project_name}  /  {timeline_name}  ·  "
                        f'<span style="color:{WARN_ORANGE};">{marks}</span>'
                    )
                    self.pill_dot.setStyleSheet(f"color: {WARN_ORANGE};")
                else:
                    self.timeline_label.setTextFormat(Qt.TextFormat.PlainText)
                    self.timeline_label.setText(
                        f"{project_name}  /  {timeline_name}  ·  {marks}"
                    )
                    self.pill_dot.setStyleSheet("color: #f0f0f0;")
                # Bottom status: compact timeline readout.
                self._set_timeline_status(
                    f"{timeline_name} ; V{v_count}/A{a_count}/{media_count}Media"
                )
                if state.get("inferred_root"):
                    self.media_root_edit.setText(str(state["inferred_root"]))
                else:
                    # Keep any path the user already typed; don't wipe the field.
                    if not self.media_root_edit.text().strip():
                        self.media_root_edit.setText("")
                if state["recovered_from"]:
                    self.append_log(
                        f"Detected previous failed copy {state['recovered_from']}; switched back to source timeline."
                    )
                self.append_log(
                    "Timeline loaded; "
                    f"V{v_count} / A{a_count} / {media_count} media paths"
                )
                if state.get("media_root_warning"):
                    self.append_log(str(state["media_root_warning"]))
                ok_pkg, pkg_root, pkg_msg = check_media_same_package_tree(state["media_paths"])
                if ok_pkg and pkg_root is not None:
                    self.append_log(f"Media package tree: {pkg_root}")
                elif pkg_msg:
                    first = pkg_msg.splitlines()[0]
                    self.append_log(f"Media package tree: FAIL — {first}")
                if state["mark_out"] is not None:
                    in_tc = state.get("mark_in_tc") or "00:00:00:00"
                    out_tc = state.get("mark_out_tc") or "?"
                    fps = state.get("timeline_fps")
                    fps_label = f"{fps:g}" if isinstance(fps, (int, float)) else "?"
                    default_note = (
                        "; In defaulted to 00:00:00:00"
                        if state.get("mark_in_defaulted")
                        else ""
                    )
                    self.append_log(
                        f"In/Out: {in_tc} → {out_tc}  "
                        f"(frames {state['mark_in']}–{state['mark_out']}, "
                        f"{fps_label} fps, source: {state.get('mark_source') or 'timeline'}"
                        f"{default_note})"
                    )
                else:
                    self.append_log(
                        "Out mark not readable yet (In defaults to 00:00:00:00). "
                        "Press O on the timeline, or click Refresh."
                    )
                return True
            except Exception as exc:
                self.timeline_label.setTextFormat(Qt.TextFormat.PlainText)
                self.timeline_label.setText("Timeline not ready")
                self.pill_dot.setStyleSheet(f"color: {MUTED};")
                self._set_timeline_status("Timeline not loaded")
                self.append_log(f"Load failed: {exc}")
                if show_error:
                    QMessageBox.critical(self, "Could not load current timeline", str(exc))
                return False

        def export_clicked(self) -> None:
            do_xml, do_aaf, do_mp4 = self._selected_exports()
            if not (do_xml or do_aaf or do_mp4):
                QMessageBox.warning(
                    self, "Nothing selected", "Enable at least one of: XML, AAF, or MOV."
                )
                return

            selected_root = Path(self.media_root_edit.text()).expanduser()
            if not selected_root.is_dir():
                QMessageBox.critical(
                    self,
                    "Folder error",
                    "Please select an output folder. XML / AAF / MOV video are written there directly.",
                )
                return

            width = int(self._config["mp4_width"])
            height = int(self._config["mp4_height"])
            bitrate = float(self._config["mp4_bitrate_mbps"])
            if do_mp4:
                try:
                    width = int(self.width_edit.text().strip())
                    height = int(self.height_edit.text().strip())
                    bitrate = float(self.bitrate_edit.text().strip())
                    build_mp4_render_settings(
                        selected_root,
                        "validation",
                        0,
                        1,
                        width,
                        height,
                        bitrate,
                        int(self._config["mp4_audio_sample_rate"]),
                    )
                except (ValueError, ExportHelperError) as exc:
                    QMessageBox.critical(self, "MOV settings error", str(exc))
                    return

            updated = dict(self._config)
            updated.update(
                {
                    "export_xml": do_xml,
                    "export_aaf": do_aaf,
                    "export_mp4": do_mp4,
                    "mp4_width": width,
                    "mp4_height": height,
                    "mp4_bitrate_mbps": bitrate,
                }
            )
            save_config(updated)
            self._config = updated

            lines = ["Will export:\n"]
            if do_xml:
                lines.append("• Video-only XML")
            if do_aaf:
                lines.append("• Audio-only AAF")
            if do_mp4:
                lines.append(
                    f"• MOV (H.264, MP4 fallback) In/Out "
                    f"({width}×{height} / Restrict to {bitrate:g} Mbps)"
                )
            lines.append("\nThe original timeline will not be modified.")
            reply = QMessageBox.question(
                self,
                "Start export",
                "\n".join(lines),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return

            self.set_busy(True)
            self.append_log(f"Starting export: {self._export_summary()}…")
            worker = ExportWorker(
                selected_root,
                width,
                height,
                bitrate,
                do_xml,
                do_aaf,
                do_mp4,
                self,
            )
            worker.log_line.connect(self.append_log)
            worker.finished_ok.connect(self._on_export_ok)
            worker.finished_err.connect(self._on_export_err)
            worker.finished.connect(lambda: self.set_busy(False))
            self._worker = worker
            worker.start()

        def _on_export_ok(self, result_path: str) -> None:
            QMessageBox.information(
                self,
                "Done",
                f"{self._export_summary()} and report written to:\n{result_path}",
            )

        def _on_export_err(self, message: str) -> None:
            self.append_log(f"Stopped: {message}")
            QMessageBox.critical(self, "Export stopped", message)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont(FONT, 10))
    app.setStyleSheet(APP_STYLESHEET)
    app_icon = load_app_icon()
    if app_icon is not None:
        app.setWindowIcon(app_icon)
    window = ExportHelperWindow()
    if app_icon is not None:
        window.setWindowIcon(app_icon)
    window.show()
    # Win32 override: without this, taskbar often keeps the generic Python .py icon.
    apply_windows_taskbar_icon(window)
    # Re-apply after the window is fully mapped (some Windows builds ignore the first set).
    QTimer.singleShot(0, lambda: apply_windows_taskbar_icon(window))
    app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(cli_main())
    except ExportHelperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

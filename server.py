"""
SafeCleanup - local disk space / app uninstall dashboard.

Everything here is stdlib-only (no pip installs needed). Run with:
    python server.py
then open http://localhost:8765 in your browser.

What it finds (Programs tab):
  - Classic installed apps (Windows registry Uninstall keys) -> real uninstaller.
  - Microsoft Store / UWP apps (Get-AppxPackage) -> Remove-AppxPackage.
  - "Files only" installs: folders under Program Files / Program Files (x86) /
    AppData\\Local\\Programs that don't belong to any app above (portable apps,
    manually-copied tools). These have no uninstaller, so "removing" them just
    means moving that folder to the Recycle Bin.

Safety model:
  - Deleting a file/folder NEVER permanently erases it: it goes to the
    Recycle Bin via the Windows Shell API, so it can be restored.
  - Paths classified as "system_critical" (inside C:\\Windows, WindowsApps,
    System Volume Information, boot files, etc.) can never be deleted
    through this tool, full stop - enforced on the server, not just the UI.
  - Paths that belong to an installed app's folder are also blocked from
    direct deletion; the UI points you to remove that app instead.
  - Uninstalling a classic app always launches that app's OWN real
    uninstaller, same as Windows Settings > Apps would.
"""

import ctypes
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import winreg
from ctypes import wintypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 8765

WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows")).resolve()
SYSTEM_DRIVE_LETTER = os.environ.get("SystemDrive", "C:")
SYSTEM_DRIVE = Path(SYSTEM_DRIVE_LETTER + "\\").resolve()
USER_PROFILE = Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve()
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(USER_PROFILE / "AppData" / "Local")))

# Running as a PyInstaller --onefile exe? Static assets are bundled read-only inside the
# exe (extracted to a temp dir at sys._MEIPASS); anything we need to persist (the size
# cache, crash logs) has to live somewhere real instead, next to per-user app data.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
    DATA_DIR = LOCALAPPDATA / "SafeCleanup"
else:
    BASE_DIR = Path(__file__).resolve().parent
    DATA_DIR = BASE_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = BASE_DIR / "static"
CACHE_FILE = DATA_DIR / "size_cache.json"
ERROR_LOG = DATA_DIR / "error.log"


def get_onedrive_root():
    for var in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        val = os.environ.get(var)
        if val:
            p = Path(val)
            if p.exists():
                return p.resolve()
    guess = USER_PROFILE / "OneDrive"
    return guess.resolve() if guess.exists() else None


ONEDRIVE_ROOT = get_onedrive_root()
ONEDRIVE_KNOWN_FOLDERS = ("Desktop", "Documents", "Pictures", "Screenshots", "Music", "Videos")
UNLINKED_ROOT = USER_PROFILE / "Files Unlinked from OneDrive"

SYSTEM_CRITICAL_DIRS = [
    WINDIR,
    SYSTEM_DRIVE / "Program Files" / "WindowsApps",
    SYSTEM_DRIVE / "System Volume Information",
    SYSTEM_DRIVE / "$Recycle.Bin",
    SYSTEM_DRIVE / "Recovery",
    SYSTEM_DRIVE / "PerfLogs",
    SYSTEM_DRIVE / "Boot",
]
SYSTEM_CRITICAL_FILES = {
    "pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "bootnxt",
    "ntldr", "boot.ini", "config.sys", "io.sys", "msdos.sys",
}

# Patterns matched against the lowercase full path (regenerable cache/temp data)
CACHE_LIKE_PATTERNS = [
    re.compile(r"\\appdata\\local\\temp\\?", re.I),
    re.compile(r"\\windows\\temp\\?", re.I),
    re.compile(r"\\windows\\softwaredistribution\\download\\?", re.I),
    re.compile(r"\\windows\\prefetch\\?", re.I),
    re.compile(r"\\windows\.old\\?", re.I),
    re.compile(r"\\appdata\\local\\[^\\]+\\cache\\?", re.I),
    re.compile(r"\\appdata\\local\\[^\\]+\\crashdumps\\?", re.I),
    re.compile(r"\\appdata\\local\\[^\\]+\\code cache\\?", re.I),
    re.compile(r"\\appdata\\local\\package cache\\?", re.I),
    re.compile(r"\\appdata\\local\\d3dscache\\?", re.I),
    re.compile(r"\\appdata\\local\\nvidia\\?", re.I),
    re.compile(r"\\appdata\\local\\amd\\?", re.I),
    re.compile(r"\\node_modules\\?", re.I),
    re.compile(r"\\\.cache\\?", re.I),
    re.compile(r"\\\$recycle\.bin\\?", re.I),
    re.compile(r"\\recycler\\?", re.I),
]

PERSONAL_SUBDIRS = ("Documents", "Pictures", "Desktop", "Videos", "Music", "Downloads")

CANDIDATE_INSTALL_ROOTS = [
    SYSTEM_DRIVE / "Program Files",
    SYSTEM_DRIVE / "Program Files (x86)",
    LOCALAPPDATA / "Programs",
]
SKIP_UNMANAGED_NAMES = {"windowsapps", "common files", "common files (x86)"}

# Folder names that show up directly under Program Files / Program Files (x86) but are
# core Windows/.NET/dev-toolchain components, not removable "apps" - never deletable,
# even though they have no registry Uninstall entry of their own.
SYSTEM_COMPONENT_FOLDER_NAMES = {
    "windows defender", "windows defender advanced threat protection", "windowspowershell",
    "windows nt", "windows mail", "windows media player", "windows photo viewer",
    "windows sidebar", "internet explorer", "microsoft.net", ".net", "reference assemblies",
    "installshield installation information", "uninstall information", "modifiablewindowsapps",
    "msbuild", "windows kits", "windows defender security intelligence", "windows security",
    "common files", "common files (x86)", "windowsapps",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Classic installed apps (registry Uninstall keys)
# ---------------------------------------------------------------------------

UNINSTALL_HIVES = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
]

_installed_cache = {"apps": [], "ts": 0}
_installed_lock = threading.Lock()


def _reg_get(key, name, default=None):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def list_installed_apps(force=False):
    with _installed_lock:
        if not force and _installed_cache["apps"] and (time.time() - _installed_cache["ts"] < 30):
            return _installed_cache["apps"]

        seen = {}
        for hive, path, extra_flag in UNINSTALL_HIVES:
            try:
                key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | extra_flag)
            except OSError:
                continue
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                except OSError:
                    continue
                try:
                    subkey = winreg.OpenKey(key, subkey_name)
                except OSError:
                    continue
                name = _reg_get(subkey, "DisplayName")
                if not name:
                    subkey.Close()
                    continue
                if _reg_get(subkey, "SystemComponent") == 1:
                    subkey.Close()
                    continue
                if _reg_get(subkey, "ParentKeyName"):
                    subkey.Close()
                    continue
                if _reg_get(subkey, "ReleaseType") in ("Update", "Hotfix", "SecurityUpdate"):
                    subkey.Close()
                    continue

                install_location = _reg_get(subkey, "InstallLocation") or None
                if install_location:
                    install_location = install_location.strip().strip('"') or None

                app = {
                    "id": f"REG|{'HKLM' if hive == winreg.HKEY_LOCAL_MACHINE else 'HKCU'}|{subkey_name}",
                    "name": name,
                    "publisher": _reg_get(subkey, "Publisher", "") or "",
                    "version": _reg_get(subkey, "DisplayVersion", "") or "",
                    "install_location": install_location,
                    "uninstall_string": _reg_get(subkey, "UninstallString"),
                    "quiet_uninstall_string": _reg_get(subkey, "QuietUninstallString"),
                    "estimated_size_kb": _reg_get(subkey, "EstimatedSize"),
                    "install_date": _reg_get(subkey, "InstallDate", ""),
                    "type": "installer",
                }
                subkey.Close()

                dedupe_key = (name.lower(), (install_location or "").lower())
                if dedupe_key in seen:
                    if not seen[dedupe_key]["uninstall_string"] and app["uninstall_string"]:
                        seen[dedupe_key] = app
                else:
                    seen[dedupe_key] = app
            key.Close()

        apps = sorted(seen.values(), key=lambda a: a["name"].lower())
        _installed_cache["apps"] = apps
        _installed_cache["ts"] = time.time()
        return apps


# ---------------------------------------------------------------------------
# Microsoft Store / UWP apps
# ---------------------------------------------------------------------------

_uwp_cache = {"apps": [], "ts": 0}
_uwp_lock = threading.Lock()


def list_uwp_apps(force=False):
    with _uwp_lock:
        if not force and _uwp_cache["apps"] and (time.time() - _uwp_cache["ts"] < 60):
            return _uwp_cache["apps"]

        ps_cmd = (
            "Get-AppxPackage | Where-Object { -not $_.IsFramework -and -not $_.NonRemovable } | "
            "Select-Object Name, PackageFullName, Publisher, Version, InstallLocation | "
            "ConvertTo-Json -Compress"
        )
        apps = []
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=30,
            )
            raw = (result.stdout or "").strip()
            data = json.loads(raw) if raw else []
            if isinstance(data, dict):
                data = [data]
            for pkg in data:
                name = pkg.get("Name") or pkg.get("PackageFullName")
                if not name:
                    continue
                publisher = (pkg.get("Publisher") or "")
                publisher = publisher.split(",")[0].replace("CN=", "").strip()
                apps.append({
                    "id": f"UWP|{pkg.get('PackageFullName')}",
                    "name": name,
                    "publisher": publisher,
                    "version": pkg.get("Version") or "",
                    "install_location": pkg.get("InstallLocation") or None,
                    "package_full_name": pkg.get("PackageFullName"),
                    "estimated_size_kb": None,
                    "type": "uwp",
                })
        except Exception:
            apps = _uwp_cache["apps"]  # keep stale data rather than wiping on a transient failure

        _uwp_cache["apps"] = apps
        _uwp_cache["ts"] = time.time()
        return apps


# ---------------------------------------------------------------------------
# "Files only" installs - not registered anywhere, just a folder
# ---------------------------------------------------------------------------

def find_unmanaged_installs(known_apps):
    known_paths = []
    for a in known_apps:
        loc = a.get("install_location")
        if loc:
            try:
                known_paths.append(Path(loc).resolve())
            except OSError:
                continue

    def is_known(folder: Path):
        for k in known_paths:
            if folder == k or _is_relative_to(folder, k) or _is_relative_to(k, folder):
                return True
        return False

    results = []
    for root in CANDIDATE_INSTALL_ROOTS:
        if not root.exists():
            continue
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if entry.name.lower() in SKIP_UNMANAGED_NAMES:
                        continue
                    folder = Path(entry.path)
                    if is_known(folder):
                        continue
                    results.append({
                        "id": f"FILES|{folder}",
                        "name": entry.name,
                        "publisher": "",
                        "version": "",
                        "install_location": str(folder),
                        "estimated_size_kb": None,
                        "type": "files_only",
                    })
        except PermissionError:
            continue
    return results


def get_all_apps(force=False):
    installed = list_installed_apps(force=force)
    uwp = list_uwp_apps(force=force)
    unmanaged = find_unmanaged_installs(installed + uwp)
    return installed + uwp + unmanaged


def find_app_by_id(app_id):
    for a in get_all_apps():
        if a["id"] == app_id:
            return a
    return None


def uninstall_app_dispatch(app_id):
    app = find_app_by_id(app_id)
    if not app:
        raise ValueError("App not found - refresh the list and try again.")
    t = app.get("type")

    if t == "installer":
        cmd = app.get("uninstall_string") or app.get("quiet_uninstall_string")
        if not cmd:
            raise ValueError("Windows has no uninstall command recorded for this app.")
        subprocess.Popen(cmd, shell=True)
        return f"Launched the uninstaller for {app['name']}."

    if t == "uwp":
        pkg = app.get("package_full_name")
        if not pkg:
            raise ValueError("Missing package identifier for this app.")
        pkg_escaped = pkg.replace("'", "''")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Remove-AppxPackage -Package '{pkg_escaped}'"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise ValueError(f"Failed to remove {app['name']}: {(result.stderr or '').strip()[:300]}")
        return f"Removed {app['name']}."

    if t == "files_only":
        loc = app.get("install_location")
        if not loc:
            raise ValueError("No folder recorded for this item.")
        p = Path(loc)
        if not p.exists():
            raise ValueError("That folder is already gone.")
        risk = classify_path(p)
        if risk["blocked"]:
            raise ValueError(f"Blocked: {risk['reason']}")
        send_to_recycle_bin(p)
        with _cache_lock:
            _size_cache.pop(str(p).lower(), None)
        _save_cache()
        return f"Moved {app['name']}'s files to the Recycle Bin."

    raise ValueError("Unknown app type.")


# ---------------------------------------------------------------------------
# Size cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
try:
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        _size_cache = json.load(f)
except Exception:
    _size_cache = {}


def _save_cache():
    with _cache_lock:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(_size_cache, f)
        except Exception:
            pass


def _dir_mtime_signature(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def compute_dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def get_size(path: Path, force=False) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    key = str(path).lower()
    sig = _dir_mtime_signature(path)
    with _cache_lock:
        cached = _size_cache.get(key)
    if not force and cached and cached.get("sig") == sig:
        return cached["size"]
    size = compute_dir_size(path)
    with _cache_lock:
        _size_cache[key] = {"size": size, "sig": sig, "ts": time.time()}
    _save_cache()
    return size


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

def classify_path(path: Path):
    name = path.name.lower()
    full = str(path)

    if name in SYSTEM_CRITICAL_FILES:
        return {
            "level": "system_critical",
            "label": "Windows system file",
            "reason": "Core Windows boot/system file. Deleting it can stop Windows from starting.",
            "blocked": True,
        }

    for crit in SYSTEM_CRITICAL_DIRS:
        if path == crit or _is_relative_to(path, crit):
            return {
                "level": "system_critical",
                "label": "Windows system files",
                "reason": f"Inside {crit} - part of Windows itself. Never safe to delete manually.",
                "blocked": True,
            }

    if any(path.parent == root for root in CANDIDATE_INSTALL_ROOTS) and name in SYSTEM_COMPONENT_FOLDER_NAMES:
        return {
            "level": "system_critical",
            "label": "Windows/.NET component",
            "reason": f'"{path.name}" is a core Windows or .NET runtime component, not a removable app. '
                      f"Deleting it can break Windows or other installed software.",
            "blocked": True,
        }

    for app in get_all_apps():
        # "files_only" entries are unmanaged folders discovered by this same classifier;
        # never let one claim ownership of itself (or of another files_only folder) -
        # only real installers/Store apps count as an owning app here.
        if app.get("type") == "files_only":
            continue
        loc = app.get("install_location")
        if not loc:
            continue
        try:
            loc_path = Path(loc)
        except (OSError, ValueError):
            continue
        if path == loc_path or _is_relative_to(path, loc_path):
            return {
                "level": "app_owned",
                "label": f"Belongs to {app['name']}",
                "reason": f'This is part of the app "{app["name"]}". Deleting files by hand can break it '
                          f"- remove the app instead from the Programs tab.",
                "blocked": True,
                "app": app["name"],
                "app_id": app["id"],
            }

    for pat in CACHE_LIKE_PATTERNS:
        if pat.search(full):
            return {
                "level": "likely_safe_cache",
                "label": "Cache / temp data",
                "reason": "Temporary or regenerable data. Generally safe to delete; apps will recreate it if needed.",
                "blocked": False,
            }

    if ONEDRIVE_ROOT and (path == ONEDRIVE_ROOT or _is_relative_to(path, ONEDRIVE_ROOT)):
        for kf in ONEDRIVE_KNOWN_FOLDERS:
            kdir = ONEDRIVE_ROOT / kf
            if path == kdir or _is_relative_to(path, kdir):
                return {
                    "level": "onedrive_personal",
                    "label": "Personal (synced via OneDrive)",
                    "reason": "Your personal files, currently synced through OneDrive. "
                              "Use the OneDrive tab to unlink or delete them.",
                    "blocked": False,
                }
        return {
            "level": "onedrive",
            "label": "OneDrive",
            "reason": "Stored inside your OneDrive folder and synced to the cloud. "
                      "Use the OneDrive tab to unlink or delete it.",
            "blocked": False,
        }

    for sub in PERSONAL_SUBDIRS:
        pdir = USER_PROFILE / sub
        if path == pdir or _is_relative_to(path, pdir):
            return {
                "level": "personal",
                "label": "Your personal files",
                "reason": "Your own documents/media, not junk by default. Review before deleting.",
                "blocked": False,
            }

    fl = full.lower()
    if "\\appdata\\roaming\\" in fl or "\\appdata\\local\\" in fl:
        return {
            "level": "app_data_unknown",
            "label": "App data (unrecognized)",
            "reason": "Looks like settings/data for an app that isn't in your installed-apps list "
                      "(could be a portable app or leftover from an uninstall). Check the folder name first.",
            "blocked": False,
        }

    return {
        "level": "unknown",
        "label": "Unclassified",
        "reason": "Not automatically recognized. Delete with care.",
        "blocked": False,
    }


# ---------------------------------------------------------------------------
# Recycle Bin delete (never permanent)
# ---------------------------------------------------------------------------

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400


def send_to_recycle_bin(path: Path):
    p_from = str(path) + "\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = p_from
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI
    op.fAnyOperationsAborted = False
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0:
        raise OSError(f"Windows refused the delete (code {res}). It may be open in another program.")
    if op.fAnyOperationsAborted:
        raise OSError("Delete was aborted.")


def unlink_from_onedrive(path: Path):
    if not ONEDRIVE_ROOT or not _is_relative_to(path, ONEDRIVE_ROOT):
        raise ValueError("That item isn't inside your OneDrive folder.")
    rel = path.relative_to(ONEDRIVE_ROOT)
    target = UNLINKED_ROOT / rel
    if target.exists():
        raise ValueError(f'"{target}" already exists - rename or remove it first.')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    key = str(path).lower()
    with _cache_lock:
        _size_cache.pop(key, None)
    _save_cache()
    return target


# ---------------------------------------------------------------------------
# Storage overview (sidebar bar)
# ---------------------------------------------------------------------------

def candidate_cache_dirs():
    dirs = [WINDIR / "Temp", WINDIR / "SoftwareDistribution" / "Download", SYSTEM_DRIVE / "Windows.old"]
    temp_env = os.environ.get("TEMP")
    if temp_env:
        dirs.append(Path(temp_env))
    seen = set()
    out = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def build_categories():
    cats = [{"id": "windows", "label": "Windows & System", "path": str(WINDIR)}]
    if ONEDRIVE_ROOT:
        cats.append({"id": "onedrive", "label": "OneDrive", "path": str(ONEDRIVE_ROOT)})
    cats.append({"id": "apps", "label": "Apps", "path": None})
    cats.append({
        "id": "personal", "label": "Personal files", "path": None,
        "paths": [str(USER_PROFILE / s) for s in PERSONAL_SUBDIRS],
    })
    cats.append({
        "id": "cache", "label": "Cache && Temp", "path": None,
        "paths": [str(d) for d in candidate_cache_dirs()],
    })
    return cats


def compute_category_size(cat_id, force=False):
    if cat_id == "windows":
        return get_size(WINDIR, force=force)

    if cat_id == "onedrive":
        if not ONEDRIVE_ROOT:
            return 0
        return get_size(ONEDRIVE_ROOT, force=force)

    if cat_id == "apps":
        apps = get_all_apps()
        locs = []
        for a in apps:
            loc = a.get("install_location")
            if loc:
                try:
                    locs.append(Path(loc).resolve())
                except OSError:
                    continue
        locs = sorted(set(locs), key=lambda p: len(str(p)))
        top = []
        for p in locs:
            if not any(_is_relative_to(p, t) for t in top):
                top.append(p)
        return sum(get_size(p, force=force) for p in top if p.exists())

    if cat_id == "personal":
        total = 0
        for sub in PERSONAL_SUBDIRS:
            pdir = USER_PROFILE / sub
            if pdir.exists() and not (ONEDRIVE_ROOT and _is_relative_to(pdir, ONEDRIVE_ROOT)):
                total += get_size(pdir, force=force)
        return total

    if cat_id == "cache":
        return sum(get_size(d, force=force) for d in candidate_cache_dirs() if d.exists())

    return None


# ---------------------------------------------------------------------------
# Drives
# ---------------------------------------------------------------------------

def list_drives():
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            drives.append(f"{letter}:\\")
    return drives


# ---------------------------------------------------------------------------
# Native "pick a folder" dialog (for the Storage Explorer's "Browse folder..." button)
# ---------------------------------------------------------------------------

_picker_lock = threading.Lock()


def pick_folder_dialog():
    import tkinter as tk
    from tkinter import filedialog

    with _picker_lock:  # Tk isn't happy with two dialogs open at once
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            chosen = filedialog.askdirectory(
                initialdir=str(USER_PROFILE), title="Select a folder to inspect", parent=root
            )
        finally:
            root.destroy()
        return chosen or None


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def scan_directory(p: Path):
    entries = []
    with os.scandir(p) as it:
        for entry in it:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            ep = Path(entry.path)
            if is_dir:
                key = str(ep).lower()
                cached = _size_cache.get(key)
                size_bytes = cached["size"] if cached and cached.get("sig") == _dir_mtime_signature(ep) else None
            else:
                try:
                    size_bytes = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size_bytes = 0
            risk = classify_path(ep)
            entries.append({
                "name": entry.name,
                "path": str(ep),
                "is_dir": is_dir,
                "size_bytes": size_bytes,
                "risk": risk,
            })
    return entries


class Handler(BaseHTTPRequestHandler):
    server_version = "SafeCleanup/2.0"

    def log_message(self, fmt, *args):
        pass  # keep console quiet

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path == "/api/drives":
                return self._send_json({"drives": list_drives()})

            if parsed.path == "/api/onedrive/info":
                return self._send_json({"root": str(ONEDRIVE_ROOT) if ONEDRIVE_ROOT else None})

            if parsed.path == "/api/overview":
                total, used, free = shutil.disk_usage(str(SYSTEM_DRIVE))
                return self._send_json({
                    "drive": str(SYSTEM_DRIVE),
                    "total_bytes": total,
                    "used_bytes": used,
                    "free_bytes": free,
                    "categories": build_categories(),
                })

            if parsed.path == "/api/overview/category":
                cat_id = qs.get("id", [None])[0]
                force = qs.get("refresh", ["0"])[0] == "1"
                size = compute_category_size(cat_id, force=force)
                if size is None:
                    return self._send_error_json("Unknown category", 404)
                return self._send_json({"id": cat_id, "size_bytes": size})

            if parsed.path == "/api/apps":
                force = qs.get("refresh", ["0"])[0] == "1"
                apps = get_all_apps(force=force)
                out = []
                for a in apps:
                    size_bytes = None
                    if a.get("estimated_size_kb"):
                        try:
                            size_bytes = int(a["estimated_size_kb"]) * 1024
                        except (TypeError, ValueError):
                            size_bytes = None
                    out.append({**a, "size_bytes": size_bytes})
                return self._send_json({"apps": out})

            if parsed.path == "/api/app-size":
                loc = qs.get("path", [None])[0]
                if not loc:
                    return self._send_error_json("Missing path")
                p = Path(loc)
                if not p.exists():
                    return self._send_error_json("Install location not found on disk", 404)
                return self._send_json({"size_bytes": get_size(p)})

            if parsed.path == "/api/entries":
                raw = qs.get("paths", [None])[0]
                if not raw:
                    return self._send_error_json("Missing paths")
                try:
                    paths = json.loads(raw)
                except Exception:
                    return self._send_error_json("Invalid paths")
                out = []
                for p_str in paths:
                    p = Path(p_str)
                    if not p.exists():
                        continue
                    is_dir = p.is_dir()
                    if is_dir:
                        key = str(p).lower()
                        cached = _size_cache.get(key)
                        size_bytes = cached["size"] if cached and cached.get("sig") == _dir_mtime_signature(p) else None
                    else:
                        try:
                            size_bytes = p.stat().st_size
                        except OSError:
                            size_bytes = 0
                    risk = classify_path(p)
                    out.append({"name": str(p), "path": str(p), "is_dir": is_dir, "size_bytes": size_bytes, "risk": risk})
                return self._send_json({"entries": out})

            if parsed.path == "/api/browse":
                raw_path = qs.get("path", [None])[0]
                if not raw_path:
                    return self._send_json({"path": None, "entries": [], "drives": list_drives()})
                p = Path(raw_path)
                if not p.exists() or not p.is_dir():
                    return self._send_error_json("Folder not found", 404)
                try:
                    entries = scan_directory(p)
                except PermissionError:
                    return self._send_error_json("Permission denied for this folder", 403)
                parent = str(p.parent) if p.parent != p else None
                return self._send_json({"path": str(p), "parent": parent, "entries": entries})

            if parsed.path == "/api/size":
                raw_path = qs.get("path", [None])[0]
                force = qs.get("refresh", ["0"])[0] == "1"
                if not raw_path:
                    return self._send_error_json("Missing path")
                p = Path(raw_path)
                if not p.exists():
                    return self._send_error_json("Not found", 404)
                return self._send_json({"path": str(p), "size_bytes": get_size(p, force=force)})

            # static files
            rel = parsed.path.lstrip("/") or "index.html"
            file_path = (STATIC_DIR / rel).resolve()
            if STATIC_DIR not in file_path.parents and file_path != STATIC_DIR:
                return self._send_error_json("Not found", 404)
            if not file_path.exists() or not file_path.is_file():
                return self._send_error_json("Not found", 404)
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(file_path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        except Exception as exc:
            self._send_error_json(str(exc), 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_json_body()
        try:
            if parsed.path == "/api/uninstall":
                app_id = body.get("app_id")
                if not app_id:
                    return self._send_error_json("Missing app_id")
                try:
                    message = uninstall_app_dispatch(app_id)
                except ValueError as exc:
                    return self._send_error_json(str(exc), 400)
                return self._send_json({"ok": True, "message": message})

            if parsed.path == "/api/delete":
                raw_path = body.get("path")
                confirmed = bool(body.get("confirmed"))
                if not raw_path:
                    return self._send_error_json("Missing path")
                p = Path(raw_path)
                if not p.exists():
                    return self._send_error_json("Path no longer exists", 404)
                risk = classify_path(p)
                if risk["blocked"]:
                    return self._send_error_json(f"Blocked: {risk['reason']}", 403)
                if not confirmed:
                    return self._send_error_json("Confirmation required", 400)
                send_to_recycle_bin(p)
                with _cache_lock:
                    _size_cache.pop(str(p).lower(), None)
                _save_cache()
                return self._send_json({"ok": True})

            if parsed.path == "/api/pick-folder":
                try:
                    chosen = pick_folder_dialog()
                except Exception as exc:
                    return self._send_error_json(f"Could not open the folder picker: {exc}", 500)
                return self._send_json({"path": chosen})

            if parsed.path == "/api/shutdown":
                self._send_json({"ok": True})
                threading.Thread(target=_shutdown_server, daemon=True).start()
                return

            if parsed.path == "/api/onedrive/unlink":
                raw_path = body.get("path")
                if not raw_path:
                    return self._send_error_json("Missing path")
                p = Path(raw_path)
                if not p.exists():
                    return self._send_error_json("Path no longer exists", 404)
                try:
                    target = unlink_from_onedrive(p)
                except ValueError as exc:
                    return self._send_error_json(str(exc), 400)
                return self._send_json({"ok": True, "new_path": str(target)})

            return self._send_error_json("Unknown endpoint", 404)
        except Exception as exc:
            return self._send_error_json(str(exc), 500)


_server_instance = None


def _shutdown_server():
    time.sleep(0.3)  # let the /api/shutdown response finish sending first
    if _server_instance is not None:
        _server_instance.shutdown()


def main():
    global _server_instance
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    _server_instance = server
    url = f"http://127.0.0.1:{PORT}/"
    print(f"SafeCleanup running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        # No console window when frozen (--windowed build) - if startup blows up,
        # at least leave a trace instead of vanishing silently.
        try:
            main()
        except Exception:
            import traceback
            ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
            raise
    else:
        main()

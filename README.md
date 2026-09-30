# SafeCleanup

**[⬇ Download SafeCleanup.exe](../../releases/latest/download/SafeCleanup.exe)**
— no install, just download and double-click. ([All releases](../../releases))

A local, offline Windows dashboard for finding out what's taking up space on your
PC and safely removing it — without touching anything that could break Windows.

Everything runs on your own machine: no internet connection is used or required,
no data leaves your PC. It's a small Python HTTP server (stdlib only) serving a
plain HTML/JS dashboard at `http://127.0.0.1:8765`.

## Download and run

Grab `SafeCleanup.exe` from the [Releases](../../releases) page and double-click
it. No Python installation needed — it's a self-contained executable. It opens
as its own standalone app window (using the Windows WebView2 runtime — already
built into Windows 10/11, no browser involved); click **Quit** in the app when
you're done. If WebView2 isn't available for some reason, it automatically
falls back to opening the dashboard in your default browser instead.

## What it does

**Installed Programs tab** — finds *everything* installed, not just what
Windows' own uninstaller shows:
- Classic desktop apps (from the registry's Uninstall keys) → removed with each
  app's own real uninstaller, same as Control Panel would run.
- Microsoft Store / UWP apps → removed with Windows' `Remove-AppxPackage`.
- "Files only" installs: folders under Program Files / Program Files (x86) /
  `AppData\Local\Programs` that don't belong to any app above (portable apps,
  leftover installs). These have no uninstaller, so removing one just moves
  that folder to the Recycle Bin.

**Storage Explorer tab** — drill into any drive or folder by size, with a
"Browse folder..." picker to jump anywhere. Every item gets a risk label:
Windows system files, belongs to an installed app, cache/temp data, personal
files, or unclassified.

**OneDrive tab** — browse what's inside your OneDrive folder specifically, with
options to **unlink** an item (move it out of OneDrive, stop it syncing, keep
the file) or delete it.

**Sidebar** — a clickable storage breakdown (Windows, Apps, OneDrive, Personal,
Cache) showing what's using your disk; click any segment to jump straight into
that category.

## Safety model

- Deleting something **never permanently erases it** — everything goes to the
  Recycle Bin via the Windows Shell API, so it can be restored.
- Anything classified as a **Windows system file** (inside `C:\Windows`,
  `WindowsApps`, System Volume Information, boot files, core `.NET`/PowerShell
  components, etc.) is **blocked from deletion**, enforced by the server itself
  — not just hidden in the UI.
- Anything that belongs to an installed app's folder is also blocked from
  direct deletion; the UI points you to remove that app properly instead.
- Uninstalling a classic app always launches that app's own real uninstaller.

## Running from source

Requires Python 3.10+. The server itself needs no pip packages:

```
python server.py
```

For the standalone app window (instead of opening in your browser), also install:

```
pip install pywebview
```

## Building the .exe

```
pip install pyinstaller pywebview
python -m PyInstaller --onefile --windowed --add-data "static;static" --name SafeCleanup server.py
```

The compiled exe ends up in `dist/SafeCleanup.exe`.

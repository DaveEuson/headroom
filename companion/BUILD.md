# Packaging the companion as a double-click app

The companion is a single stdlib-only Python file, so end users with Python can
run it directly. To give users who *don't* have Python a true double-click
`.exe` / `.app` (no terminal, no install), build a standalone binary with
[PyInstaller]. **This must be built on the target OS** — a Windows `.exe` builds
on Windows, a macOS `.app` on a Mac. (A CI matrix — GitHub Actions with
windows-latest + macos-latest — is the clean way to produce both.)

## One command per OS

```bash
pip install pyinstaller
pyinstaller --onefile --name ClaudeTrackerCompanion --noconsole companion.py
```

The binary lands in `dist/`:
- Windows → `dist/ClaudeTrackerCompanion.exe`
- macOS   → `dist/ClaudeTrackerCompanion` (wrap in a `.app` or ship as-is)
- Linux   → `dist/ClaudeTrackerCompanion`

`--noconsole` hides the terminal window so it runs silently in the background.

## What the user does

1. Download the binary for their OS.
2. Double-click it.

On first run it **auto-discovers the tracker** on the local network (no address
typing), pushes the first reading, and **installs itself to run at every login**.
Nothing else — it keeps the tracker fed from then on.

To stop it running automatically, run it once with `--uninstall`.

## Signing (avoids "unknown developer" warnings)

For a real product, code-sign the binaries so the OS doesn't warn users:
- **Windows:** an Authenticode certificate + `signtool`.
- **macOS:** an Apple Developer ID + `codesign` + notarization (`notarytool`).

Unsigned binaries still work; users just have to click through a warning
(Windows SmartScreen "More info → Run anyway"; macOS right-click → Open).

[PyInstaller]: https://pyinstaller.org/

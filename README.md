# Mall Audio Scheduler

An offline desktop application for unattended mall background music and scheduled announcements. It runs on Windows and macOS using Python and Qt.

## Current MVP

- Background-music playlist that loops continuously
- Import music files/folders and preview announcement audio files
- Drag rows in either playlist to set the running order; the order is saved
- Daily schedules with selectable weekdays and repeat intervals, edited either
  on the **Schedule** page or from the gear on the dashboard's voice-ad panel
- Music pauses while an announcement plays and resumes from the same point,
  advancing to the next track if the old one ran out while the ad was playing
- Spectrum visualiser with 30 styles and ten colour themes, chosen from the
  on-air button above it
- Local SQLite storage and a playback log
- CSV playback-log export
- Manual **Play now**, skip-next, pause and resume controls

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m mall_audio.app
```

Windows activation uses `.venv\\Scripts\\activate` instead.

Audio schedules are evaluated locally every second. Leave the app running for unattended operation.

The playback log is trimmed to the most recent 20,000 entries on start-up so an
unattended installation cannot grow without bound.

Scheduled runs are persisted locally. If the computer or app restarts, the same occurrence is not played twice; recurring schedules resume at their next valid interval.

## Deployment controls

The **Settings** page includes these local, persisted controls:

- Music and announcement volume
- Fade duration, and whether music fades back in or resumes instantly after an announcement
- Audio-output device selection (choose the dedicated USB interface in production)
- Start automatically when the computer user logs in (macOS and Windows)

## Licence keys

The app refuses to run until a licence key is entered, and re-checks it every
hour while running. Keys are verified offline against a public key compiled
into the app (`PUBLIC_KEY_HEX` in `mall_audio/license.py`); the matching
private key lives only on the developer's machine in `tools/keys/` and is
git-ignored. Never commit or ship that folder.

Generate keys with the developer tool (from the project root):

```bat
.venv\Scripts\python tools\make_license.py --customer "Berkarar Mall"
.venv\Scripts\python tools\make_license.py --customer "Berkarar Mall" --expires 2027-12-31
.venv\Scripts\python tools\make_license.py --customer "Berkarar Mall" --machine 1A2B-3C4D-5E6F
.venv\Scripts\python tools\make_license.py --customer "Berkarar Mall" --out "Berkarar Mall.key"
.venv\Scripts\python tools\make_license.py --verify MAS1-...
```

- No `--expires` means a lifetime key.
- `--machine` locks the key to one PC. The customer reads the machine ID from
  the activation window (or Settings → Licence) and sends it to you.
- Send the key as text or as a `.key` file; the activation window accepts either.
- The key is stored in `%APPDATA%\Mall Audio Scheduler\license.key`. Delete it
  to return the installation to the unactivated state.

Setting up a new developer machine: run `make_license.py --init`, paste the
printed public key into `mall_audio/license.py`, rebuild. Keys made with the
old private key stop working in the new build, so keep one key pair per product.

## Build a distributable

Build on the operating system you are targeting - PyInstaller does not
cross-compile, so a Windows app must be built on Windows.

### Windows installer

On any Windows PC with Python 3.11+ and [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```bat
py -m venv .venv
.venv\Scripts\pip install -e ".[build]"
.venv\Scripts\python scripts\build.py
iscc installer\windows.iss
```

The installer is written to `dist\Mall Audio Scheduler Setup.exe`. It installs
to Program Files, adds Start-menu and optional desktop shortcuts, offers to
start the app when Windows starts, and includes an uninstaller.

No Windows machine to hand: push the repository to GitHub and run the
**Windows installer** workflow (Actions tab). It builds on a Windows runner
and attaches the same `Setup.exe` as a downloadable artifact.

### macOS

```bash
.venv/bin/pip install -e '.[build]'
.venv/bin/python scripts/build.py
```

The app is in `dist/Mall Audio Scheduler/`. Code signing and notarisation
should be done on the client machine or in deployment CI.

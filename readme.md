# Autorecovery

Daily incremental backup of the user's Desktop to a fixed folder, packaged as a single portable `Autorecovery.exe` (no Python needed on the target PC).

## Configuration

All settings are constants at the top of [autorecovery.py](autorecovery.py):

| Constant | Default | Meaning |
|---|---|---|
| `SOURCE_DIR` | user's Desktop (OneDrive-aware) | Folder to back up |
| `DESTINATION_DIR` | `D:\PcBackup\LindaPc` | Where the copy goes (local, USB or `\\server\share`) |
| `BACKUP_TIME` | `12:00` | Fixed daily backup time |
| `CHECK_INTERVAL_HOURS` | `6` | How often the task re-checks for a missed backup |
| `EXCLUDE_PATTERNS` | `desktop.ini`, `~$*`, `*.tmp`, ... | Names that are never copied |
| `LOW_PRIORITY` | `True` | Run with low CPU/disk priority |

After changing a constant, rebuild the exe with `build.bat`. To change the folders without rebuilding, use the `changebackupdir` / `changelocationdir` commands.

## Build

```powershell
.\build.bat
```

Output: `dist\Autorecovery.exe`.

## Install on a PC

1. Copy `Autorecovery.exe` to a permanent location, e.g. `C:\Tools\Autorecovery\`.
2. Register the scheduled task (current user, no admin rights needed):

```powershell
C:\Tools\Autorecovery\Autorecovery.exe install
```

If you move the exe later, run `install` again.

| Command | Action |
|---|---|
| `Autorecovery.exe` | Normal run, used by the task |
| `run` | Back up now, ahead of the schedule |
| `changebackupdir <path>` | Change where backups are stored |
| `changelocationdir <path>` | Change which folder is backed up |
| `status` | Show paths, last backup, and whether one is due |
| `install` / `uninstall` | Add / remove the scheduled task |

See [COMMANDS.md](COMMANDS.md) for the full command guide.

## How it works

- **Not resident:** the Task Scheduler starts the exe at `BACKUP_TIME`, then every `CHECK_INTERVAL_HOURS`, at logon (+15 min), and as soon as possible after a missed run. If the backup for the latest slot is already done, the exe exits within milliseconds.
- **Incremental:** a file is copied only if it is new, or its size or last-modified time differ from the existing copy. Each copy goes to a temp file first and then replaces the old one, so a good copy is never half-overwritten.
- **Tray icon:** a blue sync icon is visible while copying. Right-click it for progress, to open the log or backup folder, or to cancel.
- **Failures:** if the destination is unreachable (for example, the USB drive or network is offline), the run is not marked done and the next check retries. Locked files are logged and skipped.
- **No deletions:** files deleted from the Desktop stay in the backup, so you can recover them.
- **Log and state:** `%LOCALAPPDATA%\Autorecovery\autorecovery.log` and `state.json`.

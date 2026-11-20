# Autorecovery – Command Guide

You can run `Autorecovery.exe` from PowerShell or Command Prompt, or from a desktop shortcut with the command added after the exe path. The exe has no console window, so it shows its results in a small pop-up window.

In the examples, the exe is in `C:\Tools\Autorecovery\`. Use your own path.

---

## Run a backup now

Starts a backup right away, without waiting for the schedule. The tray icon is visible while it runs.

```powershell
C:\Tools\Autorecovery\Autorecovery.exe run
```

- As usual, only new or changed files are copied.
- This counts as the backup for the current schedule slot. The next scheduled check sees the backup as done and skips it.
- If a backup is already running, a message says so and nothing new starts.

---

## Change the backup folder (where copies are stored)

```powershell
C:\Tools\Autorecovery\Autorecovery.exe changebackupdir "E:\Backups\Linda"
```

- Network shares work too: `changebackupdir "\\server\backups\Linda"`.
- The folder doesn't need to exist yet. It's created on the next backup.
- The backup folder can't be inside the folder that's being backed up.
- Files already in the old backup folder are not moved. The next backup copies everything into the new folder.

Go back to the folder set in the code (`DESTINATION_DIR`):

```powershell
C:\Tools\Autorecovery\Autorecovery.exe changebackupdir default
```

---

## Change the folder that is backed up

```powershell
C:\Tools\Autorecovery\Autorecovery.exe changelocationdir "C:\Users\Linda\Documents"
```

- The folder must exist, otherwise nothing is changed.
- It can't contain the backup folder.

Go back to the folder set in the code (`SOURCE_DIR`, the user's Desktop):

```powershell
C:\Tools\Autorecovery\Autorecovery.exe changelocationdir default
```

---

## Other commands

| Command | What it does |
|---|---|
| *(no command)* | Scheduled run: backs up only if the backup is due, otherwise exits immediately |
| `status` | Shows both folders, the schedule, the last backup and whether a backup is due |
| `install` | Registers the daily scheduled task for the current user |
| `uninstall` | Removes the scheduled task |
| `help` | Lists all commands |

---

## Notes

- **Paths with spaces:** put them in quotes, e.g. `"C:\My Files"`.
- **Leading dashes are optional:** `run`, `--run` and `/run` all work.
- **Where changed paths are saved:** `changebackupdir` and `changelocationdir` save to `%LOCALAPPDATA%\Autorecovery\config.json`. The settings apply to the current Windows user only and stay in place when you rebuild or replace the exe. Deleting this file resets both folders to the defaults.
- **No reinstall needed:** the scheduled task reads the saved paths on every run.
- **Log file:** `%LOCALAPPDATA%\Autorecovery\autorecovery.log`.

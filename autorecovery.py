"""
Autorecovery - daily incremental backup of the user's Desktop.

How it works
------------
* Windows Task Scheduler starts this program once a day at BACKUP_TIME and then
  re-checks every CHECK_INTERVAL_HOURS (plus at logon / when the PC wakes up after
  a missed run). The program does NOT stay resident in memory.
* On every start it checks whether the backup for the most recent scheduled slot
  has already been done. If yes, it exits immediately (takes milliseconds).
* If a backup is due, a tray icon appears while files are copied. A file is only
  copied when it is new, or when its size or last-modified time differ from the
  existing copy. Unchanged files are skipped.

Command line
------------
    Autorecovery.exe                           normal run (used by the scheduled task)
    Autorecovery.exe run                       force a backup now, ignoring the schedule
    Autorecovery.exe changebackupdir <path>    change where backups are stored
    Autorecovery.exe changelocationdir <path>  change which folder is backed up
    Autorecovery.exe install                   register the scheduled task for the current user
    Autorecovery.exe uninstall                 remove the scheduled task
    Autorecovery.exe status                    show paths, schedule and last run
    Autorecovery.exe help                      list the commands
Commands also work with a leading "--" (e.g. --install). Paths changed with the
change* commands are saved in %LOCALAPPDATA%\\Autorecovery\\config.json and
override the constants below; use "default" as the path to go back to them.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime, timedelta
from fnmatch import fnmatch
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _known_desktop_folder() -> Path:
    """Real Desktop path (handles OneDrive / GPO folder redirection)."""
    try:
        guid_bytes = uuid.UUID("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}").bytes_le
        guid = (ctypes.c_byte * 16).from_buffer_copy(guid_bytes)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return Path(path)
    except Exception:
        pass
    return Path.home() / "Desktop"


# =============================================================================
#  CONFIGURATION  - edit these, then rebuild the exe (build.bat)
# =============================================================================

# Folder to back up. Default: the current user's Desktop (OneDrive-aware).
# To use a fixed folder instead:  SOURCE_DIR = Path(r"C:\Users\someone\Desktop")
SOURCE_DIR = _known_desktop_folder()

# Where the backup copy is stored (local folder, external drive or UNC share).
DESTINATION_DIR = Path(r"D:\PcBackup\LindaPc")

# Daily fixed time of the backup (24h, local time) and how often the scheduled
# task re-checks afterwards in case the PC was off / asleep at that time.
BACKUP_TIME = "12:00"
CHECK_INTERVAL_HOURS = 6

# Files / folders that are never copied (shell-style wildcards, matched on name).
EXCLUDE_PATTERNS = ["desktop.ini", "Thumbs.db", "~$*", "*.tmp", "*.crdownload", "*.partial"]

# Allowed difference in modification time (seconds). 2s covers FAT/exFAT drives.
MTIME_TOLERANCE_SECONDS = 2

# Run the backup with low CPU and disk priority so the PC stays responsive.
LOW_PRIORITY = True

# Show a balloon message in the tray when the backup finishes.
SHOW_COMPLETION_NOTICE = True

# =============================================================================

APP_NAME = "Autorecovery"
TASK_NAME = "Autorecovery Desktop Backup"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
STATE_FILE = DATA_DIR / "state.json"
CONFIG_FILE = DATA_DIR / "config.json"  # holds paths changed with changebackupdir / changelocationdir
_DEFAULT_SOURCE_DIR = SOURCE_DIR
_DEFAULT_DESTINATION_DIR = DESTINATION_DIR
LOG_FILE = DATA_DIR / "autorecovery.log"
PARTIAL_SUFFIX = ".arpart"

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- utils

def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    if sys.stdout is not None:
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


def message(text: str, error: bool = False) -> None:
    """Print to console when available, otherwise show a message box (windowed exe)."""
    if sys.stdout is not None:
        print(text)
    else:
        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)


def acquire_single_instance():
    """Return a mutex handle, or None if another instance is already running."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\AutorecoveryBackupMutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def set_low_priority() -> None:
    PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000  # low CPU + low disk I/O priority
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
    kernel32 = ctypes.windll.kernel32
    proc = kernel32.GetCurrentProcess()
    if not kernel32.SetPriorityClass(proc, PROCESS_MODE_BACKGROUND_BEGIN):
        kernel32.SetPriorityClass(proc, BELOW_NORMAL_PRIORITY_CLASS)


# ------------------------------------------------------------------- schedule state

def parse_backup_time() -> tuple[int, int]:
    hour, minute = BACKUP_TIME.split(":")
    return int(hour), int(minute)


def last_scheduled_slot(now: datetime) -> datetime:
    """Most recent moment the backup was supposed to run (today or yesterday at BACKUP_TIME)."""
    hour, minute = parse_backup_time()
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if now >= slot else slot - timedelta(days=1)


def load_last_success() -> datetime | None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_success"])
    except Exception:
        return None


def save_last_success(when: datetime, stats: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_success": when.isoformat(timespec="seconds"), "stats": stats}, indent=2),
                          encoding="utf-8")


def backup_is_due(now: datetime) -> bool:
    last = load_last_success()
    return last is None or last < last_scheduled_slot(now)


# -------------------------------------------------------------------------- backup

class BackupJob:
    def __init__(self, source: Path, destination: Path):
        self.source = source
        self.destination = destination
        self.cancel = threading.Event()
        self.stats = {"copied": 0, "skipped": 0, "failed": 0, "bytes_copied": 0}
        self.current = ""
        self.ok = False

    def _excluded(self, name: str) -> bool:
        return any(fnmatch(name.lower(), p.lower()) for p in EXCLUDE_PATTERNS)

    @staticmethod
    def _needs_copy(src_stat: os.stat_result, dst: Path) -> bool:
        try:
            dst_stat = dst.stat()
        except FileNotFoundError:
            return True
        if dst_stat.st_size != src_stat.st_size:
            return True
        return abs(dst_stat.st_mtime - src_stat.st_mtime) > MTIME_TOLERANCE_SECONDS

    def _copy(self, src: Path, dst: Path) -> None:
        """Copy to a temp file first, then swap in, so an old good copy is never half-overwritten."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + PARTIAL_SUFFIX)
        try:
            shutil.copy2(src, tmp)  # copy2 keeps the modification time
            if dst.exists():
                os.chmod(dst, 0o666)  # clear read-only so it can be replaced
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def run(self) -> None:
        if not self.source.is_dir():
            log.error("Source folder not found: %s", self.source)
            return
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Destination not reachable: %s (%s)", self.destination, exc)
            return

        dest_resolved = self.destination.resolve()
        log.info("Backup started: %s -> %s", self.source, self.destination)
        started = time.monotonic()

        for root, dirs, files in os.walk(self.source, onerror=lambda e: log.warning("Cannot read %s", e)):
            root_path = Path(root)
            # prune excluded folders, symlinked/junction folders and the destination itself
            dirs[:] = [
                d for d in dirs
                if not self._excluded(d)
                and not (root_path / d).is_symlink()
                and (root_path / d).resolve() != dest_resolved
            ]
            rel_root = root_path.relative_to(self.source)

            for name in files:
                if self.cancel.is_set():
                    log.warning("Backup cancelled by user")
                    return
                if self._excluded(name) or name.endswith(PARTIAL_SUFFIX):
                    continue
                src = root_path / name
                dst = self.destination / rel_root / name
                self.current = str(rel_root / name)
                try:
                    st = src.stat()
                    if self._needs_copy(st, dst):
                        self._copy(src, dst)
                        self.stats["copied"] += 1
                        self.stats["bytes_copied"] += st.st_size
                        log.info("Copied   %s", rel_root / name)
                    else:
                        self.stats["skipped"] += 1
                except Exception as exc:  # locked file, permission denied, ...
                    self.stats["failed"] += 1
                    log.warning("FAILED   %s (%s)", rel_root / name, exc)

        self.ok = True
        log.info("Backup finished in %.1fs: %d copied (%.1f MB), %d unchanged, %d failed",
                 time.monotonic() - started, self.stats["copied"], self.stats["bytes_copied"] / 1e6,
                 self.stats["skipped"], self.stats["failed"])


# ---------------------------------------------------------------------- tray icon

def make_icon_image(size: int = 64):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s), radius=12 * s, fill=(0, 120, 212, 255))
    # circular "sync" arrow
    d.arc((14 * s, 14 * s, 50 * s, 50 * s), start=40, end=320, fill="white", width=max(2, round(6 * s)))
    d.polygon([(44 * s, 8 * s), (56 * s, 26 * s), (36 * s, 26 * s)], fill="white")
    return img


def run_with_tray(job: BackupJob) -> None:
    import pystray

    def status_text(_item=None) -> str:
        return f"Copied {job.stats['copied']}, unchanged {job.stats['skipped']}"

    def open_folder(path: Path):
        return lambda icon, item: os.startfile(path)

    menu = pystray.Menu(
        pystray.MenuItem("Autorecovery - backup running", None, enabled=False),
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open backup folder", open_folder(DESTINATION_DIR)),
        pystray.MenuItem("Open log", lambda icon, item: os.startfile(LOG_FILE)),
        pystray.MenuItem("Cancel backup", lambda icon, item: job.cancel.set()),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), "Autorecovery: backup running...", menu)

    def worker(icon_):
        icon_.visible = True
        t = threading.Thread(target=job.run, daemon=True)
        t.start()
        while t.is_alive():
            t.join(1.0)
            tip = f"Autorecovery: backing up... ({job.stats['copied']} copied, {job.stats['skipped']} unchanged)"
            icon_.title = tip[:127]
        if SHOW_COMPLETION_NOTICE:
            if job.ok:
                text = (f"{job.stats['copied']} file(s) copied, {job.stats['skipped']} unchanged"
                        + (f", {job.stats['failed']} failed (see log)" if job.stats["failed"] else ""))
                icon_.notify(text, "Backup finished")
            elif not job.cancel.is_set():
                icon_.notify("Backup could not run - see log for details.", "Backup failed")
            time.sleep(5)
        icon_.stop()

    icon.run(setup=worker)


# ------------------------------------------------------------- task scheduler

def _task_command() -> tuple[str, str, str]:
    """(command, arguments, working dir) for the scheduled task."""
    if getattr(sys, "frozen", False):  # PyInstaller exe
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else sys.executable), f'"{script}"', str(script.parent)


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def install_task() -> None:
    hour, minute = parse_backup_time()
    start = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    command, arguments, workdir = _task_command()

    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Daily backup of {_xml_escape(str(SOURCE_DIR))} to {_xml_escape(str(DESTINATION_DIR))}. Runs at {BACKUP_TIME}, re-checks every {CHECK_INTERVAL_HOURS}h and exits immediately when the backup is already done.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.isoformat(timespec="seconds")}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition>
        <Interval>PT{CHECK_INTERVAL_HOURS}H</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_xml_escape(user)}</UserId>
      <Delay>PT15M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml_escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(command)}</Command>
      <Arguments>{_xml_escape(arguments)}</Arguments>
      <WorkingDirectory>{_xml_escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(xml)
        xml_path = f.name
    try:
        result = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        os.unlink(xml_path)

    if result.returncode == 0:
        message(f"Scheduled task '{TASK_NAME}' installed.\n\n"
                f"Backup: {SOURCE_DIR}\n    ->  {DESTINATION_DIR}\n"
                f"Daily at {BACKUP_TIME}, re-check every {CHECK_INTERVAL_HOURS}h and at logon.\n"
                f"Program: {command}")
    else:
        message(f"Could not install the scheduled task:\n{result.stderr or result.stdout}", error=True)


def uninstall_task() -> None:
    result = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode == 0:
        message(f"Scheduled task '{TASK_NAME}' removed.")
    else:
        message(f"Could not remove the scheduled task:\n{result.stderr or result.stdout}", error=True)


def show_status() -> None:
    last = load_last_success()
    now = datetime.now()
    overrides = load_config()
    message(f"Source:       {SOURCE_DIR}{'  (changed with changelocationdir)' if 'source_dir' in overrides else ''}\n"
            f"Destination:  {DESTINATION_DIR}{'  (changed with changebackupdir)' if 'destination_dir' in overrides else ''}\n"
            f"Schedule:     daily at {BACKUP_TIME}, re-check every {CHECK_INTERVAL_HOURS}h\n"
            f"Last backup:  {last.strftime('%Y-%m-%d %H:%M') if last else 'never'}\n"
            f"Backup due:   {'yes' if backup_is_due(now) else 'no'}\n"
            f"Log file:     {LOG_FILE}")


# ------------------------------------------------------- runtime path overrides

def load_config() -> dict:
    """Paths changed at runtime (changebackupdir / changelocationdir). Missing file = use the constants."""
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config_value(key: str, value: str | None) -> None:
    config = load_config()
    if value is None:
        config.pop(key, None)
    else:
        config[key] = value
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def apply_config() -> None:
    global SOURCE_DIR, DESTINATION_DIR
    config = load_config()
    if config.get("source_dir"):
        SOURCE_DIR = Path(config["source_dir"])
    if config.get("destination_dir"):
        DESTINATION_DIR = Path(config["destination_dir"])


def _path_from_args(rest: list[str]) -> Path | None:
    """Join the remaining args (handles unquoted paths with spaces) and clean stray quotes."""
    raw = " ".join(rest).strip().strip('"').strip("'").strip()
    if not raw:
        return None
    return Path(os.path.expandvars(os.path.expanduser(raw))).absolute()


def change_backup_dir(rest: list[str]) -> int:
    if rest and rest[0].lower() == "default":
        save_config_value("destination_dir", None)
        message(f"Backup folder reset to the built-in default:\n{_DEFAULT_DESTINATION_DIR}")
        return 0
    path = _path_from_args(rest)
    if path is None:
        message("Usage: Autorecovery.exe changebackupdir <path>", error=True)
        return 2
    if path == SOURCE_DIR.absolute() or SOURCE_DIR.absolute() in path.parents:
        message(f"The backup folder cannot be inside the folder being backed up:\n{SOURCE_DIR}", error=True)
        return 2
    save_config_value("destination_dir", str(path))
    note = "" if path.exists() else "\n\n(The folder does not exist yet - it will be created on the next backup.)"
    message(f"Backup folder changed to:\n{path}{note}")
    return 0


def change_location_dir(rest: list[str]) -> int:
    if rest and rest[0].lower() == "default":
        save_config_value("source_dir", None)
        message(f"Folder to back up reset to the built-in default:\n{_DEFAULT_SOURCE_DIR}")
        return 0
    path = _path_from_args(rest)
    if path is None:
        message("Usage: Autorecovery.exe changelocationdir <path>", error=True)
        return 2
    if not path.is_dir():
        message(f"Folder not found - nothing changed:\n{path}", error=True)
        return 2
    if path == DESTINATION_DIR.absolute() or path in DESTINATION_DIR.absolute().parents:
        message(f"The folder to back up cannot contain the backup folder:\n{DESTINATION_DIR}", error=True)
        return 2
    save_config_value("source_dir", str(path))
    message(f"Folder to back up changed to:\n{path}")
    return 0


USAGE = """Autorecovery commands:

  Autorecovery.exe                           scheduled run (backs up only if due)
  Autorecovery.exe run                       back up now, ahead of the schedule
  Autorecovery.exe changebackupdir <path>    set where backups are stored
  Autorecovery.exe changelocationdir <path>  set which folder is backed up
  Autorecovery.exe changebackupdir default   go back to the built-in path
  Autorecovery.exe changelocationdir default go back to the built-in path
  Autorecovery.exe status                    show paths and last backup
  Autorecovery.exe install / uninstall       add / remove the scheduled task
  Autorecovery.exe help                      show this text"""


# ---------------------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    apply_config()
    command = argv[0].lower().lstrip("-/") if argv else ""
    rest = argv[1:]

    if command in ("help", "h", "?"):
        message(USAGE)
        return 0
    if command == "install":
        install_task()
        return 0
    if command == "uninstall":
        uninstall_task()
        return 0
    if command == "status":
        show_status()
        return 0
    if command == "changebackupdir":
        return change_backup_dir(rest)
    if command == "changelocationdir":
        return change_location_dir(rest)
    if command not in ("", "run", "now"):
        message(f"Unknown command: {argv[0]}\n\n{USAGE}", error=True)
        return 2

    force = command in ("run", "now")
    now = datetime.now()
    if not force and not backup_is_due(now):
        return 0  # already done for this slot - exit right away

    mutex = acquire_single_instance()
    if mutex is None:
        if force:
            message("A backup is already running (see the tray icon).")
        return 0  # another backup is already running

    setup_logging()
    if LOW_PRIORITY:
        set_low_priority()

    job = BackupJob(SOURCE_DIR, DESTINATION_DIR)
    try:
        run_with_tray(job)
    except Exception:
        log.exception("Tray icon unavailable, running without it")
        if not job.ok:
            job.run()

    if job.ok:
        save_last_success(now, job.stats)
        return 0
    return 1  # not marked as done -> the next periodic check retries


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

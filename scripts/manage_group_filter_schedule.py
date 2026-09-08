#!/usr/bin/env python3
"""Install, remove, or inspect the LoopSmith group-filter crontab entry."""

import argparse
import datetime as dt
import os
import subprocess
import tempfile
from pathlib import Path


os.umask(0o077)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "group-monitor"
BEGIN = "# loopsmith:group-share-filter BEGIN"
END = "# loopsmith:group-share-filter END"
SPEC = ROOT / "group-share-filter.loop.json"
LOG = DATA_DIR / "cron.log"
WORKFLOW = ROOT / "scripts" / "group_filter_loop.py"
CRON_LINE = "17,47 * * * * /usr/bin/python3 {} >> {} 2>&1".format(WORKFLOW, LOG)


def read_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("could not read crontab")
    return result.stdout


def without_managed_block(text):
    output = []
    inside = False
    for line in text.splitlines():
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if not inside:
            output.append(line)
    return "\n".join(output).rstrip()


def write_crontab(text):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA_DIR / ("crontab-backup-" + stamp + ".txt")
    backup.write_text(read_crontab(), encoding="utf-8")
    backup.chmod(0o600)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(text.rstrip() + "\n")
        temp_name = handle.name
    try:
        result = subprocess.run(["crontab", temp_name], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError("could not install crontab: " + result.stderr.strip())
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return backup


def install():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG.touch(mode=0o600, exist_ok=True)
    LOG.chmod(0o600)
    existing = without_managed_block(read_crontab())
    managed = "\n".join([BEGIN, CRON_LINE, END])
    combined = (existing + "\n" + managed).strip() + "\n"
    backup = write_crontab(combined)
    print("installed twice-hourly group filter; backup: {}".format(backup))


def uninstall():
    existing = read_crontab()
    cleaned = without_managed_block(existing)
    backup = write_crontab(cleaned + "\n")
    print("removed group filter schedule; backup: {}".format(backup))


def status():
    text = read_crontab()
    active = BEGIN in text and CRON_LINE in text and END in text
    print("active" if active else "inactive")
    if active:
        print(CRON_LINE)
    return 0 if active else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    args = parser.parse_args()
    if args.action == "install":
        install()
        return 0
    if args.action == "uninstall":
        uninstall()
        return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
X Bookmark Manager — Native Dashboard

Opens a native macOS window with a full-featured dashboard for managing
bookmarks and DM syncs. Uses Flask for the API backend and pywebview
for the native window.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Add project paths
SERVICE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SERVICE_DIR / "data"
APP_DIR = SERVICE_DIR / "app"
SCRIPTS_DIR = SERVICE_DIR / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

from flask import Flask, jsonify, request, send_from_directory
import service
from json_filelock import locked_json_read

app = Flask(__name__, static_folder=str(APP_DIR))

# Track running sync tasks
_sync_lock = threading.Lock()
_sync_status = {"bookmarks": "idle", "dms": "idle"}
_sync_log_buffer = []


def add_log(msg):
    with _sync_lock:
        _sync_log_buffer.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message": msg,
        })
        # Keep last 200 entries
        if len(_sync_log_buffer) > 200:
            del _sync_log_buffer[:-200]


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(APP_DIR), "dashboard.html")


@app.route("/tweets.json")
def tweets_json():
    """Serve tweets.json for the client-side tweet browser."""
    return send_from_directory(str(APP_DIR), "tweets.json")


@app.route("/api/status")
def api_status():
    """Get full system status."""
    accounts = service.load_accounts()
    dm_config = service.load_dm_config()
    master = service.master_data_file()

    total_tweets = 0
    categories = {}
    sources = {}
    tweets = locked_json_read(master, default=[])
    if tweets:
        total_tweets = len(tweets)
        for t in tweets:
            cat = t.get("category", "Other")
            categories[cat] = categories.get(cat, 0) + 1
            for src in t.get("sources", []):
                sources[src] = sources.get(src, 0) + 1

    # Read scheduler state
    scheduler_state_file = DATA_DIR / "sync_scheduler_state.json"
    scheduler_state = locked_json_read(scheduler_state_file, default={})
    scheduler_runs = scheduler_state.get("runs", {})

    # Check if scheduler daemon is running
    scheduler_active = False
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.mshrmnsr.bookmark-sync"],
            capture_output=True, text=True
        )
        scheduler_active = result.returncode == 0
    except Exception:
        pass

    # Read last few lines from sync log
    log_file = DATA_DIR / "sync_stdout.log"
    last_log_lines = []
    if log_file.exists():
        with open(log_file) as f:
            lines = f.readlines()
        last_log_lines = [l.rstrip() for l in lines[-50:]]

    return jsonify({
        "accounts": {name: {"added": info.get("added", "?")} for name, info in accounts.items()},
        "total_tweets": total_tweets,
        "categories": categories,
        "sources": sources,
        "sync_status": _sync_status,
        "scheduler_active": scheduler_active,
        "scheduler_runs_today": scheduler_runs.get(time.strftime("%Y-%m-%d"), 0),
        "dm_config": {
            "conversations": dm_config.get("conversations", []),
            "last_synced": dm_config.get("last_synced"),
            "last_message_ids": dm_config.get("last_message_ids", {}),
        },
        "last_log_lines": last_log_lines,
    })


@app.route("/api/sync/bookmarks", methods=["POST"])
def api_sync_bookmarks():
    """Trigger a manual bookmark sync."""
    if _sync_status["bookmarks"] != "idle":
        return jsonify({"error": "Bookmark sync already running"}), 409

    def run_sync():
        _sync_status["bookmarks"] = "running"
        add_log("Manual bookmark sync started...")
        try:
            service.cmd_sync(fetch_all=False)
            add_log("Bookmark sync completed.")
        except Exception as e:
            add_log(f"Bookmark sync error: {e}")
        finally:
            _sync_status["bookmarks"] = "idle"

    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/sync/dms", methods=["POST"])
def api_sync_dms():
    """Trigger a manual DM sync."""
    if _sync_status["dms"] != "idle":
        return jsonify({"error": "DM sync already running"}), 409

    def run_dm_sync():
        _sync_status["dms"] = "running"
        add_log("Manual DM sync started...")
        try:
            dm_tweets = service.cmd_sync_dms()
            if dm_tweets:
                service.categorize_tweets_batch(dm_tweets)
                service.merge_into_master(dm_tweets)
                service.rebuild_browser_app()
                add_log(f"DM sync completed: {len(dm_tweets)} new tweets")
            else:
                add_log("DM sync completed: no new tweets")
        except Exception as e:
            add_log(f"DM sync error: {e}")
        finally:
            _sync_status["dms"] = "idle"

    threading.Thread(target=run_dm_sync, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/sync/all", methods=["POST"])
def api_sync_all():
    """Trigger both bookmark + DM sync."""
    if _sync_status["bookmarks"] != "idle" or _sync_status["dms"] != "idle":
        return jsonify({"error": "A sync is already running"}), 409

    def run_all():
        _sync_status["bookmarks"] = "running"
        _sync_status["dms"] = "running"
        add_log("Full sync started (bookmarks + DMs)...")
        try:
            service.cmd_sync(fetch_all=False)
            add_log("Full sync completed.")
        except Exception as e:
            add_log(f"Full sync error: {e}")
        finally:
            _sync_status["bookmarks"] = "idle"
            _sync_status["dms"] = "idle"

    threading.Thread(target=run_all, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/logs")
def api_logs():
    """Get sync log lines."""
    log_file = DATA_DIR / "sync_stdout.log"
    lines = []
    if log_file.exists():
        with open(log_file) as f:
            all_lines = f.readlines()
        n = int(request.args.get("n", 100))
        lines = [l.rstrip() for l in all_lines[-n:]]
    with _sync_lock:
        dash_log = list(_sync_log_buffer[-50:])
    return jsonify({"lines": lines, "dashboard_log": dash_log})


@app.route("/api/accounts")
def api_accounts():
    """List accounts."""
    return jsonify(service.load_accounts())


@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    """Add account."""
    data = request.json
    username = data.get("username", "").strip()
    auth_token = data.get("auth_token", "").strip()
    ct0 = data.get("ct0", "").strip()
    if not all([username, auth_token, ct0]):
        return jsonify({"error": "Missing fields"}), 400
    accounts = service.load_accounts()
    accounts[username] = {
        "auth_token": auth_token,
        "ct0": ct0,
        "added": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    service.save_accounts(accounts)
    add_log(f"Account @{username} added")
    return jsonify({"status": "ok"})


@app.route("/api/accounts/<username>", methods=["DELETE"])
def api_remove_account(username):
    """Remove account."""
    accounts = service.load_accounts()
    if username in accounts:
        del accounts[username]
        service.save_accounts(accounts)
        add_log(f"Account @{username} removed")
    return jsonify({"status": "ok"})


@app.route("/api/accounts/<username>", methods=["PUT"])
def api_update_account(username):
    """Update account credentials."""
    data = request.json
    accounts = service.load_accounts()
    if username not in accounts:
        return jsonify({"error": "Account not found"}), 404
    if data.get("auth_token"):
        accounts[username]["auth_token"] = data["auth_token"]
    if data.get("ct0"):
        accounts[username]["ct0"] = data["ct0"]
    service.save_accounts(accounts)
    add_log(f"Account @{username} credentials updated")
    return jsonify({"status": "ok"})


@app.route("/api/dm-config")
def api_dm_config():
    """Get DM configuration."""
    return jsonify(service.load_dm_config())


@app.route("/api/dm-config", methods=["POST"])
def api_update_dm_config():
    """Update DM configuration."""
    data = request.json
    config = service.load_dm_config()
    if "conversations" in data:
        config["conversations"] = data["conversations"]
    service.save_dm_config(config)
    add_log("DM configuration updated")
    return jsonify({"status": "ok"})


@app.route("/api/tweets")
def api_tweets():
    """Get tweets with optional filtering."""
    master = service.master_data_file()
    tweets = locked_json_read(master, default=[])
    if not tweets:
        return jsonify({"tweets": [], "total": 0})

    # Filters
    category = request.args.get("category")
    source = request.args.get("source")
    search = request.args.get("q", "").lower()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))

    if category:
        tweets = [t for t in tweets if t.get("category") == category]
    if source:
        tweets = [t for t in tweets if source in " ".join(t.get("sources", []))]
    if search:
        tweets = [t for t in tweets if search in (t.get("text", "") or "").lower()
                  or search in (t.get("summary", "") or "").lower()]

    total = len(tweets)
    # Sort by likes desc
    tweets.sort(key=lambda x: x.get("likeCount", 0), reverse=True)
    # Paginate
    start = (page - 1) * per_page
    tweets = tweets[start:start + per_page]

    # Compact for transfer
    compact = []
    for t in tweets:
        author = t.get("author", {})
        username = ""
        if isinstance(author, dict):
            username = author.get("username", author.get("userName", ""))
        compact.append({
            "id": t.get("id"),
            "text": (t.get("text", "") or "")[:300],
            "author": username,
            "date": t.get("createdAt", ""),
            "likes": t.get("likeCount", 0),
            "retweets": t.get("retweetCount", 0),
            "views": t.get("viewCount", 0),
            "category": t.get("category", ""),
            "subcategory": t.get("subcategory", ""),
            "summary": t.get("summary", ""),
            "sources": t.get("sources", []),
        })

    return jsonify({"tweets": compact, "total": total, "page": page, "per_page": per_page})


@app.route("/api/scheduler", methods=["POST"])
def api_scheduler():
    """Start/stop the scheduler daemon."""
    action = request.json.get("action")
    plist = Path.home() / "Library/LaunchAgents/com.mshrmnsr.bookmark-sync.plist"

    if action == "start":
        if plist.exists():
            subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
            add_log("Scheduler started")
        else:
            service.cmd_setup_auto()
            add_log("Scheduler installed and started")
        return jsonify({"status": "started"})

    elif action == "stop":
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            add_log("Scheduler stopped")
        return jsonify({"status": "stopped"})

    return jsonify({"error": "Invalid action"}), 400


# ─── Main ────────────────────────────────────────────────────────────────────

def run_native_window():
    """Launch the dashboard in a native macOS window using pywebview."""
    import webview

    # Start Flask in a background thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=8743, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    time.sleep(0.5)

    # Create native window
    window = webview.create_window(
        "X Bookmark Manager",
        "http://127.0.0.1:8743",
        width=1200,
        height=800,
        min_size=(900, 600),
    )
    webview.start()


def run_browser_mode():
    """Run in browser mode (fallback if pywebview not available)."""
    import webbrowser
    print("Starting dashboard at http://localhost:8743")
    print("Opening in browser...")
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:8743")).start()
    app.run(host="127.0.0.1", port=8743, debug=False)


if __name__ == "__main__":
    try:
        import webview
        run_native_window()
    except ImportError:
        print("pywebview not installed. Falling back to browser mode.")
        print("Install for native window: pip install pywebview")
        run_browser_mode()

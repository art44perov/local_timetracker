
#!/usr/bin/env python3
"""
WorkTracker — отслеживание рабочего времени с веб-интерфейсом
Требования: pip install flask psutil pywin32 (Windows)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# ── Попытка импорта win32 (только Windows) ─────────────────────────────────
try:
    import win32gui
    import win32process
    import psutil
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False
    print("[WARN] pywin32/psutil не установлен — трекинг активного окна недоступен.")
    print("       Установите: pip install pywin32 psutil")

# ── Конфиг ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "worktracker.db"
POLL_SEC = 5          # интервал опроса активного окна (секунды)
IDLE_SEC = 300        # 5 мин без активности → пауза сессии

app = Flask(__name__, static_folder=str(BASE_DIR))

# ── Браузеры ────────────────────────────────────────────────────────────────
BROWSER_EXES = {"chrome.exe", "firefox.exe", "msedge.exe", "opera.exe",
                "brave.exe", "vivaldi.exe", "chromium.exe"}

# ── RDP-паттерны ────────────────────────────────────────────────────────────
RDP_PATTERNS = [
    r"mstsc",
    r"remote desktop",
    r"удалённый рабочий стол",
    r"удаленный рабочий стол",
]

# ── Паттерны проводника / файловых менеджеров ───────────────────────────────
EXPLORER_PATTERNS = [
    r"explorer\.exe",
    r"проводник",
    r"windows explorer",
    r"total commander",
    r"far manager",
    r"files -",
]

# ────────────────────────────────────────────────────────────────────────────
#  БД
# ────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT UNIQUE NOT NULL,
            color    TEXT DEFAULT '#4f8ef7',
            rdp_host TEXT DEFAULT NULL,
            created  TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER REFERENCES clients(id),
            app_name    TEXT NOT NULL,
            window_title TEXT,
            category    TEXT NOT NULL,   -- browser | rdp | explorer | app
            start_time  TEXT NOT NULL,
            end_time    TEXT,
            duration_s  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id  INTEGER REFERENCES clients(id),
            text       TEXT NOT NULL,
            created    TEXT DEFAULT (datetime('now','localtime'))
        );

        -- Начальные клиенты
        INSERT OR IGNORE INTO clients (name, color) VALUES ('Без клиента', '#6b7280');
        INSERT OR IGNORE INTO clients (name, color) VALUES ('Личное',      '#10b981');
        """)
        # Миграция: добавить колонку rdp_host если её нет (для существующих БД)
        cols = [r[1] for r in db.execute("PRAGMA table_info(clients)").fetchall()]
        if "rdp_host" not in cols:
            db.execute("ALTER TABLE clients ADD COLUMN rdp_host TEXT DEFAULT NULL")


# ────────────────────────────────────────────────────────────────────────────
#  Определение активного окна
# ────────────────────────────────────────────────────────────────────────────

def get_active_window_info():
    """Возвращает (exe_name, window_title) или (None, None)."""
    if not WIN32_AVAILABLE:
        return None, None
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid)
        exe = proc.name().lower()
        return exe, title
    except Exception:
        return None, None


def classify_window(exe: str, title: str) -> str:
    """Вернуть категорию: browser | rdp | explorer | app"""
    t = (title or "").lower()
    e = (exe or "").lower()

    for pat in RDP_PATTERNS:
        if re.search(pat, t, re.I) or re.search(pat, e, re.I):
            return "rdp"

    if e in BROWSER_EXES:
        return "browser"

    for pat in EXPLORER_PATTERNS:
        if re.search(pat, t, re.I) or re.search(pat, e, re.I):
            return "explorer"

    return "app"


def extract_rdp_client(title: str) -> str | None:
    """Извлечь имя RDP-хоста из заголовка окна."""
    #  ТИХОВ — 188.40.34.180:7777 — Подключение к удаленному рабочему столу
    patterns = [
        r"(\S+)\s*[-–]\s*remote desktop",
        r"(\S+)\s*[-–]\s*удал[её]нный рабочий стол",
        r"mstsc.*?(\S+)",
    ]
    for pat in patterns:
        m = re.search(pat, title or "", re.I)
        if m:
            return m.group(1).strip()
    # Если просто «Удалённый рабочий стол - hostname»
    parts = re.split(r"[-–]", title or "")
    if len(parts) >= 2:
        candidate = parts[-1].strip()
        if candidate:
            return candidate
    return None


def extract_browser_site(title: str) -> str:
    """Упрощённое извлечение имени вкладки/сайта."""
    # «Google — Mozilla Firefox» → «Google»
    for sep in [" — ", " - ", " | "]:
        if sep in (title or ""):
            return title.split(sep)[0].strip()
    return title or "Браузер"


# ────────────────────────────────────────────────────────────────────────────
#  Трекер (фоновый поток)
# ────────────────────────────────────────────────────────────────────────────

class Tracker:
    def __init__(self):
        self.current_session_id = None
        self.current_exe        = None
        self.current_title      = None
        self.session_start      = None
        self.last_activity      = time.time()
        self.active_client_id   = 1   # «Без клиента» по умолчанию
        self.paused             = False
        self._lock              = threading.Lock()

    def set_client(self, client_id: int):
        with self._lock:
            self.active_client_id = client_id

    def pause(self):
        with self._lock:
            self._close_current_session()
            self.paused = True

    def resume(self):
        with self._lock:
            self.paused = False

    def _close_current_session(self):
        if self.current_session_id is None:
            return
        now = datetime.now()
        dur = int((now - self.session_start).total_seconds()) if self.session_start else 0
        with get_db() as db:
            db.execute(
                "UPDATE sessions SET end_time=?, duration_s=? WHERE id=?",
                (now.strftime("%Y-%m-%d %H:%M:%S"), dur, self.current_session_id)
            )
        self.current_session_id = None
        self.current_exe        = None
        self.current_title      = None
        self.session_start      = None

    def _start_session(self, exe, title, category):
        now = datetime.now()
        with get_db() as db:
        # Определить client_id: для RDP — сначала ищем по rdp_host, потом по имени
            client_id = self.active_client_id
            if category == "rdp":
                rdp_host = extract_rdp_client(title) or "RDP"
                # 1. Клиент с явно прописанным rdp_host
                row = db.execute(
                    "SELECT id FROM clients WHERE rdp_host=?", (rdp_host,)
                ).fetchone()
                if row:
                    client_id = row["id"]
                else:
                    # 2. Клиент с именем = хост (автосозданный раньше)
                    row = db.execute(
                        "SELECT id FROM clients WHERE name=?", (rdp_host,)
                    ).fetchone()
                    if row:
                        client_id = row["id"]
                    else:
                        # 3. Создать нового клиента
                        cur2 = db.execute(
                            "INSERT INTO clients (name, color, rdp_host) VALUES (?,?,?)",
                            (rdp_host, "#f59e0b", rdp_host)
                        )
                        client_id = cur2.lastrowid

            cur = db.execute(
                """INSERT INTO sessions
                   (client_id, app_name, window_title, category, start_time)
                   VALUES (?,?,?,?,?)""",
                (client_id, exe, title, category,
                 now.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.current_session_id = cur.lastrowid
            self.current_exe        = exe
            self.current_title      = title
            self.session_start      = now

    def tick(self):
        with self._lock:
            if self.paused:
                return

            exe, title = get_active_window_info()

            # Нет данных (не Windows или ошибка)
            if exe is None:
                return

            # Обновить время активности
            self.last_activity = time.time()

            category = classify_window(exe, title)

            # Проверить, изменилось ли окно
            changed = (exe != self.current_exe or
                       (category == "rdp" and title != self.current_title) or
                       (category == "browser" and
                        extract_browser_site(title) != extract_browser_site(self.current_title or "")))

            if changed:
                self._close_current_session()
                self._start_session(exe, title, category)
            else:
                # Обновить duration без смены записи
                if self.current_session_id and self.session_start:
                    dur = int((datetime.now() - self.session_start).total_seconds())
                    with get_db() as db:
                        db.execute(
                            "UPDATE sessions SET duration_s=?, end_time=? WHERE id=?",
                            (dur, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             self.current_session_id)
                        )

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"[Tracker error] {e}")
            time.sleep(POLL_SEC)


tracker = Tracker()

# ────────────────────────────────────────────────────────────────────────────
#  REST API
# ────────────────────────────────────────────────────────────────────────────

# — Статика ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "tracker_ui.html")


# — Клиенты ──────────────────────────────────────────────────────────────────
@app.route("/api/clients", methods=["GET"])
def list_clients():
    with get_db() as db:
        rows = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/clients", methods=["POST"])
def create_client():
    data = request.json
    name     = data.get("name", "").strip()
    color    = data.get("color", "#4f8ef7")
    rdp_host = data.get("rdp_host", "").strip() or None
    if not name:
        return jsonify({"error": "name required"}), 400
    with get_db() as db:
        try:
            cur = db.execute(
                "INSERT INTO clients (name, color, rdp_host) VALUES (?,?,?)",
                (name, color, rdp_host)
            )
            return jsonify({"id": cur.lastrowid, "name": name, "color": color, "rdp_host": rdp_host}), 201
        except sqlite3.IntegrityError:
            return jsonify({"error": "already exists"}), 409


@app.route("/api/clients/<int:cid>", methods=["PUT"])
def update_client(cid):
    data     = request.json
    name     = data.get("name", "").strip()
    color    = data.get("color")
    rdp_host = data.get("rdp_host")   # None = не трогать, "" = очистить
    with get_db() as db:
        if name:
            db.execute("UPDATE clients SET name=? WHERE id=?", (name, cid))
        if color:
            db.execute("UPDATE clients SET color=? WHERE id=?", (color, cid))
        if rdp_host is not None:
            val = rdp_host.strip() or None
            db.execute("UPDATE clients SET rdp_host=? WHERE id=?", (val, cid))
    return jsonify({"ok": True})


@app.route("/api/clients/<int:cid>", methods=["DELETE"])
def delete_client(cid):
    if cid <= 2:
        return jsonify({"error": "Нельзя удалить системных клиентов"}), 400
    with get_db() as db:
        db.execute("UPDATE sessions SET client_id=1 WHERE client_id=?", (cid,))
        db.execute("UPDATE notes SET client_id=1 WHERE client_id=?", (cid,))
        db.execute("DELETE FROM clients WHERE id=?", (cid,))
    return jsonify({"ok": True})


# — Активный клиент ──────────────────────────────────────────────────────────
@app.route("/api/active-client", methods=["GET"])
def get_active_client():
    return jsonify({"client_id": tracker.active_client_id})


@app.route("/api/active-client", methods=["POST"])
def set_active_client():
    cid = request.json.get("client_id")
    tracker.set_client(int(cid))
    return jsonify({"ok": True})


# — Трекер control ───────────────────────────────────────────────────────────
@app.route("/api/tracker/pause", methods=["POST"])
def pause_tracker():
    tracker.pause()
    return jsonify({"paused": True})


@app.route("/api/tracker/resume", methods=["POST"])
def resume_tracker():
    tracker.resume()
    return jsonify({"paused": False})


@app.route("/api/tracker/status", methods=["GET"])
def tracker_status():
    return jsonify({
        "paused":      tracker.paused,
        "client_id":   tracker.active_client_id,
        "current_app": tracker.current_exe,
        "current_title": tracker.current_title,
        "session_start": tracker.session_start.isoformat() if tracker.session_start else None,
        "win32_available": WIN32_AVAILABLE,
    })


# — Заметки ──────────────────────────────────────────────────────────────────
@app.route("/api/notes", methods=["GET"])
def list_notes():
    cid       = request.args.get("client_id")
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")
    q = "SELECT n.*, c.name AS client_name, c.color FROM notes n LEFT JOIN clients c ON c.id=n.client_id WHERE 1=1"
    params = []
    if cid:
        q += " AND n.client_id=?"; params.append(cid)
    if date_from:
        q += " AND n.created >= ?"; params.append(date_from)
    if date_to:
        q += " AND n.created <= ?"; params.append(date_to + " 23:59:59")
    q += " ORDER BY n.created DESC"
    with get_db() as db:
        rows = db.execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/notes", methods=["POST"])
def create_note():
    data = request.json
    text = data.get("text", "").strip()
    cid  = data.get("client_id") or tracker.active_client_id
    if not text:
        return jsonify({"error": "text required"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO notes (client_id, text, created) VALUES (?,?,?)",
            (cid, text, now)
        )
    return jsonify({"id": cur.lastrowid, "created": now}), 201


@app.route("/api/notes/<int:nid>", methods=["DELETE"])
def delete_note(nid):
    with get_db() as db:
        db.execute("DELETE FROM notes WHERE id=?", (nid,))
    return jsonify({"ok": True})


# — Сессии / история ─────────────────────────────────────────────────────────
@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    cid       = request.args.get("client_id")
    category  = request.args.get("category")
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")
    limit     = int(request.args.get("limit", 200))

    q = """SELECT s.*, c.name AS client_name, c.color
           FROM sessions s LEFT JOIN clients c ON c.id=s.client_id
           WHERE s.duration_s > 0"""
    params = []
    if cid:
        q += " AND s.client_id=?"; params.append(cid)
    if category:
        q += " AND s.category=?"; params.append(category)
    if date_from:
        q += " AND s.start_time >= ?"; params.append(date_from)
    if date_to:
        q += " AND s.start_time <= ?"; params.append(date_to + " 23:59:59")
    q += f" ORDER BY s.start_time DESC LIMIT {limit}"

    with get_db() as db:
        rows = db.execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


# — Аналитика / отчёт ────────────────────────────────────────────────────────
@app.route("/api/report", methods=["GET"])
def report():
    date_from = request.args.get("date_from", datetime.now().strftime("%Y-%m-%d"))
    date_to   = request.args.get("date_to",   datetime.now().strftime("%Y-%m-%d"))

    with get_db() as db:
        # Итого по клиентам
        by_client = db.execute("""
            SELECT c.id, c.name, c.color,
                   SUM(s.duration_s) AS total_s,
                   COUNT(s.id)       AS sessions
            FROM sessions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.start_time BETWEEN ? AND ?
              AND s.duration_s > 0
            GROUP BY c.id
            ORDER BY total_s DESC
        """, (date_from, date_to + " 23:59:59")).fetchall()

        # По категориям
        by_cat = db.execute("""
            SELECT category, SUM(duration_s) AS total_s
            FROM sessions
            WHERE start_time BETWEEN ? AND ?
              AND duration_s > 0
            GROUP BY category
            ORDER BY total_s DESC
        """, (date_from, date_to + " 23:59:59")).fetchall()

        # По дням (для графика)
        by_day = db.execute("""
            SELECT date(start_time) AS day,
                   c.name AS client_name,
                   c.color,
                   SUM(s.duration_s) AS total_s
            FROM sessions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.start_time BETWEEN ? AND ?
              AND s.duration_s > 0
            GROUP BY day, s.client_id
            ORDER BY day
        """, (date_from, date_to + " 23:59:59")).fetchall()

        # Топ-приложения
        top_apps = db.execute("""
            SELECT app_name, category, c.name AS client_name,
                   SUM(s.duration_s) AS total_s
            FROM sessions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.start_time BETWEEN ? AND ?
              AND s.duration_s > 0
            GROUP BY app_name, s.client_id
            ORDER BY total_s DESC
            LIMIT 20
        """, (date_from, date_to + " 23:59:59")).fetchall()

        # Заметки за период
        notes = db.execute("""
            SELECT n.*, c.name AS client_name, c.color
            FROM notes n
            LEFT JOIN clients c ON c.id = n.client_id
            WHERE n.created BETWEEN ? AND ?
            ORDER BY n.created
        """, (date_from, date_to + " 23:59:59")).fetchall()

    return jsonify({
        "by_client": [dict(r) for r in by_client],
        "by_category": [dict(r) for r in by_cat],
        "by_day": [dict(r) for r in by_day],
        "top_apps": [dict(r) for r in top_apps],
        "notes": [dict(r) for r in notes],
        "date_from": date_from,
        "date_to": date_to,
    })


# — Ручное добавление сессии (для тестирования без Windows) ──────────────────
@app.route("/api/sessions/manual", methods=["POST"])
def manual_session():
    data = request.json
    with get_db() as db:
        db.execute("""
            INSERT INTO sessions
            (client_id, app_name, window_title, category, start_time, end_time, duration_s)
            VALUES (?,?,?,?,?,?,?)
        """, (
            data["client_id"],
            data.get("app_name", "manual"),
            data.get("window_title", ""),
            data.get("category", "app"),
            data.get("start_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            data.get("end_time",   datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            data.get("duration_s", 0),
        ))
    return jsonify({"ok": True}), 201


# ────────────────────────────────────────────────────────────────────────────
#  Точка входа
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("=" * 55)
    print("  WorkTracker запущен")
    print("  Откройте браузер: http://127.0.0.1:5000")
    print("=" * 55)

    # Запуск фонового трекера
    t = threading.Thread(target=tracker.run, daemon=True)
    t.start()

    # Открыть браузер автоматически (через 1 с)
    def open_browser():
        time.sleep(1)
        import webbrowser
        webbrowser.open("http://127.0.0.1:5000")
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="127.0.0.1", port=5000, debug=False)

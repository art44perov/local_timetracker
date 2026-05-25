import os
import time
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import win32gui
import win32process

# Инициализация Flask
app = Flask(__name__)

# Путь к базе данных
DB_PATH = "time_tracking.db"

# Функция для получения активного окна
def get_active_window():
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd)
        return title, pid
    except Exception as e:
        return None, None

# Инициализация базы данных
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_name TEXT,
            start_time TEXT,
            end_time TEXT,
            duration INTEGER,
            client TEXT
        )''')
        conn.commit()

# Функция для записи активности
def log_activity(window_name, start_time, end_time, duration, client=None):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO activity (window_name, start_time, end_time, duration, client)
            VALUES (?, ?, ?, ?, ?)
        ''', (window_name, start_time, end_time, duration, client))
        conn.commit()

# Основной цикл отслеживания
def track_time():
    prev_window, start_time = None, None
    while True:
        try:
            current_window, _ = get_active_window()
            if current_window != prev_window:
                # Записываем предыдущее окно
                if prev_window and start_time:
                    end_time = datetime.now()
                    duration = int((end_time - start_time).total_seconds())
                    log_activity(prev_window, start_time.isoformat(), end_time.isoformat(), duration)
                # Сбрасываем на новое окно
                prev_window = current_window
                start_time = datetime.now()
            time.sleep(1)
        except KeyboardInterrupt:
            print("Остановлен пользователем.")

# Веб-интерфейс для отображения отчетов
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/data", methods=["GET"])
def get_data():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM activity ORDER BY start_time DESC")
        rows = cursor.fetchall()
    return jsonify(rows)

if __name__ == "__main__":
    init_db()
    # Два параллельных процесса: отслеживание и веб-интерфейс
    from threading import Thread
    Thread(target=track_time, daemon=True).start()
    app.run(port=5000)
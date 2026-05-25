import os
import time
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import win32gui
import win32process
from threading import Thread

# Инициализация Flask
app = Flask(__name__)

# Путь к базе данных
DB_PATH = "time_tracker.db"

# Функция для получения активного окна
def get_active_window():
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    title = win32gui.GetWindowText(hwnd)
    return title, pid

# Инициализация базы данных
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_name TEXT NOT NULL,
            client_name TEXT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration INTEGER,
            note TEXT
        )''')
        conn.commit()

# Фиксация времени работы в активных окнах
def track_time():
    prev_window = None
    start_time = None

    while True:
        try:
            # Получаем данные об активном окне
            current_window, _ = get_active_window()
            if current_window != prev_window:
                # Завершаем запись предыдущего окна, если изменился фокус
                if prev_window and start_time:
                    end_time = datetime.now()
                    duration = int((end_time - start_time).total_seconds())
                    record_activity(prev_window, start_time, end_time, duration)

                # Начинаем отслеживать новое окно
                prev_window = current_window
                start_time = datetime.now()

            time.sleep(1)
        except Exception as e:
            print("Ошибка при отслеживании окон:", e)

# Запись активности в базу данных
def record_activity(window_name, start_time, end_time, duration, client_name=None, note=None):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO activity (window_name, client_name, start_time, end_time, duration, note)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (window_name, client_name, start_time.isoformat(), end_time.isoformat(), duration, note))
        conn.commit()

# Маршрут: Главная страница
def calculate_summary():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT client_name, SUM(duration) as total_time
                          FROM activity
                          GROUP BY client_name
                          ORDER BY total_time DESC''')
        data = cursor.fetchall()
    return data

@app.route('/')
def index():
    summary_data = calculate_summary()
    return render_template('index.html', summary=summary_data)

# Маршрут: Получить данные активности
@app.route('/data', methods=['GET'])
def get_data():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT id, window_name, client_name, start_time, end_time, duration, note
                          FROM activity
                          ORDER BY start_time DESC''')
        rows = cursor.fetchall()
    return jsonify(rows)

# Маршрут: Обновить заметку для активности
@app.route('/update_note', methods=['POST'])
def update_note():
    data = request.json
    activity_id = data.get('id')
    note = data.get('note')

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE activity SET note = ? WHERE id = ?', (note, activity_id))
        conn.commit()
    return jsonify({'status': 'success'})

if __name__ == '__main__':
    init_db()
    Thread(target=track_time, daemon=True).start()
    app.run(debug=True)
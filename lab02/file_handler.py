"""
Модуль для работы с файлами, поддержка докачки и подсчет битрейта
"""
import os
import time
import shutil
from app_config import UPLOADS_DIR, PARTIAL_DIR, BUFFER_SIZE


class FileTransferStats:
    """Класс для сбора статистики передачи файлов"""

    def __init__(self):
        self.total_bytes = 0
        self.start_time = None
        self.end_time = None

    def start(self):
        self.start_time = time.time()

    def stop(self):
        self.end_time = time.time()

    def add_bytes(self, bytes_count):
        self.total_bytes += bytes_count

    def get_bitrate(self):
        if not self.start_time or not self.end_time:
            return 0
        duration = self.end_time - self.start_time
        if duration <= 0:
            return 0
        return (self.total_bytes * 8) / duration

    def print_stats(self, operation):
        duration = self.end_time - self.start_time
        bitrate = self.get_bitrate()
        print(f"\n{operation} завершен:")
        print(f"Передано: {self._format_bytes(self.total_bytes)}")
        print(f"Скорость: {bitrate / 1000:.2f} Кбит/с ({bitrate / 1000 / 8:.2f} КБ/с)")
        print(f"Время: {duration:.2f} сек")

    def _format_bytes(self, bytes_count):
        for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
            if bytes_count < 1024.0:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.1f} ТБ"


def ensure_dirs():
    """Создание необходимых директорий"""
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    os.makedirs(PARTIAL_DIR, exist_ok=True)


def get_file_size(filepath):
    """Получение размера файла"""
    try:
        return os.path.getsize(filepath) if os.path.exists(filepath) else 0
    except:
        return 0


def save_partial_file(client_id, filename, data, offset):
    """Сохранение части файла для докачки"""
    safe_client = "".join(c for c in client_id if c.isalnum() or c in '._-')
    safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')

    partial_path = os.path.join(PARTIAL_DIR, f"{safe_client}_{safe_filename}.part")

    with open(partial_path, 'ab') as f:
        f.seek(offset)
        f.write(data)

    return partial_path


def finalize_file(client_id, filename):
    """Завершение файла - перенос из временной папки"""
    safe_client = "".join(c for c in client_id if c.isalnum() or c in '._-')
    safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')

    partial_path = os.path.join(PARTIAL_DIR, f"{safe_client}_{safe_filename}.part")
    final_path = os.path.join(UPLOADS_DIR, filename)

    if os.path.exists(partial_path):
        try:
            if os.path.exists(final_path):
                base, ext = os.path.splitext(filename)
                final_path = os.path.join(UPLOADS_DIR, f"{base}_new{ext}")

            shutil.move(partial_path, final_path)
            return final_path
        except Exception as e:
            print(f"Ошибка при завершении файла: {e}")
            return None

    return None


def get_partial_size(client_id, filename):
    """Получение размера частично загруженного файла"""
    safe_client = "".join(c for c in client_id if c.isalnum() or c in '._-')
    safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')

    partial_path = os.path.join(PARTIAL_DIR, f"{safe_client}_{safe_filename}.part")

    if os.path.exists(partial_path):
        return os.path.getsize(partial_path)
    return 0


def cleanup_partial(client_id, filename=None):
    """Очистка временных файлов"""
    safe_client = "".join(c for c in client_id if c.isalnum() or c in '._-')

    try:
        if filename:
            safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')
            partial_path = os.path.join(PARTIAL_DIR, f"{safe_client}_{safe_filename}.part")
            if os.path.exists(partial_path):
                os.remove(partial_path)
        else:
            for f in os.listdir(PARTIAL_DIR):
                if f.startswith(f"{safe_client}_"):
                    os.remove(os.path.join(PARTIAL_DIR, f))
    except Exception as e:
        print(f"Ошибка при очистке: {e}")
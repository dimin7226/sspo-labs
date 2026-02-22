#!/usr/bin/env python3
"""
TCP сервер для передачи файлов с поддержкой команд и докачки
"""
import socket
import time
import threading
import os
import shutil
from datetime import datetime

from app_config import *
from socket_handler import set_keepalive, recv_until, recv_exact, send_all
from file_handler import (
    ensure_dirs, get_file_size, save_partial_file,
    finalize_file, get_partial_size, FileTransferStats,
    cleanup_partial
)
from keepalive import ConnectionMonitor


class TCPServer:
    """TCP сервер с поддержкой команд и передачи файлов"""

    def __init__(self, host=SERVER_HOST, port=SERVER_PORT):
        self.host = host
        self.port = port
        self.server_socket = None
        self.running = False
        self.start_time = time.time()
        self.client_sessions = {}

        ensure_dirs()
        print(f"Сервер запущен на {self.host}:{self.port}")
        print(f"Директория загрузок: {os.path.abspath(UPLOADS_DIR)}")
        print(f"Директория временных файлов: {os.path.abspath(PARTIAL_DIR)}")

    def start(self):
        """Запуск сервера"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(5)

        self.running = True
        print("\nДоступные команды: ECHO, TIME, UPLOAD, DOWNLOAD, CLOSE")

        try:
            while self.running:
                client_sock, client_addr = self.server_socket.accept()
                print(f"\nНовое подключение от {client_addr}")

                client_thread = threading.Thread(
                    target=self.handle_client,
                    args=(client_sock, client_addr)
                )
                client_thread.daemon = True
                client_thread.start()

        except KeyboardInterrupt:
            print("\nСервер остановлен по запросу")
        finally:
            self.stop()

    def stop(self):
        """Остановка сервера"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        print("Сервер остановлен")

    def handle_client(self, client_sock, client_addr):
        """Обработка клиентского подключения"""
        client_id = f"{client_addr[0]}:{client_addr[1]}"

        try:
            # Получение идентификатора клиента
            data = recv_until(client_sock)
            if data.startswith("CLIENT "):
                client_id = data[7:]
                print(f"Клиент идентифицирован как: {client_id}")

            send_all(client_sock, "OK\n")

            # Основной цикл обработки команд
            while self.running:
                try:
                    command = recv_until(client_sock)
                    if not command:
                        break

                    print(f"Получена команда от {client_id}: {command}")

                    if command == "CLOSE":
                        send_all(client_sock, "Соединение закрывается\n")
                        break

                    elif command == "TIME":
                        response = f"Текущее время сервера: {datetime.now().strftime('%H:%M:%S')}\n"
                        send_all(client_sock, response)

                    elif command.startswith("ECHO"):
                        response = command[5:] + "\n"
                        send_all(client_sock, response)

                    elif command.startswith("UPLOAD "):
                        # Формат: UPLOAD filename filesize
                        parts = command.split()
                        if len(parts) == 3:
                            filename = parts[1]
                            filesize = int(parts[2])
                            self.handle_upload(client_sock, client_id, filename, filesize)
                        else:
                            send_all(client_sock,
                                     "ERROR: Неверный формат команды UPLOAD. Используйте: UPLOAD filename filesize\n")

                    elif command.startswith("DOWNLOAD "):
                        filename = command[9:]
                        self.handle_download(client_sock, filename)

                    else:
                        send_all(client_sock, "Неизвестная команда\n")

                except ConnectionError as e:
                    print(f"Ошибка соединения с {client_id}: {e}")
                    break

        except Exception as e:
            print(f"Ошибка при обработке клиента {client_id}: {e}")
        finally:
            client_sock.close()
            print(f"Соединение с {client_id} закрыто")

    def handle_upload(self, client_sock, client_id, filename, filesize):
        """Обработка загрузки файла"""
        print(f"Начало загрузки файла {filename} размером {filesize} байт от клиента {client_id}")

        stats = FileTransferStats()
        stats.start()

        # Путь к временному файлу
        safe_client = "".join(c for c in client_id if c.isalnum() or c in '._-')
        safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')
        partial_path = os.path.join(PARTIAL_DIR, f"{safe_client}_{safe_filename}.part")
        final_path = os.path.join(UPLOADS_DIR, filename)

        try:
            # Если файл уже существует, добавляем суффикс
            if os.path.exists(final_path):
                base, ext = os.path.splitext(filename)
                final_path = os.path.join(UPLOADS_DIR, f"{base}_{int(time.time())}{ext}")
                print(f"Файл уже существует, сохраняем как: {os.path.basename(final_path)}")

            with open(partial_path, 'wb') as f:
                received = 0
                while received < filesize:
                    chunk_size = min(BUFFER_SIZE, filesize - received)
                    data = recv_exact(client_sock, chunk_size)

                    f.write(data)
                    received += len(data)
                    stats.add_bytes(len(data))

                    # Показываем прогресс
                    percent = (received / filesize) * 100
                    print(f"\rЗагрузка {filename}: {percent:.1f}%", end="")

            print()

            # Перемещаем файл из временной папки
            shutil.move(partial_path, final_path)

            stats.stop()
            stats.print_stats("Загрузка файла")

            # Отправляем подтверждение
            response = f"Файл {os.path.basename(final_path)} успешно загружен\n"
            send_all(client_sock, response)

        except Exception as e:
            print(f"\nОшибка при загрузке: {e}")
            # Удаляем временный файл в случае ошибки
            if os.path.exists(partial_path):
                os.remove(partial_path)
            send_all(client_sock, f"ERROR: {e}\n")

    def handle_download(self, client_sock, filename):
        """Обработка скачивания файла"""
        filepath = os.path.join(UPLOADS_DIR, filename)

        if not os.path.exists(filepath):
            send_all(client_sock, "ERROR: Файл не найден\n")
            return

        filesize = get_file_size(filepath)

        # Отправляем размер файла
        send_all(client_sock, f"FILESIZE {filesize}\n")

        # Проверяем, нужно ли докачивать
        try:
            offset_str = recv_until(client_sock)
            if offset_str.isdigit():
                offset = int(offset_str)
                print(f"Докачка файла {filename} с позиции {offset}")
            else:
                offset = 0
        except:
            offset = 0

        stats = FileTransferStats()
        stats.start()

        try:
            with open(filepath, 'rb') as f:
                if offset > 0:
                    f.seek(offset)

                remaining = filesize - offset
                sent = 0

                while remaining > 0:
                    chunk_size = min(BUFFER_SIZE, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break

                    send_all(client_sock, data)
                    sent += len(data)
                    remaining -= len(data)
                    stats.add_bytes(len(data))

                    percent = ((offset + sent) / filesize) * 100
                    print(f"\rСкачивание {filename}: {percent:.1f}%", end="")

            print()
            stats.stop()
            stats.print_stats("Скачивание файла")

        except Exception as e:
            print(f"\nОшибка при скачивании: {e}")


def main():
    """Точка входа"""
    server = TCPServer()

    try:
        server.start()
    except KeyboardInterrupt:
        print("\nЗавершение работы сервера...")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
TCP клиент для передачи файлов с поддержкой докачки
"""
import socket
import os
import time
import signal
import sys

from app_config import *
from socket_handler import set_keepalive, recv_until, recv_exact, send_all
from file_handler import (
    ensure_dirs, get_file_size, get_partial_size,
    FileTransferStats
)


class TCPClient:
    """TCP клиент с поддержкой команд и передачи файлов"""

    def __init__(self, server_host, server_port, client_id="1"):
        self.server_host = server_host
        self.server_port = server_port
        self.client_id = client_id
        self.socket = None
        self.connected = False

        ensure_dirs()

    def connect(self):
        """Подключение к серверу"""
        if self.connected:
            print("Уже подключено к серверу")
            return True

        try:
            print(f"Подключение к {self.server_host}:{self.server_port}...")

            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(CONNECTION_TIMEOUT)

            self.socket.connect((self.server_host, self.server_port))
            self.socket.settimeout(SOCKET_TIMEOUT)

            # Отправляем идентификатор клиента
            send_all(self.socket, f"CLIENT {self.client_id}\n")

            # Получаем ответ
            response = recv_until(self.socket)

            if response == "OK":
                self.connected = True
                print(f"✓ Подключено к серверу {self.server_host}:{self.server_port}")
                return True
            else:
                print(f"✗ Ошибка: {response}")
                return False

        except ConnectionRefusedError:
            print("✗ Сервер недоступен")
            return False
        except Exception as e:
            print(f"✗ Ошибка подключения: {e}")
            return False

    def disconnect(self):
        """Отключение от сервера"""
        if self.connected:
            try:
                send_all(self.socket, "CLOSE\n")
            except:
                pass

        if self.socket:
            self.socket.close()

        self.connected = False
        print("Отключено от сервера")

    def send_command(self, command):
        """Отправка команды серверу"""
        if not self.connected:
            print("✗ Нет подключения к серверу. Выполните CONNECT")
            return

        try:
            parts = command.strip().split()
            if not parts:
                return

            cmd = parts[0].upper()

            if cmd == "CLOSE":
                self.disconnect()

            elif cmd == "TIME":
                self._send_simple_command(command)

            elif cmd == "ECHO":
                self._send_simple_command(command)

            elif cmd == "UPLOAD" and len(parts) >= 2:
                filename = ' '.join(parts[1:])
                self.upload_file(filename)

            elif cmd == "DOWNLOAD" and len(parts) >= 2:
                filename = ' '.join(parts[1:])
                self.download_file(filename)

            else:
                print(f"✗ Неизвестная команда: {command}")

        except ConnectionError as e:
            print(f"✗ Ошибка отправки команды: {e}")
            self.connected = False
        except Exception as e:
            print(f"✗ Ошибка: {e}")

    def _send_simple_command(self, command):
        """Отправка простой команды"""
        send_all(self.socket, f"{command}\n")
        response = recv_until(self.socket)
        print(f"Ответ: {response}")

    def upload_file(self, filename):
        """Загрузка файла на сервер"""
        if not os.path.exists(filename):
            print(f"✗ Файл '{filename}' не найден")
            return

        filesize = get_file_size(filename)
        basename = os.path.basename(filename)

        print(f"\nЗагрузка файла '{basename}' ({filesize} байт)...")

        # Отправляем команду с размером файла
        cmd = f"UPLOAD {basename} {filesize}\n"
        send_all(self.socket, cmd)

        stats = FileTransferStats()
        stats.start()

        try:
            with open(filename, 'rb') as f:
                sent = 0
                while sent < filesize:
                    data = f.read(BUFFER_SIZE)
                    if not data:
                        break

                    send_all(self.socket, data)
                    sent += len(data)
                    stats.add_bytes(len(data))

                    percent = (sent / filesize) * 100
                    print(f"\rЗагрузка: {percent:.1f}%", end="")

            print()
            stats.stop()
            stats.print_stats("Загрузка файла")

            # Ждем подтверждение от сервера
            response = recv_until(self.socket)
            print(f"Сервер: {response}")

        except Exception as e:
            print(f"\n✗ Ошибка при загрузке: {e}")

    def download_file(self, filename):
        """Скачивание файла с сервера"""
        basename = os.path.basename(filename)

        print(f"\nСкачивание файла '{basename}'...")

        # Отправляем команду
        cmd = f"DOWNLOAD {basename}\n"
        send_all(self.socket, cmd)

        # Получаем размер файла
        response = recv_until(self.socket)
        if response.startswith("ERROR"):
            print(f"✗ {response}")
            return

        if response.startswith("FILESIZE "):
            filesize = int(response[9:])
            print(f"Размер файла: {filesize} байт")
        else:
            print(f"✗ Неожиданный ответ: {response}")
            return

        # Проверяем частичную загрузку
        offset = 0
        if os.path.exists(basename):
            existing = get_file_size(basename)
            if existing < filesize:
                print(f"↻ Найден частичный файл, продолжаем с {existing} байт")
                offset = existing
                send_all(self.socket, f"{offset}\n")
            elif existing == filesize:
                print("✓ Файл уже полностью скачан")
                return
            else:
                # Файл больше, чем на сервере - перезаписываем
                offset = 0
                send_all(self.socket, "0\n")
        else:
            send_all(self.socket, "0\n")

        stats = FileTransferStats()
        stats.start()

        try:
            mode = 'ab' if offset > 0 else 'wb'
            with open(basename, mode) as f:
                if mode == 'ab':
                    f.seek(offset)

                received = offset
                while received < filesize:
                    chunk_size = min(BUFFER_SIZE, filesize - received)
                    data = recv_exact(self.socket, chunk_size)

                    f.write(data)
                    received += len(data)
                    stats.add_bytes(len(data))

                    percent = (received / filesize) * 100
                    print(f"\rСкачивание: {percent:.1f}%", end="")

            print()
            stats.stop()
            stats.print_stats("Скачивание файла")

        except Exception as e:
            print(f"\n✗ Ошибка при скачивании: {e}")

    def run(self):
        """Основной цикл клиента"""
        print("TCP Клиент для передачи файлов")
        print("Доступные команды:")
        print("  CONNECT - подключение к серверу")
        print("  TIME - время сервера")
        print("  ECHO <текст> - эхо-команда")
        print("  UPLOAD <файл> - загрузить файл на сервер")
        print("  DOWNLOAD <файл> - скачать файл с сервера")
        print("  CLOSE - закрыть соединение")
        print("  Q - выход из программы")

        while True:
            try:
                cmd = input("\n> ").strip()

                if cmd.upper() == "Q":
                    if self.connected:
                        self.disconnect()
                    break

                elif cmd.upper() == "CONNECT":
                    self.connect()

                elif self.connected:
                    self.send_command(cmd)
                else:
                    print("✗ Сначала выполните CONNECT")

            except KeyboardInterrupt:
                print("\nЗавершение работы...")
                break
            except Exception as e:
                print(f"✗ Ошибка: {e}")

        if self.connected:
            self.disconnect()


def main():
    """Точка входа"""
    server_host = input("Введите IP сервера (по умолчанию localhost): ").strip()
    if not server_host:
        server_host = "localhost"

    client_id = input("Введите ID клиента (по умолчанию 1): ").strip()
    if not client_id:
        client_id = "1"

    client = TCPClient(server_host, SERVER_PORT, client_id)

    try:
        client.run()
    except KeyboardInterrupt:
        print("\nЗавершение работы клиента...")


if __name__ == "__main__":
    main()
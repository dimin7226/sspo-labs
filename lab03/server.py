#!/usr/bin/env python3
"""
TCP/UDP сервер для передачи файлов с поддержкой команд и докачки
"""
import socket
import time
import threading
import os
import shutil
import select
from datetime import datetime
from unittest import result

from pynvim import command

from app_config import *
from socket_handler import set_keepalive, recv_until, recv_exact, send_all
from file_handler import (
    ensure_dirs,
    get_file_size,
    save_partial_file,
    finalize_file,
    get_partial_size,
    FileTransferStats,
    cleanup_partial,
)
from keepalive import ConnectionMonitor
from udp_handler import *
from sliding_window import SlidingWindow, ReceiveWindow


class TCPServerHandler:
    """Обработчик TCP подключений (полностью из оригинального server.py)"""

    def __init__(self, server):
        self.server = server

    def process_command(self, client_sock, client_addr, command):
        """Обработка одной TCP команды"""

        client_id = f"{client_addr[0]}:{client_addr[1]}"

        try:
            print(f"Команда от {client_id}: {command}")

            if command == "CLOSE":
                send_all(client_sock, "Соединение закрывается\n")
                return True  # сигнал на закрытие

            elif command == "TIME":
                response = (
                    f"Текущее время сервера: {datetime.now().strftime('%H:%M:%S')}\n"
                )
                send_all(client_sock, response)

            elif command.startswith("ECHO"):
                response = command[5:] + "\n" if len(command) > 5 else "\n"
                send_all(client_sock, response)

            elif command.startswith("UPLOAD "):
                parts = command.split()
                if len(parts) == 3:
                    filename = parts[1]
                    filesize = int(parts[2])
                    self._handle_upload_nonblocking(client_sock, filename, filesize)
                else:
                    send_all(client_sock, "ERROR: Неверный формат UPLOAD\n")

            elif command.startswith("DOWNLOAD "):
                filename = command[9:]
                self._handle_download_nonblocking(client_sock, filename)

            else:
                send_all(client_sock, "Неизвестная команда\n")

        except Exception as e:
            send_all(client_sock, f"ERROR: {e}\n")

        return False

    def _handle_download_nonblocking(self, client_sock, filename):
        """Инициализация неблокирующего скачивания файла"""

        filename = filename.strip()
        filepath = os.path.join(UPLOADS_DIR, filename)

        if not os.path.exists(filepath):
            send_all(client_sock, "ERROR: Файл не найден\n")
            return

        filesize = os.path.getsize(filepath)
        send_all(client_sock, f"FILESIZE {filesize}\n")

        try:
            offset_str = recv_until(client_sock)
            offset = int(offset_str) if offset_str.isdigit() else 0
        except Exception:
            offset = 0

        try:
            f = open(filepath, "rb")
            if offset > 0:
                f.seek(offset)

            client_sock.download_state = {
                "file": f,
                "filesize": filesize,
                "offset": offset,
                "sent": offset,
                "filename": filename,
            }

            print(f"Начато неблокирующее скачивание {filename}")

        except Exception as e:
            send_all(client_sock, f"ERROR: {e}\n")

    def _handle_upload_nonblocking(self, client_sock, filename, filesize):
        """Инициализация неблокирующей загрузки"""

        filepath = os.path.join(UPLOADS_DIR, filename)

        client_sock.upload_state = {
            "filename": filename,
            "filepath": filepath,
            "filesize": filesize,
            "received": 0,
            "file": open(filepath, "wb"),
        }

        print(f"Начата неблокирующая загрузка {filename} ({filesize} байт)")

    def _handle_upload(self, client_sock, client_id, filename, filesize):
        """Обработка загрузки файла (оригинальный код)"""
        print(
            f"Начало загрузки файла {filename} размером {filesize} байт от клиента {client_id}"
        )

        stats = FileTransferStats()
        stats.start()

        safe_client = "".join(c for c in client_id if c.isalnum() or c in "._-")
        safe_filename = "".join(c for c in filename if c.isalnum() or c in "._-")
        partial_path = os.path.join(PARTIAL_DIR, f"{safe_client}_{safe_filename}.part")
        final_path = os.path.join(UPLOADS_DIR, filename)

        try:
            if os.path.exists(final_path):
                base, ext = os.path.splitext(filename)
                final_path = os.path.join(
                    UPLOADS_DIR, f"{base}_{int(time.time())}{ext}"
                )
                print(
                    f"Файл уже существует, сохраняем как: {os.path.basename(final_path)}"
                )

            with open(partial_path, "wb") as f:
                received = 0
                while received < filesize:
                    chunk_size = min(BUFFER_SIZE, filesize - received)
                    data = recv_exact(client_sock, chunk_size)
                    f.write(data)
                    received += len(data)
                    stats.add_bytes(len(data))
                    percent = (received / filesize) * 100
                    print(f"\rЗагрузка {filename}: {percent:.1f}%", end="")

            print()
            shutil.move(partial_path, final_path)
            stats.stop()
            stats.print_stats("Загрузка файла")
            response = f"Файл {os.path.basename(final_path)} успешно загружен\n"
            send_all(client_sock, response)

        except Exception as e:
            print(f"\nОшибка при загрузке: {e}")
            if os.path.exists(partial_path):
                os.remove(partial_path)
            send_all(client_sock, f"ERROR: {e}\n")

    def _handle_download(self, client_sock, filename):
        """Обработка скачивания файла (оригинальный код)"""
        # Очищаем filename от возможных пробелов и символов
        filename = filename.strip()

        # Проверяем несколько возможных путей
        possible_paths = [
            os.path.join(UPLOADS_DIR, filename),  # uploads/filename
            os.path.join(os.getcwd(), UPLOADS_DIR, filename),  # полный путь
            filename,  # прямой путь
            os.path.join(UPLOADS_DIR, os.path.basename(filename)),  # только имя
        ]

        filepath = None
        for path in possible_paths:
            print(f"Проверка пути: {path}")
            if os.path.exists(path):
                filepath = path
                print(f"Файл найден: {path}")
                break

        if not filepath:
            print(f"Файл не найден. Проверенные пути: {possible_paths}")
            print(f"Содержимое папки {UPLOADS_DIR}:")
            try:
                files = os.listdir(UPLOADS_DIR)
                for f in files:
                    print(f"  - {f}")
            except Exception as e:
                print(f"Ошибка чтения папки: {e}")

            send_all(client_sock, "ERROR: Файл не найден\n")
            return

        filesize = get_file_size(filepath)
        print(f"Размер файла: {filesize} байт")
        send_all(client_sock, f"FILESIZE {filesize}\n")

        try:
            offset_str = recv_until(client_sock)
            print(f"Получен offset: {offset_str}")
            offset = int(offset_str) if offset_str.isdigit() else 0
            if offset > 0:
                print(f"Докачка файла {filename} с позиции {offset}")
        except Exception as e:
            print(f"Ошибка при получении offset: {e}")
            offset = 0

        stats = FileTransferStats()
        stats.start()

        try:
            with open(filepath, "rb") as f:
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


class UDPServerHandler:
    """Обработчик UDP подключений"""

    def __init__(self, server):
        self.server = server
        self.clients = {}  # addr -> session_data
        self.client_sessions = {}

    def handle_packet(self, data, client_addr):
        """Обработка UDP пакета"""
        result = parse_packet(data)
        if not result:
            return

        packet_id, total_packets, flags, payload = result
        print(
            f"UDP пакет от {client_addr}: id={packet_id}, flags={flags}, размер={len(payload)}"
        )

        # Отправляем ACK
        if not (flags & FLAG_START):
            ack = create_ack_packet(packet_id)
            self.server.udp_socket.sendto(ack, client_addr)

        if flags & FLAG_START:
            self._handle_start(client_addr, payload)
            return

        if flags & FLAG_DATA:
            self._handle_data(client_addr, packet_id, total_packets, flags, payload)

        elif flags & FLAG_END:
            self._handle_end(client_addr, payload)

    def _handle_start(self, client_addr, payload):
        """Обработка начала сессии"""
        try:
            data = payload.decode("utf-8")
            print(f"UDP START: {data}")

            if data.startswith("CLIENT "):
                client_id = data[7:]
                self.clients[client_addr] = {
                    "client_id": client_id,
                    "connected": True,
                    "time": time.time(),
                    "recv_window": ReceiveWindow(),
                    "file_session": {},
                }
                print(f"UDP клиент {client_id} подключился с адреса {client_addr}")

                # Отправляем подтверждение
                packet = create_packet(0, 1, FLAG_ACK | FLAG_END, b"OK")
                self.server.udp_socket.sendto(packet, client_addr)

        except Exception as e:
            print(f"Ошибка при начале UDP сессии: {e}")

    def _handle_end(self, client_addr, payload):
        """Обработка завершения сессии"""
        try:
            data = payload.decode("utf-8")
            print(f"UDP END: {data}")

            if data == "CLOSE":
                if client_addr in self.clients:
                    client_id = self.clients[client_addr].get("client_id", "unknown")
                    del self.clients[client_addr]
                    print(f"UDP клиент {client_id} отключился")
            else:
                # Это может быть команда
                self._handle_command(client_addr, self.clients.get(client_addr), data)

        except Exception as e:
            print(f"Ошибка при завершении UDP сессии: {e}")

    def _handle_data(self, client_addr, packet_id, total_packets, flags, payload):
        """Обработка данных"""
        if client_addr not in self.clients:
            print(f"UDP данные от неизвестного клиента {client_addr}")
            # Создаем временную сессию
            self.clients[client_addr] = {
                "client_id": "unknown",
                "connected": True,
                "time": time.time(),
                "recv_window": ReceiveWindow(),
                "file_session": {},
            }

        client_info = self.clients[client_addr]

        # Определяем тип данных по packet_id
        if packet_id == 0:  # Команда
            self._handle_command(
                client_addr, client_info, payload.decode("utf-8", errors="ignore")
            )
        else:  # Данные файла
            self._handle_file_data(
                client_addr, client_info, packet_id, total_packets, flags, payload
            )

    def _handle_command(self, client_addr, client_info, command):
        """Обработка команд"""
        try:
            command = command.strip()
            print(f"UDP команда от {client_info.get('client_id')}: '{command}'")

            if command == "TIME":
                response = f"Текущее время: {time.strftime('%H:%M:%S')}"
                self._send_response(client_addr, response)

            elif command.startswith("ECHO"):
                response = command[5:] if len(command) > 5 else ""
                self._send_response(client_addr, response)

            elif command.startswith("UPLOAD "):
                parts = command.split()
                if len(parts) == 3:
                    filename = parts[1]
                    filesize = int(parts[2])
                    client_info["file_session"] = {
                        "filename": filename,
                        "filesize": filesize,
                        "received": 0,
                        "packets": {},
                        "start_time": time.time(),
                    }
                    self._send_response(client_addr, "READY")
                else:
                    self._send_response(client_addr, "ERROR: Invalid UPLOAD command")

            elif command.startswith("DOWNLOAD "):
                filename = command[9:].strip()
                self._handle_download(client_addr, filename)

            else:
                self._send_response(client_addr, f"Unknown command: {command}")

        except Exception as e:
            print(f"Ошибка обработки UDP команды: {e}")
            self._send_response(client_addr, f"ERROR: {e}")

    def _handle_file_data(
        self, client_addr, client_info, packet_id, total_packets, flags, payload
    ):
        """Обработка данных файла"""
        session = client_info.get("file_session")
        if not session:
            print(f"Нет активной сессии для UDP клиента {client_addr}")
            return

        session["packets"][packet_id] = payload
        session["received"] += len(payload)

        percent = (session["received"] / session["filesize"]) * 100
        print(f"\rUDP прием {session['filename']}: {percent:.1f}%", end="")

        if flags & FLAG_END:
            self._finalize_upload(client_addr, client_info)

    def _finalize_upload(self, client_addr, client_info):
        """Завершение UDP загрузки"""
        session = client_info.get("file_session")
        if not session:
            return

        filename = session["filename"]
        filepath = os.path.join(UPLOADS_DIR, filename)

        # Проверяем, не существует ли уже файл
        if os.path.exists(filepath):
            base, ext = os.path.splitext(filename)
            filepath = os.path.join(UPLOADS_DIR, f"{base}_udp{ext}")

        # Собираем пакеты в правильном порядке
        with open(filepath, "wb") as f:
            for packet_id in sorted(session["packets"].keys()):
                f.write(session["packets"][packet_id])

        duration = time.time() - session["start_time"]
        bitrate = (session["filesize"] * 8) / duration if duration > 0 else 0

        print(f"\n✓ UDP файл {os.path.basename(filepath)} загружен")
        print(f"  Размер: {session['filesize']} байт")
        print(f"  Скорость: {bitrate / 1000:.2f} Кбит/с")

        self._send_response(client_addr, f"UPLOAD_OK {os.path.basename(filepath)}")
        client_info["file_session"] = {}

    def _handle_download(self, client_addr, filename):
        """Обработка UDP скачивания"""
        # Очищаем filename
        filename = filename.strip()

        # Проверяем несколько возможных путей
        possible_paths = [
            os.path.join(UPLOADS_DIR, filename),
            os.path.join(os.getcwd(), UPLOADS_DIR, filename),
            filename,
            os.path.join(UPLOADS_DIR, os.path.basename(filename)),
        ]

        filepath = None
        for path in possible_paths:
            print(f"UDP проверка пути: {path}")
            if os.path.exists(path):
                filepath = path
                print(f"UDP файл найден: {path}")
                break

        if not filepath:
            print(f"UDP файл не найден. Проверенные пути: {possible_paths}")
            print(f"Содержимое папки {UPLOADS_DIR}:")
            try:
                files = os.listdir(UPLOADS_DIR)
                for f in files:
                    print(f"  - {f}")
            except Exception as e:
                print(f"Ошибка чтения папки: {e}")

            self._send_response(client_addr, "ERROR: Файл не найден")
            return

        filesize = os.path.getsize(filepath)
        print(f"UDP размер файла: {filesize} байт")
        self._send_response(client_addr, f"FILESIZE {filesize}")

        # Небольшая пауза для обработки
        time.sleep(0.2)

        # Отправляем файл
        try:
            with open(filepath, "rb") as f:
                packet_seq = 1000
                sent = 0
                total_packets = (filesize + (1400 - PACKET_HEADER_SIZE) - 1) // (
                    1400 - PACKET_HEADER_SIZE
                )

                while True:
                    chunk = f.read(1400 - PACKET_HEADER_SIZE)
                    if not chunk:
                        break

                    flags = FLAG_DATA
                    sent += len(chunk)
                    if sent >= filesize:
                        flags |= FLAG_END

                    packet = create_packet(packet_seq, total_packets, flags, chunk)
                    self.server.udp_socket.sendto(packet, client_addr)

                    packet_seq += 1
                    percent = (sent / filesize) * 100
                    print(f"\rUDP отправка {filename}: {percent:.1f}%", end="")

                    # Небольшая задержка для предотвращения переполнения
                    time.sleep(0.002)

            print(f"\nUDP файл {filename} отправлен")

        except Exception as e:
            print(f"Ошибка при UDP отправке: {e}")

    def _send_response(self, client_addr, response_text):
        """Отправка UDP ответа"""
        try:
            data = response_text.encode("utf-8")
            print(f"Отправка UDP ответа {client_addr}: {response_text}")

            # Разбиваем на пакеты если нужно
            max_chunk = 1400 - PACKET_HEADER_SIZE
            total_packets = (len(data) + max_chunk - 1) // max_chunk

            for i in range(total_packets):
                start = i * max_chunk
                end = min(start + max_chunk, len(data))
                chunk = data[start:end]

                flags = FLAG_DATA
                if i == total_packets - 1:
                    flags |= FLAG_END

                packet = create_packet(i, total_packets, flags, chunk)
                self.server.udp_socket.sendto(packet, client_addr)

                # Небольшая задержка
                time.sleep(0.001)

        except Exception as e:
            print(f"Ошибка отправки UDP ответа: {e}")


class Server:
    """Единый сервер с поддержкой TCP и UDP"""

    def __init__(
        self,
        tcp_host=SERVER_HOST,
        tcp_port=SERVER_PORT,
        udp_host=SERVER_HOST,
        udp_port=SERVER_PORT + 1,
    ):
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.udp_host = udp_host
        self.udp_port = udp_port

        self.running = False
        self.tcp_socket = None
        self.udp_socket = None

        # Инициализация обработчиков
        self.tcp_handler = TCPServerHandler(self)
        self.udp_handler = UDPServerHandler(self)

        ensure_dirs()

    def start(self):
        """Запуск сервера (ЛР №3 — мультиплексирование через select)"""

        self.running = True

        # --- TCP ---
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_socket.bind((self.tcp_host, self.tcp_port))
        self.tcp_socket.listen()
        self.tcp_socket.setblocking(False)

        # --- UDP ---
        self.udp_socket = create_udp_socket()
        self.udp_socket.bind((self.udp_host, self.udp_port))
        self.udp_socket.setblocking(False)

        print(f"TCP сервер: {self.tcp_host}:{self.tcp_port}")
        print(f"UDP сервер: {self.udp_host}:{self.udp_port}")

        # monitored sockets
        inputs = [self.tcp_socket, self.udp_socket]
        tcp_clients = {}  # sock -> client_addr

        try:
            while self.running:

                readable, _, exceptional = select.select(inputs, [], inputs, 1)

                for sock in readable:

                    # --- новое TCP подключение ---
                    if sock is self.tcp_socket:
                        client_sock, client_addr = self.tcp_socket.accept()
                        client_sock.setblocking(False)
                        inputs.append(client_sock)
                        tcp_clients[client_sock] = client_addr
                        print(f"Новое TCP подключение: {client_addr}")

                    # --- UDP пакет ---
                    elif sock is self.udp_socket:
                        data, addr = self.udp_socket.recvfrom(65535)
                        self.udp_handler.handle_packet(data, addr)

                    # --- данные TCP клиента ---
                    else:
                        if not self._handle_tcp_event(sock, tcp_clients, inputs):
                            continue

                # --- обработка ошибок ---
                for sock in exceptional:
                    self._close_tcp_client(sock, tcp_clients, inputs)

        except KeyboardInterrupt:
            print("\nОстановка сервера...")

        finally:
            self.stop()

    def _handle_tcp_event(self, sock, tcp_clients, inputs):
        """Обработка события TCP клиента (однопоточно)"""

        try:
            data = sock.recv(BUFFER_SIZE)

            if not data:
                self._close_tcp_client(sock, tcp_clients, inputs)
                return False

            # Если идёт загрузка файла — принимаем бинарные данные
            if hasattr(sock, "upload_state"):
                state = sock.upload_state
                remaining = state["filesize"] - state["received"]

                chunk = data[:remaining]
                state["file"].write(chunk)
                state["received"] += len(chunk)

                if state["received"] >= state["filesize"]:
                    state["file"].close()
                    send_all(sock, f"Файл {state['filename']} успешно загружен\n")
                    print(f"\nUPLOAD завершён: {state['filename']}")
                    del sock.upload_state

                # если в data были лишние байты — возвращаем в буфер
                extra = data[len(chunk) :]
                if extra:
                    if not hasattr(sock, "buffer"):
                        sock.buffer = b""
                    sock.buffer += extra

                return True

            # --- неблокирующее скачивание ---
            if hasattr(sock, "download_state"):
                state = sock.download_state
                remaining = state["filesize"] - state["sent"]

                if remaining > 0:
                    chunk_size = min(BUFFER_SIZE, remaining)
                    data = state["file"].read(chunk_size)

                    if data:
                        send_all(sock, data)
                        state["sent"] += len(data)
                else:
                    state["file"].close()
                    print(f"DOWNLOAD завершён: {state['filename']}")
                    del sock.download_state

                return True

            # обычная буферизация команд
            if not hasattr(sock, "buffer"):
                sock.buffer = b""

            sock.buffer += data

            while b"\n" in sock.buffer:
                line, sock.buffer = sock.buffer.split(b"\n", 1)
                command = line.decode("utf-8").strip()

                should_close = self.tcp_handler.process_command(
                    sock, tcp_clients[sock], command
                )

                if should_close:
                    self._close_tcp_client(sock, tcp_clients, inputs)
                    return False

            return True

        except Exception:
            self._close_tcp_client(sock, tcp_clients, inputs)
            return False

    def _close_tcp_client(self, sock, tcp_clients, inputs):
        """Корректное закрытие TCP клиента"""

        addr = tcp_clients.get(sock)
        if addr:
            print(f"TCP клиент отключился: {addr}")

        if sock in inputs:
            inputs.remove(sock)

        if sock in tcp_clients:
            del tcp_clients[sock]

        try:
            sock.close()
        except:
            pass

    def _run_tcp_server(self):
        """Запуск TCP сервера (оригинальный код)"""
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_socket.bind((self.tcp_host, self.tcp_port))
        self.tcp_socket.listen(5)

        print(f"TCP сервер слушает порт {self.tcp_port}")

        while self.running:
            try:
                client_sock, client_addr = self.tcp_socket.accept()
                print(f"\nНовое TCP подключение от {client_addr}")

                client_thread = threading.Thread(
                    target=self.tcp_handler.handle_client,
                    args=(client_sock, client_addr),
                )
                client_thread.daemon = True
                client_thread.start()
            except Exception as e:
                if self.running:
                    print(f"Ошибка TCP сервера: {e}")

    def _run_udp_server(self):
        """Запуск UDP сервера"""
        self.udp_socket = create_udp_socket()
        self.udp_socket.bind((self.udp_host, self.udp_port))
        self.udp_socket.settimeout(1.0)

        print(f"UDP сервер слушает порт {self.udp_port}")

        while self.running:
            try:
                data, client_addr = self.udp_socket.recvfrom(65535)
                # Обрабатываем в отдельном потоке
                thread = threading.Thread(
                    target=self.udp_handler.handle_packet, args=(data, client_addr)
                )
                thread.daemon = True
                thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Ошибка UDP сервера: {e}")

    def stop(self):
        self.running = False

        if self.tcp_socket:
            self.tcp_socket.close()

        if self.udp_socket:
            self.udp_socket.close()

        print("Сервер остановлен")


def main():
    """Точка входа"""
    server = Server()

    try:
        server.start()
    except KeyboardInterrupt:
        print("\nЗавершение работы сервера...")


if __name__ == "__main__":
    main()

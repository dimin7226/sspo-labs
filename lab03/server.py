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

from app_config import *
from socket_handler import recv_until, recv_exact, send_all
from file_handler import (
    ensure_dirs,
    get_file_size,
    FileTransferStats,
)
from udp_handler import *
from sliding_window import ReceiveWindow


class ClientState:
    """Класс для хранения состояния TCP клиента отдельно от сокета"""

    def __init__(self, addr):
        self.addr = addr
        self.buffer = b""
        self.client_id = None  # Логическое имя клиента
        self.upload_state = None
        self.download_state = None

    @property
    def display_id(self):
        """Возвращает ID или адрес для логов"""
        if self.client_id:
            return self.client_id
        return f"{self.addr[0]}:{self.addr[1]}"


class TCPServerHandler:
    """Обработчик TCP подключений"""

    def __init__(self, server):
        self.server = server

    def process_command(self, client_sock, state, command):
        """Обработка одной TCP команды"""

        try:
            print(f"Команда от {state.display_id}: {command}")

            # --- HANDSHAKE (Регистрация клиента) ---
            if command.startswith("CLIENT "):
                requested_id = command.split(" ", 1)[1].strip()

                # Проверка на уникальность ID
                if requested_id in self.server.connected_ids:
                    print(f"Попытка входа с дублирующимся ID: {requested_id}")
                    send_all(client_sock, "ERROR: ID already taken\n")
                    return True  # Закрываем соединение

                # Регистрация успешна
                self.server.connected_ids.add(requested_id)
                state.client_id = requested_id
                print(f"Клиент зарегистрирован: {requested_id}")
                send_all(client_sock, "OK\n")
                return False

            elif command == "CLOSE":
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
                    self._handle_upload_nonblocking(state, filename, filesize)
                else:
                    send_all(client_sock, "ERROR: Неверный формат UPLOAD\n")

            elif command.startswith("DOWNLOAD "):
                filename = command[9:]
                self._handle_download_nonblocking(client_sock, state, filename)

            else:
                send_all(client_sock, "Неизвестная команда\n")

        except Exception as e:
            print(f"Error processing command: {e}")
            try:
                send_all(client_sock, f"ERROR: {e}\n")
            except:
                pass

        return False

    def _handle_download_nonblocking(self, client_sock, state, filename):
        """Инициализация неблокирующего скачивания файла"""
        filename = filename.strip()
        filepath = os.path.join(UPLOADS_DIR, filename)

        if not os.path.exists(filepath):
            send_all(client_sock, "ERROR: Файл не найден\n")
            return

        filesize = os.path.getsize(filepath)
        send_all(client_sock, f"FILESIZE {filesize}\n")

        try:
            # Для простоты в ЛР читаем offset блокирующе (можно улучшить)
            offset_str = recv_until(client_sock)
            offset = int(offset_str) if offset_str.isdigit() else 0
        except Exception:
            offset = 0

        try:
            f = open(filepath, "rb")
            if offset > 0:
                f.seek(offset)

            state.download_state = {
                "file": f,
                "filesize": filesize,
                "offset": offset,
                "sent": offset,
                "filename": filename,
            }
            print(f"Начато неблокирующее скачивание {filename}")

        except Exception as e:
            send_all(client_sock, f"ERROR: {e}\n")

    def _handle_upload_nonblocking(self, state, filename, filesize):
        """Инициализация неблокирующей загрузки"""
        filepath = os.path.join(UPLOADS_DIR, filename)

        # Если файл существует, добавляем таймстемп
        if os.path.exists(filepath):
            base, ext = os.path.splitext(filename)
            filepath = os.path.join(UPLOADS_DIR, f"{base}_{int(time.time())}{ext}")

        state.upload_state = {
            "filename": filename,
            "filepath": filepath,
            "filesize": filesize,
            "received": 0,
            "file": open(filepath, "wb"),
        }
        print(f"Начата неблокирующая загрузка {filename} ({filesize} байт)")


class UDPServerHandler:
    """Обработчик UDP подключений"""

    def __init__(self, server):
        self.server = server
        self.clients = {}  # addr -> session_data

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
                self._handle_command(client_addr, self.clients.get(client_addr), data)

        except Exception as e:
            print(f"Ошибка при завершении UDP сессии: {e}")

    def _handle_data(self, client_addr, packet_id, total_packets, flags, payload):
        """Обработка данных"""
        if client_addr not in self.clients:
            self.clients[client_addr] = {
                "client_id": "unknown",
                "connected": True,
                "time": time.time(),
                "recv_window": ReceiveWindow(),
                "file_session": {},
            }

        client_info = self.clients[client_addr]

        if packet_id == 0:
            self._handle_command(
                client_addr, client_info, payload.decode("utf-8", errors="ignore")
            )
        else:
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

        if os.path.exists(filepath):
            base, ext = os.path.splitext(filename)
            filepath = os.path.join(UPLOADS_DIR, f"{base}_udp{ext}")

        with open(filepath, "wb") as f:
            for packet_id in sorted(session["packets"].keys()):
                f.write(session["packets"][packet_id])

        duration = time.time() - session["start_time"]
        bitrate = (session["filesize"] * 8) / duration if duration > 0 else 0

        print(f"\n✓ UDP файл {os.path.basename(filepath)} загружен")
        self._send_response(client_addr, f"UPLOAD_OK {os.path.basename(filepath)}")
        client_info["file_session"] = {}

    def _handle_download(self, client_addr, filename):
        """Обработка UDP скачивания"""
        filename = filename.strip()
        filepath = os.path.join(UPLOADS_DIR, filename)

        if not os.path.exists(filepath):
            self._send_response(client_addr, "ERROR: Файл не найден")
            return

        filesize = os.path.getsize(filepath)
        print(f"UDP размер файла: {filesize} байт")
        self._send_response(client_addr, f"FILESIZE {filesize}")

        time.sleep(0.2)

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
                    time.sleep(0.002)

            print(f"\nUDP файл {filename} отправлен")
        except Exception as e:
            print(f"Ошибка при UDP отправке: {e}")

    def _send_response(self, client_addr, response_text):
        """Отправка UDP ответа"""
        try:
            data = response_text.encode("utf-8")
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

        self.connected_ids = set()

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

        inputs = [self.tcp_socket, self.udp_socket]

        # ВАЖНО: Используем ClientState вместо прямых атрибутов сокета
        # Ключ: объект сокета, Значение: объект ClientState
        tcp_clients = {}

        try:
            while self.running:
                # Select слушает сокеты на чтение
                readable, _, exceptional = select.select(inputs, [], inputs, 0.1)

                # Проталкиваем данные скачивания
                self.check_downloads(inputs, tcp_clients)

                for sock in readable:
                    # --- новое TCP подключение ---
                    if sock is self.tcp_socket:
                        client_sock, client_addr = self.tcp_socket.accept()
                        client_sock.setblocking(False)

                        # Создаем состояние для этого клиента
                        state = ClientState(client_addr)
                        tcp_clients[client_sock] = state

                        inputs.append(client_sock)
                        print(f"Новое TCP подключение: {client_addr}")

                    # --- UDP пакет ---
                    elif sock is self.udp_socket:
                        try:
                            data, addr = self.udp_socket.recvfrom(65535)
                            self.udp_handler.handle_packet(data, addr)
                        except Exception as e:
                            print(f"UDP Error: {e}")

                    # --- данные TCP клиента ---
                    else:
                        if sock in tcp_clients:
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
        state = tcp_clients[sock]  # Получаем состояние клиента

        try:
            try:
                data = sock.recv(BUFFER_SIZE)
            except BlockingIOError:
                return True

            if not data:
                self._close_tcp_client(sock, tcp_clients, inputs)
                return False

            # --- UPLOAD MODE ---
            if state.upload_state:
                upload = state.upload_state
                remaining = upload["filesize"] - upload["received"]

                chunk = data[:remaining]
                upload["file"].write(chunk)
                upload["received"] += len(chunk)

                extra = data[len(chunk) :]
                if extra:
                    state.buffer += extra

                if upload["received"] >= upload["filesize"]:
                    upload["file"].close()
                    send_all(sock, f"Файл {upload['filename']} успешно загружен\n")
                    print(f"\nUPLOAD завершён: {upload['filename']}")
                    state.upload_state = None

                return True

            # --- DOWNLOAD MODE ---
            # При скачивании клиент может прислать CLOSE или другие команды,
            # но пока просто буферизуем их.

            # --- COMMAND MODE ---
            state.buffer += data

            while b"\n" in state.buffer:
                line, state.buffer = state.buffer.split(b"\n", 1)
                try:
                    command = line.decode("utf-8").strip()
                    if not command:
                        continue

                    should_close = self.tcp_handler.process_command(
                        sock, state, command
                    )

                    if should_close:
                        self._close_tcp_client(sock, tcp_clients, inputs)
                        return False
                except UnicodeDecodeError:
                    print("Error decoding command")

            return True

        except Exception as e:
            print(f"TCP socket error: {e}")
            self._close_tcp_client(sock, tcp_clients, inputs)
            return False

    def check_downloads(self, inputs, tcp_clients):
        """Метод для проталкивания данных скачивания"""
        for sock in inputs:
            if sock in tcp_clients:
                state = tcp_clients[sock]
                if state.download_state:
                    try:
                        dl = state.download_state
                        remaining = dl["filesize"] - dl["sent"]
                        if remaining > 0:
                            chunk_size = min(BUFFER_SIZE, remaining)
                            data = dl["file"].read(chunk_size)
                            if data:
                                try:
                                    sock.send(data)
                                    dl["sent"] += len(data)
                                except BlockingIOError:
                                    # Откатываем позицию файла, если не удалось отправить
                                    dl["file"].seek(-len(data), 1)
                        else:
                            dl["file"].close()
                            print(f"DOWNLOAD завершён: {dl['filename']}")
                            state.download_state = None
                    except (BlockingIOError, socket.error):
                        pass

    def _close_tcp_client(self, sock, tcp_clients, inputs):
        """Корректное закрытие TCP клиента"""
        state = tcp_clients.get(sock)

        if state and state.client_id:
            if state.client_id in self.connected_ids:
                self.connected_ids.remove(state.client_id)
                print(f"Освобожден ID: {state.client_id}")

        if state:
            print(f"TCP клиент отключился: {state.addr}")

            # Закрываем файлы
            if state.upload_state:
                try:
                    state.upload_state["file"].close()
                except:
                    pass
            if state.download_state:
                try:
                    state.download_state["file"].close()
                except:
                    pass

        if sock in inputs:
            inputs.remove(sock)

        if sock in tcp_clients:
            del tcp_clients[sock]

        try:
            sock.close()
        except:
            pass

    def stop(self):
        self.running = False
        if self.tcp_socket:
            self.tcp_socket.close()
        if self.udp_socket:
            self.udp_socket.close()


def main():
    """Точка входа"""
    server = Server()

    try:
        server.start()
    except KeyboardInterrupt:
        print("\nЗавершение работы сервера...")


if __name__ == "__main__":
    main()

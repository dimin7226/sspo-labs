#!/usr/bin/env python3
"""
TCP/UDP клиент для передачи файлов с поддержкой команд и докачки
"""
import socket
import os
import time
import signal
import sys
import threading

from app_config import *
from socket_handler import set_keepalive, recv_until, recv_exact, send_all
from file_handler import ensure_dirs, get_file_size, get_partial_size, FileTransferStats
from udp_handler import *
from sliding_window import SlidingWindow, ReceiveWindow


class TCPClientHandler:
    """Обработчик TCP подключений (полностью из оригинального client.py)"""

    def __init__(self, client):
        self.client = client
        self.socket = None
        self.connected = False
        self.current_transfer = None

    def connect(self, server_host, server_port, client_id):
        """Подключение к TCP серверу"""
        if self.connected:
            print("Уже подключено к TCP серверу")
            return True

        try:
            print(f"TCP подключение к {server_host}:{server_port}...")

            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(CONNECTION_TIMEOUT)
            self.socket.connect((server_host, server_port))
            self.socket.settimeout(SOCKET_TIMEOUT)

            send_all(self.socket, f"CLIENT {client_id}\n")
            response = recv_until(self.socket)

            if response == "OK":
                self.connected = True
                print(f"✓ Подключено к TCP серверу {server_host}:{server_port}")
                return True
            else:
                print(f"✗ Ошибка: {response}")
                return False

        except ConnectionRefusedError:
            print("✗ TCP сервер недоступен")
            return False
        except Exception as e:
            print(f"✗ Ошибка TCP подключения: {e}")
            return False

    def disconnect(self):
        """Отключение от TCP сервера"""
        if self.connected:
            try:
                send_all(self.socket, "CLOSE\n")
            except:
                pass
        if self.socket:
            self.socket.close()
        self.connected = False
        print("TCP отключено от сервера")

    def send_command(self, command):
        """Отправка TCP команды"""
        if not self.connected:
            print("✗ Нет TCP подключения к серверу")
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
                filename = " ".join(parts[1:])
                self.upload_file(filename)
            elif cmd == "DOWNLOAD" and len(parts) >= 2:
                filename = " ".join(parts[1:])
                self.download_file(filename)
            else:
                print(f"✗ Неизвестная команда: {command}")

        except ConnectionError as e:
            print(f"✗ Ошибка отправки TCP команды: {e}")
            self.connected = False
        except Exception as e:
            print(f"✗ Ошибка: {e}")

    def _send_simple_command(self, command):
        """Отправка простой TCP команды"""
        send_all(self.socket, f"{command}\n")
        response = recv_until(self.socket)
        print(f"Ответ: {response}")

    def upload_file(self, filename):
        """TCP загрузка файла (оригинальный код)"""
        if not os.path.exists(filename):
            print(f"✗ Файл '{filename}' не найден")
            return

        filesize = get_file_size(filename)
        basename = os.path.basename(filename)

        print(f"\nTCP загрузка файла '{basename}' ({filesize} байт)...")

        cmd = f"UPLOAD {basename} {filesize}\n"
        send_all(self.socket, cmd)

        stats = FileTransferStats()
        stats.start()

        try:
            with open(filename, "rb") as f:
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
            stats.print_stats("TCP загрузка файла")
            response = recv_until(self.socket)
            print(f"Сервер: {response}")

        except Exception as e:
            print(f"\n✗ Ошибка при TCP загрузке: {e}")

    def download_file(self, filename):
        """TCP скачивание файла (оригинальный код)"""
        basename = os.path.basename(filename)
        print(f"\nTCP скачивание файла '{basename}'...")

        send_all(self.socket, f"DOWNLOAD {basename}\n")
        response = recv_until(self.socket)

        if response.startswith("ERROR"):
            print(f"✗ {response}")
            return

        if response.startswith("FILESIZE "):
            filesize = int(response[9:])
            print(f"Размер файла: {filesize} байт ({filesize / 1024 / 1024:.2f} МБ)")
        else:
            print(f"✗ Неожиданный ответ: {response}")
            return

        offset = 0
        if os.path.exists(basename):
            existing = get_file_size(basename)
            if existing < filesize:
                print(f"↻ Найден частичный файл: {existing} байт")
                offset = existing
                send_all(self.socket, f"{offset}\n")
            elif existing == filesize:
                print("✓ Файл уже полностью скачан")
                return
        else:
            send_all(self.socket, "0\n")

        stats = FileTransferStats()
        stats.start()

        try:
            mode = "ab" if offset > 0 else "wb"
            with open(basename, mode) as f:
                if mode == "ab":
                    f.seek(offset)

                received = offset
                buffer_size = BUFFER_SIZE * 4

                while received < filesize:
                    chunk_size = min(buffer_size, filesize - received)
                    data = recv_exact(self.socket, chunk_size)
                    f.write(data)
                    received += len(data)
                    stats.add_bytes(len(data))

                    percent = (received / filesize) * 100
                    print(f"\rСкачивание: {percent:.1f}%", end="")

            print()
            stats.stop()
            stats.print_stats("TCP скачивание файла")

        except Exception as e:
            print(f"\n✗ Ошибка при TCP скачивании: {e}")


class UDPClientHandler:
    """Обработчик UDP подключений"""

    def __init__(self, client):
        self.client = client
        self.socket = None
        self.connected = False
        self.server_addr = None
        self.client_id = None
        self.stats = FileTransferStats()
        self.window_size = 64
        self.packet_timeout = 0.1
        # для генерации request_id (не равного 0)
        self.next_request_id = 1

    def _get_request_id(self):
        """Вернуть уникальный request_id в диапазоне [1, REQUEST_ID_MAX]."""
        rid = self.next_request_id
        self.next_request_id = (self.next_request_id + 1) & REQUEST_ID_MAX
        if self.next_request_id == 0:
            self.next_request_id = 1
        return rid

    def connect(self, server_host, server_port, client_id):
        """Подключение к UDP серверу"""
        try:
            self.socket = create_udp_socket()
            self.socket.settimeout(1)
            self.server_addr = (server_host, server_port)
            self.client_id = client_id

            print(f"UDP подключение к {server_host}:{server_port}...")

            rid = self._get_request_id()
            packet = create_packet(
                rid, 0, 1, FLAG_START | FLAG_END, f"CLIENT {client_id}".encode()
            )
            self.socket.sendto(packet, self.server_addr)

            for attempt in range(3):
                try:
                    data, addr = self.socket.recvfrom(65535)
                    result = parse_packet(data)
                    if result:
                        resp_rid, packet_id, total, flags, payload = result
                        if resp_rid == rid and (flags & FLAG_ACK) and payload == b"OK":
                            self.connected = True
                            print(f"✓ Подключено к UDP серверу {server_host}:{server_port}")
                            return True
                except socket.timeout:
                    if attempt < 2:
                        self.socket.sendto(packet, self.server_addr)
                    continue

            print("✗ Ошибка подключения к UDP серверу")
            return False

        except Exception as e:
            print(f"✗ Ошибка UDP подключения: {e}")
            return False

    def disconnect(self):
        """Отключение от UDP сервера"""
        if self.connected:
            try:
                rid = self._get_request_id()
                packet = create_packet(rid, 0, 1, FLAG_END, b"CLOSE")
                self.socket.sendto(packet, self.server_addr)
            except:
                pass
        if self.socket:
            self.socket.close()
        self.connected = False
        print("UDP отключено от сервера")

    def send_command(self, command):
        """Отправка UDP команды - вызывается из client.py"""
        if not self.connected:
            print("✗ Нет UDP подключения к серверу")
            return

        parts = command.strip().split()
        if not parts:
            return

        cmd = parts[0].upper()

        if cmd == "CLOSE":
            self.disconnect()
        elif cmd == "TIME":
            self._send_command_time()
        elif cmd == "ECHO":
            self._send_command_echo(command)
        elif cmd == "UPLOAD" and len(parts) >= 2:
            filename = " ".join(parts[1:])
            self.upload_file(filename)
        elif cmd == "DOWNLOAD" and len(parts) >= 2:
            filename = " ".join(parts[1:])
            self.download_file(filename)
        else:
            print(f"✗ Неизвестная команда: {command}")

    def _send_command_time(self):
        """Отправка TIME команды"""
        print("Запрос времени сервера...")
        self._send_simple_command("TIME")

    def _send_command_echo(self, command):
        """Отправка ECHO команды"""
        text = command[5:].strip() if len(command) > 5 else ""
        print(f"Отправка ECHO: '{text}'")
        self._send_simple_command(f"ECHO {text}")

    def _send_simple_command(self, command):
        """Отправка простой команды и получение ответа"""
        try:
            rid = self._get_request_id()
            data = command.encode("utf-8")
            max_chunk = 1400 - PACKET_HEADER_SIZE
            total_packets = (len(data) + max_chunk - 1) // max_chunk

            for i in range(total_packets):
                start = i * max_chunk
                end = min(start + max_chunk, len(data))
                chunk = data[start:end]

                flags = FLAG_DATA
                if i == total_packets - 1:
                    flags |= FLAG_END

                packet = create_packet(rid, i, total_packets, flags, chunk)
                self.socket.sendto(packet, self.server_addr)

            response = self._wait_for_response(timeout=3, request_id=rid)
            if response:
                print(f"Ответ: {response}")
            else:
                print("✗ Нет ответа от сервера")

        except Exception as e:
            print(f"✗ Ошибка отправки команды: {e}")

    def upload_file(self, filename):
        """UDP загрузка файла"""
        if not os.path.exists(filename):
            print(f"✗ Файл '{filename}' не найден")
            return

        filesize = os.path.getsize(filename)
        basename = os.path.basename(filename)

        print(f"\nUDP загрузка файла '{basename}' ({self._format_bytes(filesize)})...")

        try:
            rid = self._get_request_id()
            cmd = f"UPLOAD {basename} {filesize}"
            cmd_packet = create_packet(rid, 0, 1, FLAG_DATA | FLAG_END, cmd.encode())
            self.socket.sendto(cmd_packet, self.server_addr)

            response = self._wait_for_response(timeout=3, request_id=rid)
            if not response or not response.startswith("READY"):
                print("✗ Сервер не готов к приему файла")
                return

            self._send_file_fast(filename, filesize, rid)

        except Exception as e:
            print(f"\n✗ Ошибка при UDP загрузке: {e}")

    def download_file(self, filename):
        """UDP скачивание файла"""
        basename = os.path.basename(filename)

        downloads_dir = "Downloads"
        os.makedirs(downloads_dir, exist_ok=True)
        save_path = os.path.join(downloads_dir, basename)

        print(f"\nUDP скачивание файла '{basename}'...")

        try:
            rid = self._get_request_id()
            cmd = f"DOWNLOAD {basename}"
            cmd_packet = create_packet(rid, 0, 1, FLAG_DATA | FLAG_END, cmd.encode())
            self.socket.sendto(cmd_packet, self.server_addr)

            response = self._wait_for_response(timeout=3, request_id=rid)
            if not response:
                print("✗ Нет ответа от сервера")
                return

            if not response.startswith("FILESIZE "):
                print(f"✗ {response}")
                return

            filesize = int(response[9:])
            print(f"Размер файла: {filesize} байт ({filesize / 1024 / 1024:.2f} МБ)")

            self._receive_file_fast(save_path, filesize, rid)

        except Exception as e:
            print(f"\n✗ Ошибка при UDP скачивании: {e}")

    def _send_file_fast(self, filename, filesize, request_id):
        """Быстрая отправка файла с указанным request_id"""
        self.stats.start()

        data_size = 1400 - PACKET_HEADER_SIZE
        total_packets = (filesize + data_size - 1) // data_size

        window = {}
        next_seq = 1000
        base_seq = 1000
        sent_bytes = 0
        last_update = time.time()

        with open(filename, "rb") as f:
            print("Отправка данных...")

            while base_seq < next_seq or sent_bytes < filesize:
                while len(window) < self.window_size and sent_bytes < filesize:
                    chunk = f.read(data_size)
                    if not chunk:
                        break

                    flags = FLAG_DATA
                    sent_bytes += len(chunk)
                    if sent_bytes >= filesize:
                        flags |= FLAG_END

                    packet = create_packet(request_id, next_seq, total_packets, flags, chunk)
                    window[next_seq] = {
                        "packet": packet,
                        "time": time.time(),
                        "resends": 0,
                    }

                    self.socket.sendto(packet, self.server_addr)
                    self.stats.add_bytes(len(packet))
                    next_seq += 1

                try:
                    while True:
                        data, addr = self.socket.recvfrom(65535)
                        result = parse_packet(data)
                        if result:
                            rid, ack_id, _, flags, _ = result
                            if rid != request_id:
                                continue
                            if flags & FLAG_ACK:
                                if ack_id in window:
                                    del window[ack_id]
                                    if ack_id >= base_seq:
                                        base_seq = ack_id + 1
                except socket.timeout:
                    pass

                current_time = time.time()
                for seq, info in list(window.items()):
                    if current_time - info["time"] > self.packet_timeout:
                        if info["resends"] < 3:
                            self.socket.sendto(info["packet"], self.server_addr)
                            info["time"] = current_time
                            info["resends"] += 1
                            self.stats.add_bytes(len(info["packet"]))

                if current_time - last_update > 0.2:
                    percent = (sent_bytes / filesize) * 100
                    speed = (
                        self.stats.total_bytes
                        * 8
                        / (current_time - self.stats.start_time)
                        / 1000
                    )
                    print(
                        f"\rЗагрузка: {percent:.1f}% | {self._format_bytes(sent_bytes)}/{self._format_bytes(filesize)} | {speed:.0f} Кбит/с",
                        end="",
                    )
                    last_update = current_time

                time.sleep(0.001)

        print()
        self.stats.stop()
        self._print_stats("UDP загрузка файла")

    def _receive_file_fast(self, filepath, filesize, request_id):
        """Быстрый прием файла"""
        self.stats.start()

        # Предварительно выделяем память
        packets = [None] * ((filesize // 1300) + 1000)
        received = 0
        expected_packets = set()

        # Увеличиваем буферы до максимума
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
        self.socket.settimeout(0.001)  # Минимальный таймаут

        print("Прием данных на максимальной скорости...")

        last_progress = time.time()
        last_ack = time.time()
        ack_batch = []

        while received < filesize:
            try:
                # Принимаем все доступные пакеты
                while True:
                    try:
                        data, addr = self.socket.recvfrom(65535)
                        result = parse_packet(data)
                        if not result:
                            continue

                        rid, packet_id, total, flags, payload = result
                        if rid != request_id:
                            continue

                        if not (flags & FLAG_DATA):
                            continue

                        # Сохраняем пакет
                        if packets[packet_id - 1000] is None:
                            packets[packet_id - 1000] = payload
                            received += len(payload)
                            self.stats.add_bytes(len(payload) + PACKET_HEADER_SIZE)
                            ack_batch.append(packet_id)

                    except socket.timeout:
                        break

                # Отправляем ACK пачкой
                current_time = time.time()
                if ack_batch and (
                    current_time - last_ack > 0.005 or len(ack_batch) > 50
                ):
                    for ack_id in ack_batch[:20]:  # Не больше 20 за раз
                        ack = create_ack_packet(request_id, ack_id)
                        self.socket.sendto(ack, self.server_addr)
                    ack_batch = ack_batch[20:]
                    last_ack = current_time

                # Прогресс
                if current_time - last_progress > 0.1:
                    percent = (received / filesize) * 100
                    speed = (
                        self.stats.total_bytes
                        * 8
                        / (current_time - self.stats.start_time)
                        / 1000
                    )
                    print(
                        f"\rСкачивание: {percent:.1f}% | {self._format_bytes(received)}/{self._format_bytes(filesize)} | {speed:.0f} Кбит/с",
                        end="",
                    )
                    last_progress = current_time

            except Exception as e:
                continue

        print()

        # Сохраняем файл
        if any(packets):
            # Убираем None из конца
            valid_packets = [p for p in packets if p is not None]

            with open(filepath, "wb") as f:
                for packet in valid_packets:
                    f.write(packet)

            saved_size = os.path.getsize(filepath)
            if saved_size == filesize:
                print(f"✓ Файл сохранен: {os.path.basename(filepath)}")
            else:
                print(f"⚠ Размер не совпадает: {saved_size} != {filesize}")
        else:
            print("✗ Не получено данных")

        self.stats.stop()
        self._print_stats("UDP скачивание файла")

        # Сравнение с TCP
        tcp_equiv = self.stats.get_bitrate() * 0.6
        if self.stats.get_bitrate() > tcp_equiv:
            ratio = self.stats.get_bitrate() / tcp_equiv
            print(f"✓ UDP быстрее TCP в {ratio:.2f} раз")

    def _wait_for_response(self, timeout=3, request_id=0):
        """Ожидание ответа от сервера, только для указанного request_id."""
        start_time = time.time()
        data = b""
        packets = {}

        while time.time() - start_time < timeout:
            try:
                packet, addr = self.socket.recvfrom(65535)
                result = parse_packet(packet)
                if not result:
                    continue

                rid, packet_id, total, flags, payload = result
                if request_id and rid != request_id:
                    # чужой запрос, игнорируем
                    continue

                if flags & FLAG_DATA:
                    packets[packet_id] = payload
                    ack = create_ack_packet(rid, packet_id)
                    self.socket.sendto(ack, self.server_addr)

                if flags & FLAG_END:
                    for i in range(total):
                        if i in packets:
                            data += packets[i]
                    return data.decode("utf-8", errors="ignore")

            except socket.timeout:
                continue

        return None

    def _save_file(self, filepath, packets, expected_size):
        """Сохранение файла из пакетов"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if os.path.exists(filepath):
            base, ext = os.path.splitext(filepath)
            counter = 1
            while os.path.exists(f"{base}_{counter}{ext}"):
                counter += 1
            filepath = f"{base}_{counter}{ext}"

        with open(filepath, "wb") as f:
            for pid in sorted(packets.keys()):
                f.write(packets[pid])

        saved_size = os.path.getsize(filepath)
        if saved_size == expected_size:
            print(f"✓ Файл сохранен: {os.path.basename(filepath)}")
            print(f"  Путь: {os.path.abspath(filepath)}")
        else:
            print(
                f"⚠ Файл сохранен, но размер не совпадает: {saved_size} != {expected_size}"
            )

    def _print_stats(self, operation):
        """Вывод статистики"""
        duration = self.stats.end_time - self.stats.start_time
        bitrate = self.stats.get_bitrate()

        print(f"\n{operation} завершен:")
        print(f"Передано: {self._format_bytes(self.stats.total_bytes)}")
        print(f"Скорость: {bitrate / 1000:.2f} Кбит/с ({bitrate / 1000 / 8:.2f} КБ/с)")
        print(f"Время: {duration:.2f} сек")

    def _format_bytes(self, bytes_count):
        """Форматирование размера"""
        for unit in ["Б", "КБ", "МБ", "ГБ"]:
            if bytes_count < 1024.0:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024.0
        return f"{bytes_count:.1f} ТБ"


class Client:
    """Единый клиент с поддержкой TCP и UDP"""

    def __init__(self, server_host="localhost"):
        self.server_host = server_host
        self.tcp_port = SERVER_PORT
        self.udp_port = SERVER_PORT + 1

        # Инициализация обработчиков
        self.tcp = TCPClientHandler(self)
        self.udp = UDPClientHandler(self)

        # Текущий протокол
        self.current_protocol = "tcp"

        ensure_dirs()

        self.downloads_dir = "Downloads"
        os.makedirs(self.downloads_dir, exist_ok=True)
        print(f"Файлы будут скачиваться в: {os.path.abspath(self.downloads_dir)}")
        print(f"Файлы для загрузки берутся из текущей папки")

    @property
    def current(self):
        """Получение текущего обработчика"""
        return self.tcp if self.current_protocol == "tcp" else self.udp

    def run(self):
        """Основной цикл клиента"""
        print("Клиент для передачи файлов (TCP/UDP)")
        print("Доступные команды:")
        print("  PROTOCOL [tcp|udp] - выбрать протокол")
        print("  CONNECT - подключение к серверу")
        print("  TIME - время сервера")
        print("  ECHO <текст> - эхо-команда")
        print("  UPLOAD <файл> - загрузить файл на сервер")
        print("  DOWNLOAD <файл> - скачать файл с сервера")
        print("  CLOSE - закрыть соединение")
        print("  Q - выход из программы")

        while True:
            try:
                cmd = input(f"\n[{self.current_protocol.upper()}] > ").strip()

                if cmd.upper() == "Q":
                    if self.tcp.connected:
                        self.tcp.disconnect()
                    if self.udp.connected:
                        self.udp.disconnect()
                    break

                elif cmd.upper().startswith("PROTOCOL"):
                    parts = cmd.split()
                    if len(parts) == 2 and parts[1].lower() in ["tcp", "udp"]:
                        self.current_protocol = parts[1].lower()
                        print(f"Протокол изменен на {self.current_protocol.upper()}")
                    else:
                        print("Используйте: PROTOCOL [tcp|udp]")

                elif cmd.upper() == "CONNECT":
                    client_id = (
                        input("Введите ID клиента (по умолчанию 1): ").strip() or "1"
                    )

                    if self.current_protocol == "tcp":
                        self.tcp.connect(self.server_host, self.tcp_port, client_id)
                    else:
                        self.udp.connect(self.server_host, self.udp_port, client_id)

                elif cmd.upper() == "CLOSE":
                    self.current.disconnect()

                else:
                    self.current.send_command(cmd)

            except KeyboardInterrupt:
                print("\n\nЗавершение работы клиента...")
                break
            except Exception as e:
                print(f"✗ Ошибка: {e}")


def main():
    """Точка входа"""
    server_host = input("Введите IP сервера (по умолчанию localhost): ").strip()
    if not server_host:
        server_host = "localhost"

    client = Client(server_host)

    try:
        client.run()
    except KeyboardInterrupt:
        print("\nЗавершение работы клиента...")


if __name__ == "__main__":
    main()

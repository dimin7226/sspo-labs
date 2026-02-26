"""
Модуль для работы с сокетами с учетом особенностей TCP
"""

import socket
from app_config import BUFFER_SIZE, IS_WINDOWS, SOCKET_TIMEOUT


def set_keepalive(sock, idle=30, interval=5, count=3):
    """Настройка TCP Keep-Alive для разных ОС"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        if IS_WINDOWS:
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
            except:
                pass
        else:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
            except:
                pass
    except Exception as e:
        print(f"Ошибка настройки keepalive: {e}")


def recv_until(sock, delimiter="\n"):
    """
    Получение ТЕКСТОВЫХ данных до разделителя
    Использовать ТОЛЬКО для команд!
    """
    if isinstance(delimiter, str):
        delimiter = delimiter.encode()

    data = b""
    while True:
        try:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("Соединение разорвано")

            data += chunk

            if data.endswith(delimiter) or data.endswith(b"\r\n"):
                break
        except socket.timeout:
            continue
        except BlockingIOError:
            continue

    return data.decode("utf-8").strip()


def recv_exact(sock, num_bytes):
    """Получение точного количества байт (для файлов)"""
    if num_bytes <= 0:
        return b""

    data = b""
    while len(data) < num_bytes:
        try:
            # Читаем не больше, чем нужно, и не больше размера буфера
            to_read = min(num_bytes - len(data), BUFFER_SIZE)
            chunk = sock.recv(to_read)
            if not chunk:
                raise ConnectionError("Соединение разорвано")
            data += chunk
        except socket.timeout:
            continue
        except BlockingIOError:
            # Если сокет неблокирующий и данных пока нет
            continue

    return data


def send_all(sock, data):
    """Гарантированная отправка всех данных"""
    if isinstance(data, str):
        data = data.encode("utf-8")

    total_sent = 0
    while total_sent < len(data):
        try:
            sent = sock.send(data[total_sent:])
            if sent == 0:
                raise ConnectionError("Соединение разорвано")
            total_sent += sent
        except socket.timeout:
            continue
        except BlockingIOError:
            continue
        except socket.error as e:
            raise ConnectionError(f"Ошибка сокета: {e}")


def create_socket():
    """Создание TCP сокета"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock

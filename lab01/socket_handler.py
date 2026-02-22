"""
Модуль для работы с сокетами с учетом особенностей TCP
"""
import socket
from app_config import BUFFER_SIZE, IS_WINDOWS, SOCKET_TIMEOUT, CMD_TERMINATOR


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
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, 4, idle)
                    sock.setsockopt(socket.IPPROTO_TCP, 5, interval)
                    sock.setsockopt(socket.IPPROTO_TCP, 6, count)
                except:
                    pass
    except Exception as e:
        print(f"Ошибка настройки keepalive: {e}")


def recv_exact(sock, num_bytes):
    """Получение точного количества байт из сокета"""
    if num_bytes <= 0:
        return b''

    data = b''
    while len(data) < num_bytes:
        try:
            chunk = sock.recv(min(num_bytes - len(data), BUFFER_SIZE))
            if not chunk:
                raise ConnectionError("Соединение разорвано")
            data += chunk
        except socket.timeout:
            continue
        except socket.error as e:
            raise ConnectionError(f"Ошибка сокета: {e}")

    return data


def recv_until(sock, delimiter=CMD_TERMINATOR):
    """Получение данных до указанного разделителя"""
    if isinstance(delimiter, str):
        delimiter = delimiter.encode()

    data = b''
    delimiter_len = len(delimiter)

    while True:
        try:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("Соединение разорвано")

            data += chunk

            if len(data) >= delimiter_len:
                if data[-delimiter_len:] == delimiter:
                    break
                if delimiter == b'\n' and data.endswith(b'\r\n'):
                    break
        except socket.timeout:
            continue
        except socket.error as e:
            raise ConnectionError(f"Ошибка сокета: {e}")

    if data.endswith(b'\r\n'):
        return data[:-2].decode('utf-8')
    else:
        return data[:-delimiter_len].decode('utf-8')


def send_all(sock, data):
    """Гарантированная отправка всех данных"""
    if isinstance(data, str):
        data = data.encode('utf-8')

    total_sent = 0
    while total_sent < len(data):
        try:
            sent = sock.send(data[total_sent:])
            if sent == 0:
                raise ConnectionError("Соединение разорвано")
            total_sent += sent
        except socket.timeout:
            continue
        except socket.error as e:
            raise ConnectionError(f"Ошибка сокета: {e}")


def create_socket():
    """Создание TCP сокета"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock
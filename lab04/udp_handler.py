"""
Модуль для работы с UDP сокетами

Расширен поддержкой идентификаторов запроса (request_id). Каждый
запрос обрабатывается в отдельном потоке, поток получает все пакеты с
одинаковым request_id через очередь.
"""
import socket
import struct
import time
import queue
import threading
from app_config import BUFFER_SIZE

# Заголовок пакета: magic(2) + request_id(2) + packet_id(4) + total_packets(4) + flags(1) + data_size(2)
#   unsigned short, unsigned short, unsigned int, unsigned int, unsigned char, unsigned short
PACKET_HEADER_FORMAT = '!HHIIBH'
PACKET_HEADER_SIZE = struct.calcsize(PACKET_HEADER_FORMAT)
MAGIC = 0xDEAD
# Максимальный номер запроса (2 байта)
REQUEST_ID_MAX = 0xFFFF

# Флаги пакета
FLAG_DATA = 0x01
FLAG_ACK = 0x02
FLAG_START = 0x04
FLAG_END = 0x08
FLAG_RESEND = 0x10

# Таймауты (сек)
ACK_TIMEOUT = 0.5
MAX_RESENDS = 5


def create_packet(request_id, packet_id, total_packets, flags, data=b''):
    """
    Создание пакета с заголовком и идентфикатором запроса.

    Формат: [magic][request_id][packet_id][total_packets][flags][data_size][data]
    Все числовые поля хранятся в сетевом порядке.
    """
    header = struct.pack(
        PACKET_HEADER_FORMAT,
        MAGIC,
        request_id,
        packet_id,
        total_packets,
        flags,
        len(data)
    )
    return header + data


def parse_packet(packet):
    """Разбор пакета и возврат (request_id, packet_id, total_packets, flags, data)"""
    if len(packet) < PACKET_HEADER_SIZE:
        print(f"Пакет слишком короткий: {len(packet)} < {PACKET_HEADER_SIZE}")
        return None

    try:
        header = packet[:PACKET_HEADER_SIZE]
        magic, request_id, packet_id, total_packets, flags, data_size = struct.unpack(
            PACKET_HEADER_FORMAT, header
        )

        if magic != MAGIC:
            print(f"Неверное магическое число: {magic} != {MAGIC}")
            return None

        data = packet[PACKET_HEADER_SIZE:PACKET_HEADER_SIZE + data_size]

        # Проверяем, что данные соответствуют заявленному размеру
        if len(data) != data_size:
            print(f"Размер данных не соответствует: {len(data)} != {data_size}")
            data = packet[PACKET_HEADER_SIZE:]

        return request_id, packet_id, total_packets, flags, data

    except Exception as e:
        print(f"Ошибка парсинга пакета: {e}")
        return None


def create_ack_packet(request_id, packet_id):
    """Создание ACK пакета с идентификатором запроса"""
    return create_packet(request_id, packet_id, 0, FLAG_ACK)


def create_udp_socket(reuse=False):
    """Создание UDP сокета.

    По умолчанию использует уникальный порт для каждого процесса. Установка
    ``reuse=True`` позволяет многим сокетам привязываться к одному порту, что
    полезно для серверов в режиме разработки, но может привести к неверной
    доставке пакетов, если несколько клиентов работают на одном хосте.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Увеличиваем буфер для UDP
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
    return sock


# --- сессии запроса --------------------------------------------------------
class RequestSession:
    """Очередь и поток обработки для одного UDP-запроса"""

    def __init__(self, request_id, client_addr, handler):
        self.request_id = request_id
        self.client_addr = client_addr
        self.handler = handler
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()

    def _run(self):
        while True:
            packet = self.queue.get()
            if packet is None:
                break
            parsed = parse_packet(packet)
            if not parsed:
                continue
            # делегируем обработку старому методу
            self.handler.handle_packet(packet, self.client_addr)
            _, packet_id, total, flags, data = parsed
            if flags & FLAG_END:
                break
        with self.handler._lock:
            if self.request_id in self.handler.requests:
                del self.handler.requests[self.request_id]


"""
Модуль для работы с UDP сокетами
"""
import socket
import struct
import time
from app_config import BUFFER_SIZE

# Заголовок пакета: magic(2) + packet_id(4) + total_packets(4) + flags(1) + data_size(2)
PACKET_HEADER_FORMAT = '!HIIBH'
PACKET_HEADER_SIZE = struct.calcsize(PACKET_HEADER_FORMAT)
MAGIC = 0xDEAD

# Флаги пакета
FLAG_DATA = 0x01
FLAG_ACK = 0x02
FLAG_START = 0x04
FLAG_END = 0x08
FLAG_RESEND = 0x10

# Таймауты (сек)
ACK_TIMEOUT = 0.5
MAX_RESENDS = 5


def create_packet(packet_id, total_packets, flags, data=b''):
    """
    Создание пакета с заголовком
    Формат: [magic(2)][packet_id(4)][total_packets(4)][flags(1)][data_size(2)][data]
    """
    header = struct.pack(
        PACKET_HEADER_FORMAT,
        MAGIC,
        packet_id,
        total_packets,
        flags,
        len(data)
    )
    return header + data


def parse_packet(packet):
    """Разбор пакета и возврат (packet_id, total_packets, flags, data)"""
    if len(packet) < PACKET_HEADER_SIZE:
        print(f"Пакет слишком короткий: {len(packet)} < {PACKET_HEADER_SIZE}")
        return None

    try:
        header = packet[:PACKET_HEADER_SIZE]
        magic, packet_id, total_packets, flags, data_size = struct.unpack(PACKET_HEADER_FORMAT, header)

        if magic != MAGIC:
            print(f"Неверное магическое число: {magic} != {MAGIC}")
            return None

        data = packet[PACKET_HEADER_SIZE:PACKET_HEADER_SIZE + data_size]

        # Проверяем, что данные соответствуют заявленному размеру
        if len(data) != data_size:
            print(f"Размер данных не соответствует: {len(data)} != {data_size}")
            # Пытаемся взять сколько есть
            data = packet[PACKET_HEADER_SIZE:]

        return packet_id, total_packets, flags, data

    except Exception as e:
        print(f"Ошибка парсинга пакета: {e}")
        return None


def create_ack_packet(packet_id):
    """Создание ACK пакета"""
    return create_packet(packet_id, 0, FLAG_ACK)


def create_udp_socket():
    """Создание UDP сокета"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Увеличиваем буфер для UDP
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
    return sock

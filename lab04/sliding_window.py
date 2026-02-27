"""
Модуль для реализации скользящего окна в UDP
"""
import time
import threading
from collections import OrderedDict
from udp_handler import create_ack_packet, MAX_RESENDS, ACK_TIMEOUT


class SlidingWindow:
    """Скользящее окно для надежной UDP передачи"""

    def __init__(self, window_size=10, timeout=ACK_TIMEOUT):
        self.window_size = window_size
        self.timeout = timeout
        self.base = 0  # Номер первого неподтвержденного пакета
        self.next_seq = 0  # Номер следующего пакета для отправки
        self.packets = {}  # Словарь для хранения неподтвержденных пакетов
        self.timers = {}  # Таймеры для повторной отправки
        self.lock = threading.Lock()
        self.closed = False

    def can_send(self):
        """Проверка, можно ли отправить новый пакет"""
        with self.lock:
            return self.next_seq < self.base + self.window_size

    def add_packet(self, seq_num, packet, callback=None):
        """Добавление пакета в окно"""
        with self.lock:
            self.packets[seq_num] = {
                'packet': packet,
                'time': time.time(),
                'resends': 0,
                'callback': callback
            }
            self._start_timer(seq_num)

    def ack_received(self, seq_num):
        """Обработка полученного подтверждения"""
        with self.lock:
            if seq_num in self.packets:
                # Останавливаем таймер
                self._stop_timer(seq_num)

                # Вызываем callback если есть
                if self.packets[seq_num].get('callback'):
                    self.packets[seq_num]['callback'](True)

                # Удаляем пакет
                del self.packets[seq_num]

                # Сдвигаем окно
                if seq_num >= self.base:
                    self.base = seq_num + 1
                    while self.base in self.packets:
                        self.base += 1

                return True
        return False

    def get_resend_packets(self):
        """Получение списка пакетов для повторной отправки"""
        with self.lock:
            resend_list = []
            current_time = time.time()

            for seq_num, info in list(self.packets.items()):
                if current_time - info['time'] > self.timeout:
                    if info['resends'] >= MAX_RESENDS:
                        # Превышено число попыток
                        if info.get('callback'):
                            info['callback'](False)
                        del self.packets[seq_num]
                    else:
                        # Увеличиваем счетчик и добавляем в список на переотправку
                        info['resends'] += 1
                        info['time'] = current_time
                        resend_list.append((seq_num, info['packet']))
                        self._start_timer(seq_num)

            return resend_list

    def _start_timer(self, seq_num):
        """Запуск таймера для пакета"""
        # В реальной реализации здесь можно использовать threading.Timer
        pass

    def _stop_timer(self, seq_num):
        """Остановка таймера для пакета"""
        if seq_num in self.timers:
            # Останавливаем таймер
            pass

    def close(self):
        """Закрытие окна"""
        with self.lock:
            self.closed = True
            for seq_num in list(self.packets.keys()):
                if self.packets[seq_num].get('callback'):
                    self.packets[seq_num]['callback'](False)
            self.packets.clear()


class ReceiveWindow:
    """Окно приема для упорядочивания пакетов"""

    def __init__(self, window_size=10):
        self.window_size = window_size
        self.expected_seq = 0
        self.buffer = OrderedDict()
        self.received_packets = set()  # Для предотвращения дубликатов

    def add_packet(self, seq_num, total_packets, data):
        """
        Добавление полученного пакета
        Возвращает (готово_ли_к_обработке, данные_пакета)
        """
        # Проверка на дубликат
        if seq_num in self.received_packets:
            return False, None

        # Если пакет ожидаемый
        if seq_num == self.expected_seq:
            self.expected_seq += 1
            self.received_packets.add(seq_num)

            # Проверяем буфер на наличие следующих пакетов
            result_data = [data]

            while self.expected_seq in self.buffer:
                result_data.append(self.buffer.pop(self.expected_seq))
                self.received_packets.add(self.expected_seq)
                self.expected_seq += 1

            return True, result_data if len(result_data) > 1 else data

        # Если пакет из будущего, сохраняем в буфер
        elif seq_num > self.expected_seq and seq_num < self.expected_seq + self.window_size:
            if seq_num not in self.buffer:
                self.buffer[seq_num] = data
            return False, None

        return False, None

    def get_missing_packets(self):
        """Получение списка пропущенных пакетов"""
        missing = []
        for i in range(self.expected_seq, self.expected_seq + self.window_size):
            if i not in self.received_packets and i not in self.buffer:
                missing.append(i)
        return missing

    def reset(self):
        """Сброс окна приема"""
        self.expected_seq = 0
        self.buffer.clear()
        self.received_packets.clear()
"""
Модуль для управления Keep-Alive и восстановлением соединения
"""
import socket
import time
import threading
from app_config import KEEPALIVE_IDLE, MAX_RECOVERY_TIME


class ConnectionMonitor:
    """Мониторинг соединения и восстановление при обрыве"""

    def __init__(self, sock, on_disconnect=None, on_reconnect=None):
        self.sock = sock
        self.on_disconnect = on_disconnect
        self.on_reconnect = on_reconnect
        self.connected = True
        self.last_activity = time.time()
        self.monitor_thread = None
        self.running = False
        self.keepalive_idle = KEEPALIVE_IDLE
        self.max_recovery = MAX_RECOVERY_TIME

    def start(self):
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()

    def stop(self):
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)

    def update_activity(self):
        self.last_activity = time.time()

    def _monitor_loop(self):
        recovery_start = None

        while self.running:
            try:
                if time.time() - self.last_activity > self.keepalive_idle:
                    try:
                        self.sock.send(b'\x00')
                        self.update_activity()
                    except:
                        if self.connected:
                            self.connected = False
                            recovery_start = time.time()
                            if self.on_disconnect:
                                self.on_disconnect("Соединение разорвано")

                if not self.connected:
                    if recovery_start is None:
                        recovery_start = time.time()

                    if time.time() - recovery_start > self.max_recovery:
                        if self.on_disconnect:
                            self.on_disconnect("Время восстановления истекло")
                        break

                    if self.on_reconnect and self.on_reconnect():
                        self.connected = True
                        recovery_start = None
                        self.update_activity()
                        print("Соединение восстановлено")

                time.sleep(1)
            except:
                time.sleep(5)
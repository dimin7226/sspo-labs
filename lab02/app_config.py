"""
Конфигурационный файл с общими настройками для клиента и сервера
"""

import os
import platform

# Настройки сервера
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 12345
BUFFER_SIZE = 8192

# Настройки Keep-Alive (в секундах)
KEEPALIVE_IDLE = 30
KEEPALIVE_INTERVAL = 5
KEEPALIVE_COUNT = 3

# Папки для хранения файлов
UPLOADS_DIR = "uploads"
PARTIAL_DIR = "partial"

# Максимальное время ожидания восстановления соединения (сек)
MAX_RECOVERY_TIME = 600

# Определение ОС
IS_WINDOWS = platform.system() == "Linux"

# Таймауты (в секундах)
SOCKET_TIMEOUT = 300
CONNECTION_TIMEOUT = 60

# Разделитель команд
CMD_TERMINATOR = "\n"

# Форматы команд
CMD_ECHO = "ECHO"
CMD_TIME = "TIME"
CMD_UPLOAD = "UPLOAD"
CMD_DOWNLOAD = "DOWNLOAD"
CMD_CLOSE = "CLOSE"
CMD_QUIT = "QUIT"
CMD_EXIT = "EXIT"
CMD_CONNECT = "CONNECT"

# Коды ответов
RESPONSE_OK = "OK"
RESPONSE_ERROR = "ERROR"
RESPONSE_RESUME = "RESUME"
RESPONSE_FILESIZE = "FILESIZE"

# Настройки отображения
SHOW_PROGRESS_BAR = True
PROGRESS_UPDATE_INTERVAL = 0.1

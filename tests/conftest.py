import logging
from logging.handlers import SocketHandler
from lib.logger import g_logger

logging.basicConfig(format='[%(name)s] [%(levelname)s] %(message)s')
g_logger.setLevel(logging.DEBUG)

# socket_handler = SocketHandler('127.0.0.1', 19996)
# g_logger.addHandler(socket_handler)

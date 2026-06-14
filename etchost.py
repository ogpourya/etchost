#!/usr/bin/env python3

import atexit
import base64
import fcntl
import ipaddress
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

TOOL_NAME = "etchost"
HOSTS_FILE = Path("/etc/hosts")
LOCK_FILE = Path("/tmp/etchost-hosts.lock")
USAGE = "etchost domain=ip [domain=ip ...] [--] command [args ...]"

_current_patch: "HostsPatch | None" = None
_child: "subprocess.Popen | None" = None
_received_signal: "int | None" = None


def _read_hosts(path: Path) -> str:
    if os.geteuid() == 0:
        return path.read_text()
    result = subprocess.run(
        ["sudo", "cat", str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _write_hosts(path: Path, content: str) -> None:
    if os.geteuid() == 0:
        atomic_write(path, content)
        return
    encoded = base64.b64encode(content.encode()).decode()
    subprocess.run(
        ["sudo", "sh", "-c",
         f"tmp=$(mktemp -p /etc .etchost.XXXXXX) && "
         f"printf '%s' '{encoded}' | base64 -d > \"$tmp\" && "
         f"chmod 644 \"$tmp\" && "
         f"mv \"$tmp\" \"{path}\""],
        check=True,
    )


def is_valid_hostname(name: str) -> bool:
    if not name or len(name) > 253:
        return False
    if name.endswith("."):
        name = name[:-1]
    if not name:
        return False
    for label in name.split("."):
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not re.fullmatch(r"[A-Za-z0-9-]+", label):
            return False
    return True


class HostsPatch:
    def __init__(self, hosts_file: Path, mappings: list[str]):
        self.hosts_file = hosts_file
        self.marker = f"{TOOL_NAME}:{os.getpid()}:{uuid.uuid4().hex}"
        self.lines = self._build_lines(mappings)
        self.applied = False
        self._had_trailing_newline = True

    def _build_lines(self, mappings: list[str]) -> list[str]:
        lines = [f"# BEGIN {self.marker}"]
        for item in mappings:
            if "=" not in item:
                raise ValueError(f"invalid mapping: {item!r}")
            domain, ip = item.split("=", 1)
            domain = domain.strip()
            ip = ip.strip()
            if not is_valid_hostname(domain):
                raise ValueError(f"invalid hostname: {domain!r}")
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise ValueError(f"invalid IP address: {ip!r}") from None
            lines.append(f"{ip}\t{domain}\t# {self.marker}")
        lines.append(f"# END {self.marker}")
        return lines

    def apply(self) -> None:
        content = _read_hosts(self.hosts_file)
        self._had_trailing_newline = content.endswith("\n") or content == ""
        body = content
        if body and not body.endswith("\n"):
            body += "\n"
        body += "\n".join(self.lines) + "\n"
        _write_hosts(self.hosts_file, body)
        self.applied = True

    def restore(self) -> None:
        if not self.applied:
            return
        try:
            content = _read_hosts(self.hosts_file)
            kept = [line for line in content.split("\n") if self.marker not in line]
            if kept and kept[-1] == "":
                kept = kept[:-1]
            restored = "\n".join(kept)
            if restored and self._had_trailing_newline:
                restored += "\n"
            _write_hosts(self.hosts_file, restored)
        finally:
            self.applied = False


def atomic_write(path: Path, content: str) -> None:
    directory = path.parent
    try:
        source_stat = os.stat(path)
    except FileNotFoundError:
        source_stat = None

    fd, temp_name = tempfile.mkstemp(dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if source_stat is not None:
            os.chmod(temp_name, stat.S_IMODE(source_stat.st_mode))
            try:
                os.chown(temp_name, source_stat.st_uid, source_stat.st_gid)
            except PermissionError:
                pass
        else:
            os.chmod(temp_name, 0o644)
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@contextmanager
def hosts_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def cleanup() -> None:
    global _current_patch
    if _current_patch is None:
        return
    patch = _current_patch
    _current_patch = None
    try:
        with hosts_lock():
            patch.restore()
    except Exception as error:
        print(f"{TOOL_NAME}: failed to restore {patch.hosts_file}: {error}", file=sys.stderr)


def handle_signal(sig: int, _frame) -> None:
    global _received_signal
    _received_signal = sig
    child = _child
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def split_items(argv: list[str]) -> tuple[list[str], list[str]]:
    mappings: list[str] = []
    for index, item in enumerate(argv):
        if item == "--":
            if not mappings:
                raise ValueError("missing domain=ip mapping before '--'")
            command = argv[index + 1:]
            if not command:
                raise ValueError("missing command after '--'")
            return mappings, command
        if "=" in item:
            mappings.append(item)
            continue
        if not mappings:
            raise ValueError("missing domain=ip mapping")
        return mappings, argv[index:]
    if not mappings:
        raise ValueError("missing domain=ip mapping")
    raise ValueError("missing command")


def main() -> int:
    global _current_patch, _child

    argv = sys.argv[1:]
    if not argv:
        print(f"{TOOL_NAME}: missing arguments\nusage: {USAGE}", file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help"):
        print(f"usage: {USAGE}")
        return 0

    try:
        try:
            mappings, command = split_items(argv)
        except ValueError as error:
            print(f"{TOOL_NAME}: {error}", file=sys.stderr)
            return 2

        try:
            patch = HostsPatch(HOSTS_FILE, mappings)
        except ValueError as error:
            print(f"{TOOL_NAME}: {error}", file=sys.stderr)
            return 2

        _current_patch = patch
        atexit.register(cleanup)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, handle_signal)

        with hosts_lock():
            patch.apply()

        if _received_signal is not None:
            return 128 + _received_signal

        try:
            _child = subprocess.Popen(command, start_new_session=True)
        except FileNotFoundError:
            print(f"{TOOL_NAME}: command not found: {command[0]}", file=sys.stderr)
            return 127
        except OSError as error:
            print(f"{TOOL_NAME}: failed to run command: {error}", file=sys.stderr)
            return 126

        if _received_signal is not None and _child.poll() is None:
            try:
                os.killpg(_child.pid, _received_signal)
            except (ProcessLookupError, PermissionError):
                pass

        return_code = _child.wait()

        if _received_signal is not None:
            return 128 + _received_signal
        if return_code < 0:
            return 128 + (-return_code)
        return return_code

    except Exception as error:
        print(f"{TOOL_NAME}: {error}", file=sys.stderr)
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

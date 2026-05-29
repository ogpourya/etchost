# etchost

Temporarily inject entries into `/etc/hosts` for the duration of a command. When the command exits, the entries are removed automatically.

No leftover state. No manual cleanup. Safe for concurrent use.

---

## How it works

`etchost` patches `/etc/hosts` before your command starts and restores the original file after it exits — even on crash, interrupt, or signal. It uses a file lock so multiple instances running in parallel won't clobber each other.

---

## Installation

Requires [uv](https://github.com/astral-sh/uv).

```sh
uv tool install https://github.com/ogpourya/etchost.git
```

> Must be run as root to modify `/etc/hosts`.

---

## Usage

```
etchost domain=ip [domain=ip ...] [--] command [args ...]
```

### Basic example

```sh
sudo etchost myapp.local=127.0.0.1 -- curl http://myapp.local
```

### Map multiple domains at once

```sh
sudo etchost api.local=127.0.0.1 db.local=127.0.0.2 -- ./start-dev.sh
```

### Override a production domain locally

```sh
sudo etchost api.example.com=192.168.1.50 -- python test_suite.py
```

### Use with an explicit `--` separator

Useful when the command itself takes arguments that look like `key=value`.

```sh
sudo etchost staging.internal=10.0.0.5 -- pytest tests/ -k integration
```

### Inspect what gets injected

```sh
sudo etchost debug.local=127.0.0.1 -- cat /etc/hosts
```

---

## Options

| Argument | Description |
|---|---|
| `domain=ip` | One or more hostname-to-IP mappings |
| `--` | Optional separator between mappings and command |
| `-h`, `--help` | Show usage |

---

## Notes

- Supports both IPv4 and IPv6 addresses
- Validates hostnames strictly (RFC-compliant)
- Uses atomic writes to avoid partial file states
- Cleans up on `SIGINT`, `SIGTERM`, `SIGHUP`, and normal exit
- Lock file lives at `/run/lock/etchost-hosts.lock`

---

## License

MIT

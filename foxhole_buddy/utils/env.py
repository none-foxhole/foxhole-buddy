import os
from pathlib import Path

def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or value.startswith("put_your_"):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None

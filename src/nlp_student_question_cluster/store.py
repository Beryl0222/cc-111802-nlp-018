import json
from pathlib import Path

from .board import default_state


class BoardStore:
    """归纳台状态的本地 JSON 持久化（原子替换写入）。"""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default_state()
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("归纳台状态格式无效")
        return value

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)

"""公共测试夹具。"""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# 1x1 透明 PNG
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

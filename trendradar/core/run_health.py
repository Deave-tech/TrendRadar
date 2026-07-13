# coding=utf-8
"""每次运行的结构化健康记录。"""

import json
from pathlib import Path
from typing import Dict, Union


def append_run_health(record: Dict, output_dir: Union[str, Path] = "output") -> Path:
    """追加一条 JSONL 健康记录并返回文件路径。"""
    health_path = Path(output_dir) / "meta" / "run-health.jsonl"
    health_path.parent.mkdir(parents=True, exist_ok=True)
    with health_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        f.write("\n")
    return health_path

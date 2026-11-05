"""Print the task ids of a run whose patch did not apply in grading (one per line)."""

import json
import sys
from pathlib import Path

for path in sorted(Path(sys.argv[1]).glob("*/bench_result.json")):
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("patch_applied") is False:
        print(result["task_id"])

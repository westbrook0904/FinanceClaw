"""检查 wheel 内的技能包可独立解析且与源码固定清单逐字节一致。"""

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from financeclaw.shared.releases.skills import builtin_skills


def main():
    """从临时解包目录启动干净解释器，避免 editable 安装掩盖缺失的包数据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    expected = sorted(
        release.ref.model_dump_json() for release, _ in builtin_skills().entries.values()
    )
    with tempfile.TemporaryDirectory(prefix="skills-wheel-") as temporary:
        with zipfile.ZipFile(args.wheel) as archive:
            archive.extractall(temporary)
        code = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import financeclaw.shared.skills
from financeclaw.shared.releases.skills import builtin_skills
assert Path(financeclaw.shared.skills.__file__).is_relative_to(sys.argv[1])
print(json.dumps(sorted(r.ref.model_dump_json() for r, _ in builtin_skills().entries.values())))
"""
        result = subprocess.run(
            [sys.executable, "-c", code, temporary],
            check=True,
            text=True,
            capture_output=True,
            cwd=temporary,
        )
        if json.loads(result.stdout) != expected:
            raise SystemExit("wheel skill releases differ from source")
    print("Wheel skill bytes, manifests and pinned references match source")


if __name__ == "__main__":
    main()

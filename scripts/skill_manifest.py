"""为已审阅的内置技能生成固定包 hash；发布前显式运行并审查差异。"""

import argparse
import json
from pathlib import Path

from financeclaw.shared.skills.packages import load_package


def main():
    """仅为仓库内固定目录生成清单，不接收远端路径或安装请求。"""
    root = Path(__file__).resolve().parents[1] / "financeclaw/shared/skills/builtin"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    previous = (
        json.loads((root / "index.json").read_text()) if (root / "index.json").exists() else []
    )
    versions = {item["skill_id"]: item["version"] for item in previous}
    entries = []
    for path in sorted(root.iterdir()):
        if path.is_dir():
            package = load_package(path)
            entries.append(
                {
                    "skill_id": package.name,
                    "version": versions.get(package.name, "1.0.0"),
                    "package_hash": package.package_hash,
                }
            )
    if args.check:
        if entries != previous:
            raise SystemExit("skill source bytes differ from the fixed release index")
        print("Skill release manifest matches source")
        return
    (root / "index.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

"""`generate_sbom` 模块提供工程脚本相关能力。"""

import argparse
import json
import tomllib
from pathlib import Path


def main() -> None:
    """解析命令行参数并执行对应的工程任务。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="uv.lock")
    parser.add_argument("--output", default="build/sbom.cdx.json")
    args = parser.parse_args()
    lock = tomllib.loads(Path(args.lock).read_text(encoding="utf-8"))
    components = []
    for package in sorted(lock["package"], key=lambda item: (item["name"], item["version"])):
        if package.get("source", {}).get("editable"):
            continue
        components.append(
            {
                "type": "library",
                "name": package["name"],
                "version": package["version"],
                "purl": f"pkg:pypi/{package['name']}@{package['version']}",
            }
        )
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "financeclaw"}},
        "components": components,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

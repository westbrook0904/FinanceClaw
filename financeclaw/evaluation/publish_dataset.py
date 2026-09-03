"""提供 publish dataset 评测与发布能力。"""

import argparse
from pathlib import Path

from langsmith import Client

from .regression import load_cases, publish_cases


def main() -> None:
    """解析命令行参数，执行 publish dataset 操作并输出结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="Immutable dataset name including version")
    parser.add_argument(
        "--source",
        default="evals/stage5-regression-v1.json",
        help="Version-controlled sanitized dataset",
    )
    args = parser.parse_args()
    dataset = publish_cases(
        Client(),
        dataset_name=args.name,
        cases=load_cases(Path(args.source)),
    )
    print(f"created LangSmith dataset {dataset.name} ({dataset.id})")


if __name__ == "__main__":
    main()

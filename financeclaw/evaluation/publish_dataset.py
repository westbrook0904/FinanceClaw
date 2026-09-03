"""把版本库内的脱敏回归集发布为带版本名的 LangSmith 回归数据集。

属于 evaluation 层的命令行入口：默认读取 ``evals/stage5-regression-v1.json``，
用 ``--name`` 指定包含版本号的不可变数据集名，在 LangSmith 创建数据集并灌入
全部用例，作为发布门禁（见 ``regression.RegressionGate``）的评测数据。运行
方式：``python -m financeclaw.evaluation.publish_dataset --name <数据集名>``。
"""

import argparse
from pathlib import Path

from langsmith import Client

from .regression import load_cases, publish_cases


def main() -> None:
    """解析命令行参数并把回归用例发布为 LangSmith 数据集。"""
    # 1. 解析命令行参数：--name 为含版本号的不可变数据集名，--source 为回归集文件。
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="Immutable dataset name including version")
    parser.add_argument(
        "--source",
        default="evals/stage5-regression-v1.json",
        help="Version-controlled sanitized dataset",
    )
    args = parser.parse_args()
    # 2. 用默认 LangSmith 客户端创建数据集并灌入全部回归用例。
    dataset = publish_cases(
        Client(),
        dataset_name=args.name,
        cases=load_cases(Path(args.source)),
    )
    # 3. 打印数据集名与 ID，便于后续评测流程引用。
    print(f"created LangSmith dataset {dataset.name} ({dataset.id})")


if __name__ == "__main__":
    main()

"""`test_package_architecture` 模块提供`stage5`相关能力。"""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "financeclaw"


def _imports(path: Path) -> set[str]:
    """处理 `当前操作`，并返回边界约定的结果。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _assert_no_dependency(package: str, forbidden: tuple[str, ...]) -> None:
    """处理 `no_dependency`，并返回边界约定的结果。"""
    violations: list[str] = []
    for path in (PACKAGE / package).rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} -> {imported}")
    assert not violations, "invalid package dependencies:\n" + "\n".join(violations)


def test_enterprise_dependency_direction_is_enforced() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 共享内核是最内层，不得依赖其他 FinanceClaw 包。
    _assert_no_dependency("kernel", ("financeclaw.",))
    # 业务模块不得调用 HTTP、应用用例或 Agent 运行时代码。
    _assert_no_dependency(
        "modules",
        (
            "financeclaw.interfaces",
            "financeclaw.application",
            "financeclaw.orchestration",
        ),
    )
    # 应用服务拥有出站 Port，因此不得导入具体基础设施适配器。
    _assert_no_dependency(
        "application",
        ("financeclaw.interfaces", "financeclaw.infrastructure"),
    )


def test_deprecated_package_roots_are_absent() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 deprecated，供后续步骤使用。
    deprecated = (
        "agents",
        "api",
        "artifacts",
        "audit",
        "contracts",
        "conversation",
        "delegation",
        "graphs",
        "memory",
        "models",
        "observability",
        "outbox",
        "security",
        "tools",
        "workflows",
    )
    # 继续执行前验证内部不变量。
    assert all(not (PACKAGE / name).exists() for name in deprecated)


def test_all_python_definitions_are_documented() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    missing: list[str] = []
    roots = (PACKAGE, ROOT / "scripts", ROOT / "tests")
    paths = [path for root in roots for path in root.rglob("*.py")]
    paths.append(ROOT / "main.py")
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions = [tree, *ast.walk(tree)]
        for node in definitions:
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            if ast.get_docstring(node, clean=False) is None:
                name = getattr(node, "name", "<module>")
                missing.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 1)} {name}")
    assert not missing, "missing docstrings:\n" + "\n".join(missing)

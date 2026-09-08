"""检查真实包依赖，包括相对导入、聚合导出与废弃入口。"""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "financeclaw"


def _imports(path: Path, package_root: Path = PACKAGE) -> set[str]:
    """将绝对与相对导入解析成模块全名，并检查 from-package 子模块导入。"""
    module = ".".join(path.relative_to(package_root.parent).with_suffix("").parts)
    parent = (
        module.removesuffix(".__init__")
        if path.name == "__init__.py"
        else module.rpartition(".")[0]
    )
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = (
                resolve_name("." * node.level + (node.module or ""), parent)
                if node.level
                else node.module or ""
            )
            names.add(imported)
            for alias in node.names:
                candidate = imported + "." + alias.name
                candidate_path = package_root.parent / candidate.replace(".", "/")
                if candidate_path.is_dir() or candidate_path.with_suffix(".py").is_file():
                    names.add(candidate)
    return names


def test_service_dependency_direction_is_enforced() -> None:
    """三包不得互相穿透，共享包不得回指服务；BFF 只依赖公开协调入口。"""
    allowed = {
        "kernel": {"kernel"},
        "shared": {"kernel", "shared"},
        "agent_server": {"kernel", "shared", "agent_server"},
        "coordination": {"kernel", "shared", "coordination"},
        "bff": {"kernel", "shared", "bff"},
    }
    violations = []
    for owner, dependencies in allowed.items():
        paths = list((PACKAGE / owner).rglob("*.py"))
        assert paths, f"missing required package: {owner}"
        for path in paths:
            for imported in _imports(path):
                if not imported.startswith("financeclaw."):
                    continue
                if imported.split(".")[1] in dependencies:
                    continue
                if owner == "bff" and imported == "financeclaw.coordination.api":
                    continue
                if (
                    path == PACKAGE / "bff/bootstrap.py"
                    and imported == "financeclaw.coordination.bootstrap"
                ):
                    continue
                violations.append(f"{path.relative_to(ROOT)} -> {imported}")
    assert not violations, "invalid package dependencies:\n" + "\n".join(violations)
    for path in (PACKAGE / "coordination/application").rglob("*.py"):
        assert "financeclaw.coordination.backends.langgraph" not in _imports(path)


def test_relative_and_aggregate_imports_cannot_hide_dependencies(tmp_path: Path) -> None:
    """防止把绝对导入改成相对导入或包导出后绕过依赖检查。"""
    package = tmp_path / "financeclaw"
    (package / "bff").mkdir(parents=True)
    (package / "agent_server").mkdir()
    path = package / "bff/__init__.py"
    path.write_text("from ..agent_server import agents\nfrom .. import agent_server\n")
    assert "financeclaw.agent_server" in _imports(path, package)


@pytest.mark.parametrize(
    "root",
    [
        "application",
        "modules",
        "orchestration",
        "infrastructure",
        "interfaces",
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
    ],
)
def test_deprecated_package_roots_are_absent(root: str) -> None:
    """废弃路径必须实际移除，避免兼容壳继续掩盖错误依赖。"""
    assert not (PACKAGE / root).exists()
    assert not (PACKAGE / "bootstrap.py").exists()


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

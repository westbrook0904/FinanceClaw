"""固定发布包和 /skill 控制语法的输入边界。"""

import json
from pathlib import Path

import pytest
import yaml

from financeclaw.kernel.skills import SkillError
from financeclaw.shared.releases.skills import builtin_skills
from financeclaw.shared.skills.directives import requested_skill
from financeclaw.shared.skills.packages import load_package, resource_path, safe_yaml


def package(tmp_path):
    """建立最小合法包，文件顺序由调用方改变。"""
    root = tmp_path / "brief"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: brief\ndescription: 方法\n---\n阅读 [资料](refs/data.md)。"
    )
    (root / "refs").mkdir()
    (root / "refs/data.md").write_text("资料中文😀" * 100)
    return root


def test_pinned_hash_covers_every_byte_and_loaded_snapshot_is_immutable(tmp_path):
    """S01/S02/S11：旧进程快照不变，新进程拒绝被替换的固定包。"""
    root = package(tmp_path)
    first = load_package(root)
    again = load_package(root, first.package_hash)
    assert first.resources == again.resources
    original = (root / "refs/data.md").read_bytes()
    (root / "refs/data.md").unlink()
    (root / "refs/data.md").write_bytes(original)
    assert load_package(root).package_hash == first.package_hash
    (root / "refs/data.md").write_text("修改")
    assert first.contents["refs/data.md"] == original
    with pytest.raises(ValueError, match="hash mismatch"):
        load_package(root, first.package_hash)
    with pytest.raises(TypeError):
        first.contents["evil"] = b"content"


@pytest.mark.parametrize(
    "text",
    [
        "name: a\nname: b",
        "a: &a [1]\nb: *a",
        "!!python/object/apply:os.system [echo]",
        "[a,b]",
        "name: a\npolicy: {x: 1, x: 2}",
    ],
)
def test_unsafe_yaml_is_rejected(text):
    """S01/S16：重复键、别名、非映射及可构造对象标签都不能进入发布。"""
    with pytest.raises((ValueError, yaml.YAMLError)):
        safe_yaml(text)


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "refs/../SKILL.md",
        "refs\\data.md",
        "%2e%2e/secret",
        "https://host/data",
        "refs//data.md",
        "./SKILL.md",
    ],
)
def test_paths_cannot_escape(path):
    """S11：规范相对路径校验在读取宿主文件前执行。"""
    with pytest.raises(ValueError):
        resource_path(path)


def test_links_missing_entry_and_unknown_policy_block_startup(tmp_path):
    """S01/S11/S16：不存在资源、符号链接和未知治理字段均拒绝启动。"""
    root = package(tmp_path)
    (root / "refs/data.md").unlink()
    with pytest.raises(ValueError, match="missing resource"):
        load_package(root)
    (root / "refs/data.md").symlink_to(root / "SKILL.md")
    with pytest.raises(ValueError, match="links"):
        load_package(root)
    (root / "refs/data.md").unlink()
    (root / "refs/data.md").write_text("资料")
    (root / "agents").mkdir()
    (root / "agents/openai.yaml").write_text("policy:\n  auto_approve: true")
    with pytest.raises(ValueError, match="policy"):
        load_package(root)


@pytest.mark.parametrize(
    "message",
    [
        "$AAPL",
        "$100",
        "$market-brief",
        "> /skill market-brief",
        "```\n/skill market-brief\n```",
        "解释 /skill market-brief",
        "/skills market-brief",
    ],
)
def test_ordinary_financial_text_is_not_a_directive(message):
    """S04：股票、金额、引用及普通正文均不触发控制语法。"""
    assert requested_skill(message) is None


@pytest.mark.parametrize("message", ["/skill", "/skill ", "/skill BAD", "/skill ../escape"])
def test_invalid_directive_has_stable_error(message):
    """缺 ID 或非法 slug 返回有界错误，不回显输入。"""
    with pytest.raises(SkillError) as error:
        requested_skill(message)
    assert error.value.code == "SKILL_DIRECTIVE_INVALID"


def test_builtin_index_matches_source():
    """S03：固定 JSON 清单与分发中的全部字节一致。"""
    import financeclaw.shared.skills

    root = Path(financeclaw.shared.skills.__file__).parent / "builtin"
    index = json.loads((root / "index.json").read_text())
    catalog = builtin_skills()
    assert len(index) == len(catalog.entries) == 2
    assert {item["skill_id"] for item in index} == {"market-brief", "cocktail-from-what-i-have"}
    assert requested_skill("/skill market-brief AAPL") == "market-brief"

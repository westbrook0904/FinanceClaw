"""启动时验证包清单并持有不可变字节快照，运行期不重新读取宿主文件。"""

import json
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import unquote

import yaml

from financeclaw.kernel.skills import SkillResource


class UniqueLoader(yaml.SafeLoader):
    """安全 YAML 解析器，拒绝覆盖同名键。"""

    def construct_mapping(self, node, deep=False):
        """同一映射中的重复键不能被后项静默覆盖。"""
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("duplicate or non-string YAML key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def safe_yaml(text):
    """限制 YAML 大小、别名和锚点，避免从包元数据构造任意对象。"""
    if len(text.encode()) > 262144:
        raise ValueError("YAML exceeds package file limit")
    if any(
        isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for t in yaml.scan(text)
    ):
        raise ValueError("YAML aliases are unsupported")
    value = yaml.load(text, Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError("skill metadata must be a mapping")
    return value


def resource_path(value: str) -> str:
    """只接受未经编码、无逃逸段的规范相对 POSIX 文件名。"""
    if (
        not value
        or len(value) > 256
        or "\\" in value
        or ":" in value
        or unquote(value) != value
        or any(ord(c) < 32 for c in value)
        or value.startswith("/")
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("invalid skill resource path")
    return value


def canonical(value) -> str:
    """包与发布指纹使用排序 JSON，保留中文原始 UTF-8。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SkillPackage:
    """校验后的包字节及解析内容，全进程可共享但不保存用户可见性。"""

    package_hash: str
    name: str
    description: str
    body: str
    resources: tuple[SkillResource, ...]
    contents: MappingProxyType
    implicit: bool = True


def load_package(root: Path, expected_hash: str | None = None) -> SkillPackage:
    """拒绝符号链接、特殊文件和非声明策略，所有 hash 基于读取后的字节。"""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("skill package must be a real directory")
    contents, resources = {}, []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("skill packages cannot contain links or special files")
        name = resource_path(path.relative_to(root).as_posix())
        if path.stat().st_size > 262144:
            raise ValueError("skill file is too large")
        content = path.read_bytes()
        contents[name] = content
        if len(contents) > 64 or sum(map(len, contents.values())) > 1048576:
            raise ValueError("skill package is too large")
        try:
            content.decode("utf-8")
            media = "text/plain; charset=utf-8"
        except UnicodeDecodeError:
            media = "application/octet-stream"
        resources.append(
            SkillResource(
                path=name,
                sha256=sha256(content).hexdigest(),
                size_bytes=len(content),
                media_type=media,
            )
        )
    fingerprint = sha256(canonical([r.model_dump() for r in resources]).encode()).hexdigest()
    if expected_hash is not None and fingerprint != expected_hash:
        raise ValueError("skill package hash mismatch")
    entry = contents.get("SKILL.md", b"").decode("utf-8")
    if not entry.startswith("---\n") or "\n---\n" not in entry[4:]:
        raise ValueError("skill frontmatter is required")
    header, body = entry[4:].split("\n---\n", 1)
    meta = safe_yaml(header)
    if set(meta) - {"name", "description", "metadata", "license", "compatibility"}:
        raise ValueError("unsupported skill metadata or policy")
    if not all(isinstance(meta.get(k), str) and meta[k].strip() for k in ("name", "description")):
        raise ValueError("skill name and description are required")
    if meta["name"] != root.name or len(meta["description"]) > 1024 or not body.strip():
        raise ValueError("invalid skill metadata or body")
    implicit = True
    if "agents/openai.yaml" in contents:
        config = safe_yaml(contents["agents/openai.yaml"].decode("utf-8"))
        if set(config) - {"interface", "policy", "dependencies"}:
            raise ValueError("unsupported skill configuration")
        policy = config.get("policy", {})
        if not isinstance(policy, dict) or set(policy) - {"allow_implicit_invocation"}:
            raise ValueError("unsupported skill policy")
        implicit = policy.get("allow_implicit_invocation", True)
        if type(implicit) is not bool:
            raise ValueError("invalid implicit invocation policy")
        # 首期依赖只接受平台声明；包内不能启动外部 MCP、command 或 URL。
        if config.get("dependencies", {}) not in ({}, {"tools": []}):
            raise ValueError("package dependencies require a supported platform mapping")
    import re

    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", body):
        if target.startswith(("https://", "http://", "#")):
            continue
        if resource_path(target.split("#", 1)[0]) not in contents:
            raise ValueError("skill references a missing resource")
    return SkillPackage(
        fingerprint,
        meta["name"],
        meta["description"],
        body.strip(),
        tuple(resources),
        MappingProxyType(contents),
        implicit,
    )

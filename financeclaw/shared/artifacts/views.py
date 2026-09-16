"""归档中的业务视图：有界目录、JSON 字段与记录读取，不执行查询脚本。"""

import json
import re
from copy import deepcopy
from typing import Any

VIEW_VERSION = 2
REFERENCE_BYTES = 4096
READ_BYTES = 16384
VIEW_KEY = "financeclaw_result_view"
READ_KEY = "financeclaw_artifact_read"


def encode(value: Any) -> str:
    """统一测量最终 JSON 正文，包含字段和转义的实际字节。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def pointer_parts(path: str) -> list[str]:
    """解析单一 JSON Pointer，拒绝不完整转义和非绝对路径。"""
    if not path:
        return []
    if not path.startswith("/") or len(path) > 1024 or re.search(r"~(?![01])", path):
        raise ValueError("invalid JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def at(value: Any, path: str) -> Any:
    """定位实际存在的值，缺失与 null 不混同。"""
    for part in pointer_parts(path):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(value):
                raise ValueError("JSON pointer not found")
            value = value[index]
        else:
            raise ValueError("JSON pointer not found")
    return value


def _path(key: str) -> str:
    """把对象键编码成一个 JSON Pointer 段。"""
    return "/" + key.replace("~", "~0").replace("/", "~1")


def _type(value: Any) -> str:
    """描述 JSON 类型，不把 bool 当作数值。"""
    if value is None:
        return "null"
    return {dict: "object", list: "array", str: "string", bool: "boolean"}.get(
        type(value), "number"
    )


def _decode(value: Any) -> tuple[str, Any, bool]:
    """文本只尝试一次完整 JSON 解码，其他内容保持文本。"""
    if isinstance(value, str):
        try:
            return "json", json.loads(value), True
        except (ValueError, RecursionError):
            return "text", value, False
    return "json", value, False


def mcp_view(artifact: dict) -> tuple[str, Any, str, bool]:
    """选择 MCP 的唯一业务入口，同时保留原始回包中的所有内容。"""
    response = artifact["response"]
    structured = response.get("structuredContent")
    if structured is not None:
        return "json", structured, "/artifact/response/structuredContent", False
    blocks = response.get("content", [])
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        kind, data, decoded = _decode(blocks[0]["text"])
        return kind, data, "/artifact/response/content/0/text", decoded
    texts = [block["text"] for block in blocks if block.get("type") == "text"]
    if texts:
        return "text", "\n".join(texts), "/content", False
    return "binary", blocks, "/artifact/response/content", False


def archive_payload(content, artifact, *, name, status, mcp=False) -> dict:
    """生成版本化文件；MCP 成功回包不再重复保存模型正文。"""
    raw = {"artifact": artifact, "name": name, "status": status}
    if mcp and status == "success" and isinstance(artifact, dict) and "response" in artifact:
        kind, data, path, decoded = mcp_view(artifact)
        if path == "/content":
            raw["content"] = data
    else:
        raw["content"] = content
        kind, _, decoded = _decode(content)
        path = "/content"
    return {
        "archive_version": VIEW_VERSION,
        "raw": raw,
        "view": {"format": kind, "path": path, "decode_json": decoded},
    }


def business_view(payload: Any) -> tuple[str, Any]:
    """读取新版或已有归档的业务根，历史文件字节和 hash 保持不变。"""
    if isinstance(payload, dict) and payload.get("archive_version") == VIEW_VERSION:
        view = payload["view"]
        data = at(payload["raw"], view["path"])
        return view["format"], json.loads(data) if view["decode_json"] else data
    if isinstance(payload, dict) and {"content", "artifact", "name", "status"} <= payload.keys():
        artifact = payload["artifact"]
        if (
            payload["status"] == "success"
            and isinstance(artifact, dict)
            and {"mcp", "response"} <= artifact.keys()
        ):
            kind, data, _, _ = mcp_view(artifact)
            return kind, data
        kind, data, _ = _decode(payload["content"])
        return kind, data
    kind, data, _ = _decode(payload)
    return kind, data


def describe(data: Any, *, path: str = "") -> dict:
    """列出有限结构，数组字段只采样首条并显式说明。"""
    result = {"type": _type(data), "fields": [], "collections": [], "directory_complete": True}

    def visit(value, path, depth):
        """最多展开两层对象和十六个数组入口，不展开数组全部记录。"""
        if isinstance(value, list):
            if len(result["collections"]) >= 16:
                result["directory_complete"] = False
                return
            first = value[0] if value else None
            result["collections"].append(
                {
                    "path": path,
                    "total_records": len(value),
                    "fields": [_path(k) for k in list(first)[:24]]
                    if isinstance(first, dict)
                    else [],
                    "fields_sampled": bool(value),
                }
            )
        elif isinstance(value, dict):
            if depth >= 2:
                result["directory_complete"] = False
                return
            if len(value) > 24:
                result["directory_complete"] = False
            for key in list(value)[:24]:
                item = value[key]
                pointer = path + _path(key)
                if depth == 0:
                    result["fields"].append(
                        {"path": pointer, "field": _path(key), "type": _type(item)}
                    )
                if isinstance(item, (dict, list)):
                    visit(item, pointer, depth + 1)

    visit(data, path, 0)
    return result


def select_fields(value: Any, fields: list[str] | tuple[str, ...]) -> tuple[Any, list[str]]:
    """保留对象嵌套结构；数组子路径应另定位后分页，避免伪造稀疏记录。"""
    if not fields:
        return value, []
    if not isinstance(value, dict):
        raise ValueError("fields require object records; inspect the selected path")
    selected, missing = {}, []
    for field in fields:
        parts = pointer_parts(field)
        if not parts:
            raise ValueError("select named fields instead of the whole record")
        source, target = value, selected
        for i, key in enumerate(parts):
            if not isinstance(source, dict):
                raise ValueError("nested arrays require their own path and record page")
            if key not in source:
                missing.append(field)
                break
            source = source[key]
            if i == len(parts) - 1:
                target[key] = deepcopy(source)
            else:
                # 父字段已选时保留整个父值；不会修改源数据。
                if key not in target:
                    target[key] = {}
                if not isinstance(target[key], dict):
                    raise ValueError("overlapping field selection requires object paths")
                target = target[key]
    return selected, missing


def _trim_directory(result: dict, budget: int) -> dict:
    """先减少可选目录，始终保留来源与下一步所需字段。"""
    while len(encode(result).encode()) > budget:
        for key in ("preview", "collections", "fields"):
            if result.get(key):
                result[key].pop()
                result["directory_complete"] = False
                result["preview_complete"] = False
                break
        else:
            raise ValueError("artifact reference exceeds response budget")
    return result


def reference_view(base: dict, kind: str, data: Any, rule: dict | None = None) -> dict:
    """文件引用附带小目录和预览；错误预览配置回退通用目录。"""
    rule = rule or {}
    result = {**base, "format": kind, "read_with": "read_artifact", **describe(data)}
    result.update(preview=[], preview_complete=False)
    if kind == "json":
        path = rule.get("collection_path")
        if path is None and result["collections"]:
            path = result["collections"][0]["path"]
        if path is not None:
            try:
                rows = at(data, path)
                if not isinstance(rows, list):
                    raise ValueError("preview path is not an array")
                result["preview_path"] = path
                for row in rows[: rule.get("preview_records", 3)]:
                    fields = rule.get("preview_fields", ())
                    if not fields and isinstance(row, dict):
                        fields = [
                            _path(k) for k in list(row)[:5] if not isinstance(row[k], (dict, list))
                        ]
                    item, missing = select_fields(row, fields)
                    candidate = {"data": item, "missing_fields": missing}
                    if len(encode(candidate).encode()) <= 1024:
                        result["preview"].append(candidate)
                result["preview_complete"] = len(result["preview"]) == len(rows)
            except ValueError:
                result["preview_unavailable"] = True
    return _trim_directory(result, REFERENCE_BYTES - 64)


def read_view(
    kind,
    data,
    base,
    *,
    mode,
    path="",
    fields=(),
    start=0,
    limit=20,
    offset=0,
    max_chars=4000,
    max_bytes=READ_BYTES,
):
    """返回字节受限的完整记录或文本；游标只跨过实际返回的数据。"""
    if mode not in {"inspect", "json", "text"}:
        raise ValueError("unsupported artifact read mode")
    if kind == "binary":
        if mode != "inspect":
            raise ValueError("artifact has no JSON or text business view")
        return {**base, "format": kind, "mode": mode, "readable": False}
    if not 1 <= limit <= 200 or start < 0 or offset < 0 or not 1 <= max_chars <= 8000:
        raise ValueError("invalid artifact page")
    if len(fields) > 24:
        raise ValueError("select at most 24 fields")
    if kind == "text" and (path or fields or mode == "json"):
        raise ValueError("text artifact requires mode=text without JSON fields")
    if kind != "text" and mode == "text":
        raise ValueError("JSON artifact requires mode=json or inspect")
    selected = at(data, path) if kind == "json" else data
    result = {**base, "format": kind, "mode": mode, "path": path}
    if mode == "inspect":
        return _trim_directory({**result, **describe(selected, path=path)}, max_bytes)
    if mode == "text":
        count = min(max_chars, max(0, len(selected) - offset))
        result.update(content="", next_offset=None)
        low, high = 0, count
        while low < high:
            mid = (low + high + 1) // 2
            candidate = {
                **result,
                "content": selected[offset : offset + mid],
                "next_offset": offset + mid if offset + mid < len(selected) else None,
            }
            if len(encode(candidate).encode()) <= max_bytes:
                low = mid
            else:
                high = mid - 1
        if count and not low:
            raise ValueError("text page metadata exceeds response budget")
        result.update(
            content=selected[offset : offset + low],
            next_offset=offset + low if offset + low < len(selected) else None,
        )
        return result
    result["selected_fields"] = list(fields)
    if not isinstance(selected, list):
        # 分页参数只对数组生效；模型读单个对象时携带 limit=1 不应导致失败。
        value, missing = select_fields(selected, fields)
        candidate = {**result, "data": value, "missing_fields": missing}
        if len(encode(candidate).encode()) <= max_bytes:
            return candidate
        return _trim_directory(
            {**result, "status": "needs_narrower_selection", **describe(selected, path=path)},
            max_bytes,
        )
    result.update(
        total_records=len(selected), start=start, returned_records=0, records=[], next_start=None
    )
    if len(encode(result).encode()) > max_bytes:
        raise ValueError("page metadata exceeds response budget; select fewer or shorter fields")
    for index in range(start, min(start + limit, len(selected))):
        value, missing = select_fields(selected[index], fields)
        record = {"index": index, "data": value, "missing_fields": missing}
        candidate = {
            **result,
            "records": [*result["records"], record],
            "returned_records": len(result["records"]) + 1,
            "next_start": index + 1 if index + 1 < len(selected) else None,
        }
        if len(encode(candidate).encode()) > max_bytes:
            if not result["records"]:
                return _trim_directory(
                    {
                        **result,
                        "status": "needs_narrower_selection",
                        **describe(selected[index], path=path + f"/{index}"),
                    },
                    max_bytes,
                )
            break
        result = candidate
    return result

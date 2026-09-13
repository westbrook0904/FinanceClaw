"""飞书实际拒绝过的表单约束、处理中视图和最终 Markdown 交付回归。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from financeclaw.api.application.feishu_card_actions import button_values
from financeclaw.integrations.notifications.feishu import CardCreationRejected, Receipt
from financeclaw.integrations.notifications.rendering import answer_cards
from financeclaw.integrations.notifications.repository import NotificationRepository
from financeclaw.integrations.notifications.worker import deliver
from financeclaw.shared.channels.feishu.cards import interaction_elements, render_card
from financeclaw.shared.notifications.tables import NotificationDeliveryRow as Delivery
from financeclaw.shared.releases.interactions import ROOT_CLARIFICATION
from tests.stage8.test_notifications import Gateway
from tests.stage8_hotfix.test_feishu_cards import callback, fresh, publish
from tests.stage8_hotfix.test_feishu_clarification import ask, waiting
from tests.stage10.runtime import final_state, tick
from tests.stage10.runtime import runtime as runtime


def nodes(value):
    """遍历实际生成的卡片组件，核对跨容器全局唯一名称及输入框约束。"""
    if isinstance(value, dict):
        if "tag" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def assert_form_contract(card):
    """固定真实接口暴露的限制，避免只验证本地回调却漏掉平台拒绝。"""
    components = list(nodes(card))
    names = [node["name"] for node in components if "name" in node]
    assert len(names) == len(set(names)), "飞书拒绝全局重名组件"
    for node in components:
        if node["tag"] == "input":
            assert 1 <= node["max_length"] <= 1000
        if node["tag"] == "button":
            assert "action_type" not in node
            assert node["behaviors"][0]["type"] == "callback"
        if node["tag"] == "form":
            buttons = [item for item in nodes(node["elements"]) if item["tag"] == "button"]
            assert buttons and all(item["form_action_type"] == "submit" for item in buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
async def test_pending_card_passes_platform_limits_and_resumes_exact_task(runtime, approval):
    """真实受理、中断、卡片投递、点击、恢复闭环不能生成飞书拒绝的表单。"""
    service, _, accepted, _ = await waiting(runtime, approval=approval)
    event, _ = await publish(runtime)
    card = render_card(event.event_id, event.payload)
    assert_form_contract(card)
    assert not any(node.get("icon", {}).get("tag") == "custom_icon" for node in nodes(card))
    action = callback(
        event, "approve" if approval else "answer", form={} if approval else {"f0": "当地钟表时间"}
    )
    assert (await service.card_actions.handle(action))["toast"]["type"] == "info"
    await tick(runtime)
    assert len(runtime.client.calls) == 2
    assert runtime.client.calls[-1]["thread_id"] == accepted.thread_id


@pytest.mark.parametrize("status", ["accepted", "queued", "running", "resuming", "cancelling"])
def test_processing_animation_and_controls_are_separate(status):
    """加载动图不依赖消息轮询，默认关闭的任务选项仅显示停止按钮。"""
    card = render_card(
        "view",
        {
            "status": status,
            "grant": {
                "revoked": False,
                "scopes": ["tools:read"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        },
    )
    assert "header" not in card
    elements = card["body"]["elements"]
    assert elements[0]["icon"]["tag"] == "custom_icon"
    assert elements[0]["icon"]["img_key"]
    assert not any(node["tag"] == "button" for node in elements)
    if status != "cancelling":
        panel = next(node for node in elements if node["tag"] == "collapsible_panel")
        assert panel["expanded"] is False
        assert {value["op"] for value in button_values(panel)} == {"cancel"}
        assert len(panel["elements"]) == 1
        assert "授权范围" not in json.dumps(card, ensure_ascii=False)


def test_card_limit_does_not_shrink_business_answers_and_unfillable_forms_fall_back():
    """卡片限制不修改业务 Schema；必须输入超长文本的字段保留命令入口。"""
    from financeclaw.shared.channels.feishu.cards import UnsupportedForm

    item = {
        "status": "pending",
        "kind": "input",
        "question": "合成问题",
        "response_schema": ROOT_CLARIFICATION.response_schema,
    }
    assert_form_contract(interaction_elements("view", item))
    assert item["response_schema"]["properties"]["text"]["maxLength"] == 8000
    item["response_schema"] = {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 1001}},
    }
    with pytest.raises(UnsupportedForm):
        interaction_elements("view", item)


@pytest.mark.asyncio
async def test_final_answer_is_delivered_as_markdown_card(runtime):
    """从飞书受理到完成，最终正文进入 markdown 组件而非 text 消息。"""
    _, accepted, _, gateway = await fresh(runtime)
    await tick(runtime)
    markdown = (
        "# 合成标题\n\n**重点**\n\n| 项目 | 数值 |\n| --- | --- |\n| 示例 | 1 |\n\n"
        "```python\nprint(1)\n```"
    )
    final_state(runtime, accepted, content=markdown)
    await tick(runtime)
    await publish(runtime, gateway)
    answers = gateway.calls[1:]
    assert len(answers) == 1 and answers[0]["message_type"] == "card"
    assert answers[0]["target_message_id"] == "message-1"
    card = json.loads(answers[0]["content"])
    assert card["schema"] == "2.0"
    assert card["body"]["elements"] == [{"tag": "markdown", "content": markdown}]


def test_long_code_blocks_keep_fences_and_unicode_across_cards():
    """跨片代码块仍可渲染，每行和多字节字符完整保留，卡片 JSON 不超出保守预算。"""
    rows = [f"print('合成🧪{i}')\n" for i in range(1800)]
    cards = answer_cards("```python\n" + "".join(rows) + "```\n")
    assert len(cards) > 1
    recovered = ""
    for card in cards:
        assert len(json.dumps(card, ensure_ascii=False).encode()) < 30000
        content = card["body"]["elements"][0]["content"]
        assert content.startswith("```python\n") and content.rstrip().endswith("```")
        recovered += "".join(
            line + "\n" for line in content.splitlines() if line.startswith("print")
        )
    assert recovered == "".join(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation", [False, True])
async def test_schema_rejection_is_a_definite_failure(runtime, creation):
    """卡片格式被明确拒绝后留死信，不把未送达表单误标为未知更新而堵住后续视图。"""
    if creation:
        await waiting(runtime)
    else:
        _, accepted, _, _ = await fresh(runtime)
        await tick(runtime)
        await ask(runtime, accepted)
    repository = NotificationRepository(
        runtime.turns.sessions, app_id="app", allowed_open_ids=frozenset({"user"})
    )
    while repository.materialize():
        pass
    gateway = Gateway(receipts=[Receipt("failed", error_class="card_schema_rejected_11310")])
    if creation:

        async def rejected(content):
            """复现飞书输入框越界和组件重名时的拒绝响应。"""
            raise CardCreationRejected(11310)

        gateway.create_card = rejected
    claim = repository.claim("sender", lease_seconds=60)
    await deliver(repository, gateway, claim, runtime.turns.settings)
    with repository.sessions() as session:
        row = session.get(Delivery, claim["delivery_id"])
        assert row.status == "dead_letter" and not row.uncertain
        assert "11310" in row.error_class
        assert session.scalar(select(Delivery).where(Delivery.status == "uncertain")) is None

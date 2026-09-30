"""面板处置按钮与状态机动作名的契约（Task 50 采集时发现的真实缺陷的回归）。

这一条是为了钉死一个**已经发生过**的缺陷
--------------------------------------------------
Task 50 采集真实面板截图时，在浏览器里点「提交处置结论」**找不到按钮**。
查下来不是环境问题，是真缺陷：``panel.js`` 原来拿
``confirm / reject / withdraw`` 三个词去匹配服务端给的 ``opt.action``，
而 :func:`reconq.ledger.models.action_options` 给的是状态机的动作名
``submit / approve / reject / withdraw`` —— **前两个永远匹配不上**，
于是待归因与待确认两种状态下处置区**一个按钮都不渲染**。
界面上还写着「当前状态可做：提交处置结论、复核通过」，点不动。

这类缺陷测试为什么此前没抓到
---------------------------
``tests/web/`` 全部用 :class:`fastapi.testclient.TestClient` 打 **HTTP 与
HTML**，``panel.js`` 是**浏览器里才执行**的原生 JS —— 没有任何一步真的
跑过那段匹配逻辑。断言「``/static/panel.js`` 返回 200」只能证明文件发出去了。

所以这里不试图在 Python 里模拟整段 DOM，而是钉**契约两端的名字集合**：
服务端在五种状态下可能给出的每一个 ``action``，都必须出现在前端的按钮表
``ACTION_BUTTONS`` 里，且按钮表不得有服务端给不出的键（多出来的键是
「点了必报错」的死按钮）。名字对不上，测试就红 —— 正是这次缺陷的形状。
"""

from __future__ import annotations

import json
import re

import pytest

from reconq.ledger.models import ALL_STATUSES, ACTION_SOURCES
from reconq.web.state import action_options


def _panel_action_keys() -> set[str]:
    """从 ``panel.js`` 里抠出 ``ACTION_BUTTONS`` 表的键。"""
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src", "reconq", "web", "static", "panel.js",
    )
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    match = re.search(
        r"var ACTION_BUTTONS\s*=\s*\{(.*?)\n  \};", source, re.S
    )
    assert match, "panel.js 里找不到 ACTION_BUTTONS 表（前端按钮表被删或改名了？）"
    return set(re.findall(r"^\s{4}(\w+)\s*:", match.group(1), re.M))


def test_every_server_action_has_a_button() -> None:
    """服务端在**任一**状态下给出的每个动作，前端都得有按钮能渲染它。"""
    keys = _panel_action_keys()
    server_actions: set[str] = set()
    for status in ALL_STATUSES:
        for option in action_options(status, requires_supervisor=False):
            server_actions.add(option["action"])
        for option in action_options(status, requires_supervisor=True):
            server_actions.add(option["action"])

    missing = sorted(server_actions - keys)
    assert not missing, (
        f"状态机在这些状态下会给出动作 {missing}，但 panel.js 的 ACTION_BUTTONS "
        f"里没有对应按钮 —— 界面上会显示「可做：…」却点不动。"
        f"服务端能给出的全部动作：{sorted(server_actions)}；前端按钮表：{sorted(keys)}"
    )


def test_button_table_has_no_dead_keys() -> None:
    """反向：按钮表不得有服务端给不出的键。"""
    keys = _panel_action_keys()
    # ACTION_SOURCES 是 {动作: {来源状态}}，键就是动作名。
    server_actions = set(ACTION_SOURCES)
    dead = sorted(keys - server_actions)
    assert not dead, (
        f"panel.js 的 ACTION_BUTTONS 里有服务端永远不会给出的动作 {dead}，"
        f"它们是「点了必报错」的死按钮"
    )


def test_submit_and_approve_both_map_to_confirm_decision() -> None:
    """submit 与 approve 在 HTTP 上都是 ``decision=confirm``。

    这条钉的是「动作名 ≠ 接口上的 decision 词」这层映射：写错的后果是
    待归因点下去走成 reject，或者待确认点下去服务端找不到该迁移。
    """
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src", "reconq", "web", "static", "panel.js",
    )
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    block = re.search(r"var ACTION_BUTTONS\s*=\s*\{(.*?)\n  \};", source, re.S)
    assert block
    body = block.group(1)
    for action in ("submit", "approve"):
        row = re.search(rf"^\s{{4}}{action}\s*:\s*\{{(.*?)\}},?\s*$", body, re.M)
        assert row, f"ACTION_BUTTONS 里没有 {action} 那一行"
        assert 'decision: "confirm"' in row.group(1), (
            f"{action} 应当映射到 decision=confirm（服务端按当前状态分派到 "
            f"submit() 还是 approve()），实得：{row.group(1).strip()}"
        )


def test_button_labels_come_from_server_not_duplicated_in_frontend() -> None:
    """中文措辞只在 ``ACTION_ZH`` 一处写。

    前端按钮表里留了 ``label`` 兜底，但**渲染时以服务端给的
    ``opt.label`` 为准** —— 断言的是这个优先级还在。
    """
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src", "reconq", "web", "static", "panel.js",
    )
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    assert "opt.label || spec.label" in source, (
        "按钮文案应优先取服务端给的 opt.label（ACTION_ZH 是唯一中文真源）"
    )


@pytest.mark.parametrize("status", sorted(ALL_STATUSES))
def test_action_options_are_self_consistent(status: str) -> None:
    """每个状态下算出的按钮集合必须恰好是「从本状态可出发」的那些动作。

    这条不是测前端的，是把 ``action_options`` 当契约钉住：它是前端唯一
    的按钮来源，它算错的话前端无论怎么渲染都错。
    """
    for requires_supervisor in (False, True):
        options = action_options(status, requires_supervisor=requires_supervisor)
        got = {o["action"] for o in options}
        assert got == set(ACTION_SOURCES) & {
            a for a, sources in ACTION_SOURCES.items() if status in sources
        }
        for option in options:
            assert option["label"], f"{status}/{option['action']} 没有中文标签"
            assert option["roles"], f"{status}/{option['action']} 没有角色，前端无从判断谁能点"
            json.dumps(option)  # 必须可序列化：它要经 HTTP 送到浏览器

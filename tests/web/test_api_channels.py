"""渠道主态端点（Task 49 · 六路各自那一条）。

左栏是财务科判断「这期账能不能签」的第一眼。钉住的是**六个字段**：
主态、笔数、合计金额、脏行数、未取到的错误码、以及中文渠道名。

金额是定点字符串
----------------
合计不走 float：``float("5394.69")`` 存下来再取出来是
``5394.689999999999``，而那串尾巴会一路印到结论单上、被财务科当成钱
对不上。所以断言的是 ``str`` 类型与逐位相等，不是「约等于」。
"""

from __future__ import annotations

from decimal import Decimal

def _cards(client, period):
    body = client.get(f"/api/period/{period}/channels").json()
    assert body["period"] == period
    return body["channels"]


def test_six_channels_always_present_in_registry_order(client, period):
    """六张卡恒在，顺序即展示顺序。

    少一张比多一张更危险：卡片不见了，「六路里少了哪一路」就得靠人记得
    住六个名字，而这正是最常被漏掉的一类。
    """
    from reconq.sources.registry import CHANNELS

    cards = _cards(client, period)
    assert [c["channel"] for c in cards] == list(CHANNELS)
    assert len(cards) == 6


def test_fetched_channel_reports_rows_total_and_zero_dirty(
    client, period, wechat_rows, wechat_total
):
    cards = _cards(client, period)
    wechat = next(c for c in cards if c["channel"] == "wechat")
    # 主态是 ``checked``（取数完成、逐 check 跑完），不是 ``fail``：
    # 扫描本身没失败，是两条 check 没通过。前者说的是「流程炸了」，
    # 后者说的是「账对不上」，在结论单附录上这两句话完全不同。
    assert wechat["status"] == "checked"
    assert wechat["row_count"] == wechat_rows
    assert wechat["total_amount"] == wechat_total
    assert isinstance(wechat["total_amount"], str), "合计必须是定点字符串，不能是 float"
    # 定点字符串逐位还原，尾数不该被二进制浮点吃掉
    assert Decimal(wechat["total_amount"]) == Decimal(wechat_total)
    assert wechat["checks_total"] == 15 and wechat["checks_failed"] == 2
    # 样例账期微信有 1 行必填字段为空，进了隔离区。渠道卡上的「脏行 1」
    # 是财务科判断「要不要去要一份重出的账单」的唯一线索，显示成 0
    # 会让人以为这一路干干净净。
    assert wechat["dirty_rows"] == 1
    assert wechat["error_code"] == "", "取到账单的渠道不该带未取到类的错误码"


def test_unfetched_channel_is_gray_card_with_error_code(client, period):
    """没取到的渠道给**灰卡 + 错误码**，不是不给卡。

    本轮只有微信账单是文件通道、其余四路要凭据、HIS 要 DSN，而本测试
    一个环境变量都没设 —— 所以另外五路必须是灰卡，且每张都要写清是哪个
    环境变量没设。灰卡上只写「未取到」而不写原因，等于把排障的第一步
    留给猜。
    """
    from reconq.web.state import BADGE_GRAY

    cards = _cards(client, period)
    gray = [c for c in cards if c["badge"] == BADGE_GRAY]
    assert len(gray) == 5, f"只有微信取到账单，其余五路应是灰卡，实际 {len(gray)} 张"
    for card in gray:
        assert card["row_count"] == 0
        assert Decimal(card["total_amount"]) == Decimal("0")
        assert card["error_code"], f"{card['channel']} 灰卡没写错误码"
        assert card["detail"], f"{card['channel']} 灰卡没写原因"

    his = next(c for c in gray if c["channel"] == "his")
    assert his["error_code"] == "CREDENTIAL_MISSING", (
        f"HIS 缺的是 HIS_DB_DSN，错误码应写 CREDENTIAL_MISSING，实际 {his['error_code']}"
    )
    # 缺凭据与没账单是**两件要办不同事情的事**：前者去配环境变量，
    # 后者去要账单文件。灰卡上不给可区分的码，前端只能拿中文 detail
    # 去做字符串匹配 —— 而 detail 的措辞一改，那段匹配就静默失效。
    others = {c["channel"]: c["error_code"] for c in gray if c["channel"] != "his"}
    assert set(others.values()) == {"CHANNEL_NOT_FETCHED"}, (
        f"其余四路应是「没取到账单」，实际 {others}"
    )


def test_green_badge_requires_everything_to_pass(client, period):
    """绿色只在这一路全过时出现。

    反过来写这条断言（``灰卡不得是绿的``）不够：某一路拿到账单但 check
    全过、且有未归因差异时也该是黄色 —— 面板上「绿=这路干净」这句话
    一旦在某一种情形下不成立，经办人就会开始按颜色跳过检查。
    """
    from reconq.web.state import BADGE_GREEN

    cards = _cards(client, period)
    assert not any(c["badge"] == BADGE_GREEN for c in cards), (
        "样例账期只有一路取到、且有未过 check，不该出现绿卡"
    )
    wechat = next(c for c in cards if c["channel"] == "wechat")
    assert wechat["badge"] in ("red", "yellow"), "取到但没过 check 只能是黄或红"


def test_every_card_has_zh_name_and_audit_fields(client, period):
    """每张卡都要带中文名、徽标中文与检查计数。

    徽标只有色没有字时，「红」和「深红」在投影屏上分不开；而渠道名只有
    英文码时，财务科得自己认 ``unionpay``。
    """
    for card in _cards(client, period):
        assert card["channel_zh"], f"{card['channel']} 缺中文名"
        assert card["channel_zh"] != card["channel"], f"{card['channel']} 中文名没翻译"
        assert card["badge_zh"], f"{card['channel']} 缺徽标中文"
        assert "checks_total" in card and "checks_failed" in card
        assert card["checks_failed"] <= card["checks_total"], (
            f"{card['channel']} 过 check 的数比总 check 还多，两个数字取自不同口径"
        )


def test_unknown_period_returns_gray_cards_not_error(client):
    """没扫过的账期给六张灰卡，而不是报错。

    财务科切账期是随手一点的事，点到一个还没扫的期次就该看到「这期还没
    扫」，而不是一屏报错 —— 报错会让人以为服务坏了，而不是账没扫。
    """
    cards = client.get("/api/period/2099-W01/channels").json()["channels"]
    assert len(cards) == 6
    assert all(c["error_code"] == "CHANNEL_NOT_SCANNED" for c in cards)

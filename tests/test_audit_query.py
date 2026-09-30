"""链式审计事件的写入、切片检索与校验（Task 45）。

规定的六类断言逐条可核：

1. **按期切片正确** —— 读一期只读那一期的字节区间，
   :attr:`EventSlice.bytes_scanned` 证明没有全量扫描；
2. **差异时间线时序正确** —— 一条差异从建账到导出按 seq 正序返回，
   且只有处置类事件进主线（取数/口径事件不进）；
3. **篡改报错并指出序号** —— 改一条 payload，校验报出**第几条**；
4. **genesis 锚点固定** —— 锚点是第 1 条的指纹、追加多少条都不变，
   且**删掉开头重接一条自洽的链**也能被锚点看出来（链身重算全过）；
5. **未知类型告警但不丢** —— 表外类型 ``logger.warning`` 且原样返回；
6. **脱敏** —— payload 含患者标识时**抛错拒写**，且事件文件里一行都没多。

外加几条本任务真正踩到的反例：索引尾部未登记（崩在「落事件」与「更新
索引」之间）要能补扫、事件文件被截断必须**整份重建**索引（沿用旧索引会
把别的账期的行读成这一期）、``open_chain`` 幂等（重复开链会让链上凭空多
一环而面板只看得见一条）、时间窗反了要报错（空集与「这段时间没事件」在
面板上无法区分）。
"""

from __future__ import annotations

import json
import os

import pytest

from reconq.audit.query import (
    EVENT_INDEX_NAME,
    EVENT_LOG_NAME,
    EVENT_TYPES,
    AuditEvent,
    append_event,
    assert_event_deidentified,
    chain_anchor,
    discrepancy_timeline,
    load_index,
    open_chain,
    query_events,
    rebuild_index,
    verify_chain,
)
from reconq.errors import ReconqError

PERIOD = "2026-W36"
OTHER_PERIOD = "2026-W37"
KEY_A = f"{PERIOD}|wechat|taxonomy.missing:EX-9"
KEY_B = f"{PERIOD}|alipay|taxonomy.missing:EX-11"


@pytest.fixture()
def audit_dir(tmp_path):
    return str(tmp_path / "audit")


def _seed(audit_dir: str, period: str = PERIOD, keys=(KEY_A, KEY_B)) -> None:
    """铺一条典型的账期事件链：开链 → 建差异 → 归因 → 确认 → 复核 → 导出。"""
    open_chain(audit_dir, period, actor="财务科·张")
    for index, key in enumerate(keys):
        append_event(audit_dir, period, "DIFF_CREATED", actor="reconq",
                     discrepancy_key=key, payload={"lines": index + 1})
    append_event(audit_dir, period, "MODEL_CALLED", actor="reconq",
                 discrepancy_key=KEY_A, payload={"model": "院内推理端点", "elapsed_ms": 812})
    append_event(audit_dir, period, "DECIDED", actor="经办·李",
                 discrepancy_key=KEY_A, payload={"disposition": "已确认", "note": "导出方漏传"})
    append_event(audit_dir, period, "REVIEWED", actor="主管·王",
                 discrepancy_key=KEY_A, payload={"ok": True})
    append_event(audit_dir, period, "EXPORTED", actor="财务科·张",
                 discrepancy_key=KEY_A, payload={"fmt": "md"})


def _read_raw(audit_dir: str) -> list[dict]:
    with open(os.path.join(audit_dir, EVENT_LOG_NAME), "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _rewrite(audit_dir: str, events: list[dict]) -> None:
    with open(os.path.join(audit_dir, EVENT_LOG_NAME), "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) + "\n")


# —— 1. 按期切片正确 ————————————————————————————————————————


def test_period_slice_returns_only_that_period_in_order(audit_dir):
    _seed(audit_dir, PERIOD)
    _seed(audit_dir, OTHER_PERIOD, keys=(f"{OTHER_PERIOD}|wechat|taxonomy.missing:EX-1",))

    found = query_events(audit_dir, PERIOD)
    assert found.used_index, "给了账期就该走索引切片"
    assert {e.period for e in found} == {PERIOD}
    assert [e.seq for e in found] == list(range(1, len(found) + 1))
    assert len(found) == 7
    assert all(e.event_type in EVENT_TYPES for e in found)


def test_period_slice_reads_only_that_period_bytes(audit_dir):
    """切片必须**只读那一期的字节**，而不是「读了全库再过滤」。

    两期各铺一份，其中一期事件条数远少于另一期；读少的那一期，
    读到的字节数必须明显小于整个事件文件。只断言「结果条数对」不够 ——
    全量扫描再过滤也会给出同样的条数，而一期几百万条事件时面板点一条差异
    要等全库读完，那正是这一层要消掉的。
    """
    _seed(audit_dir, PERIOD)
    big = OTHER_PERIOD
    open_chain(audit_dir, big, actor="财务科·张")
    for index in range(200):
        append_event(audit_dir, big, "CHANNEL_FETCHED", actor="reconq",
                     payload={"index": index})

    log_size = os.path.getsize(os.path.join(audit_dir, EVENT_LOG_NAME))
    found = query_events(audit_dir, PERIOD)
    assert found.used_index
    assert len(found) == 7
    assert 0 < found.bytes_scanned < log_size / 2, (
        f"读一期读了 {found.bytes_scanned} 字节 / 全库 {log_size} 字节 —— "
        "这已经接近全量扫描了"
    )
    # 索引登记的区间字节数与实际读到的字节数必须一致（不许读了还多报）。
    entry = load_index(audit_dir)["periods"][PERIOD]
    assert sum(b - a for a, b in entry["ranges"]) == found.bytes_scanned


def test_query_without_period_falls_back_to_full_scan_and_says_so(audit_dir):
    _seed(audit_dir, PERIOD)
    _seed(audit_dir, OTHER_PERIOD, keys=(f"{OTHER_PERIOD}|wechat|taxonomy.missing:EX-1",))

    found = query_events(audit_dir)
    assert not found.used_index, "没给账期就不该声称走了索引"
    assert found.bytes_scanned == os.path.getsize(os.path.join(audit_dir, EVENT_LOG_NAME))
    assert {e.period for e in found} == {PERIOD, OTHER_PERIOD}


def test_query_by_type_and_time_window(audit_dir):
    _seed(audit_dir, PERIOD)
    assert len(query_events(audit_dir, PERIOD, event_type="DECIDED")) == 1
    assert len(query_events(audit_dir, PERIOD, event_type="DIFF_CREATED")) == 2
    assert len(query_events(audit_dir, PERIOD, event_type="SCAN_STARTED")) == 1
    # 早于链头的时间窗返回空集；late 早于 early 显式报错（空集在面板上
    # 与「这段时间没事件」无法区分）。
    assert len(query_events(audit_dir, PERIOD, until="2000-01-01T00:00:00+08:00")) == 0
    with pytest.raises(ReconqError) as excinfo:
        query_events(audit_dir, PERIOD, since="2026-09-30T00:00:00+08:00",
                     until="2026-09-01T00:00:00+08:00")
    assert excinfo.value.code == "AUDIT_QUERY_INVALID"
    with pytest.raises(ReconqError) as bad:
        query_events(audit_dir, PERIOD, since="上周一")
    assert bad.value.code == "AUDIT_QUERY_INVALID"


def test_missing_audit_dir_is_not_an_error(audit_dir):
    """从没写过事件的目录：查得到空集，**不报错**。

    索引与事件文件都是可重建的加速器。「还没开链」是正常态，
    报成故障会让面板上每一期未扫描的账期都显示成红色。
    """
    found = query_events(audit_dir, PERIOD)
    assert len(found) == 0 and not found.used_index
    assert chain_anchor(audit_dir, PERIOD) == ""
    verdict = verify_chain(audit_dir, PERIOD)
    assert verdict.ok and verdict.checked == 0 and verdict.reason


def test_query_never_writes_to_the_audit_files(audit_dir):
    """查询是**只读**的：跑完一轮查询，事件文件与索引的字节数都不变。"""
    _seed(audit_dir, PERIOD)
    log = os.path.join(audit_dir, EVENT_LOG_NAME)
    index_path = os.path.join(audit_dir, EVENT_INDEX_NAME)
    before = (os.path.getsize(log), os.path.getsize(index_path))
    for _ in range(3):
        query_events(audit_dir, PERIOD)
        query_events(audit_dir)
        discrepancy_timeline(audit_dir, KEY_A)
    assert (os.path.getsize(log), os.path.getsize(index_path)) == before


# —— 2. 差异时间线时序正确 ————————————————————————————————————


def test_discrepancy_timeline_is_ordered_and_complete(audit_dir):
    _seed(audit_dir, PERIOD)
    timeline = discrepancy_timeline(audit_dir, KEY_A)
    assert [e.seq for e in timeline] == sorted(e.seq for e in timeline)
    assert [e.event_type for e in timeline] == [
        "DIFF_CREATED",
        "MODEL_CALLED",
        "DECIDED",
        "REVIEWED",
        "EXPORTED",
    ]
    assert [e.actor for e in timeline] == ["reconq", "reconq", "经办·李", "主管·王", "财务科·张"]
    # 另一条差异的事件不得混进来。
    assert all(e.discrepancy_key == KEY_A for e in timeline)
    assert len(discrepancy_timeline(audit_dir, KEY_B)) == 1


def test_timeline_carries_lifecycle_stages(audit_dir):
    _seed(audit_dir, PERIOD)
    stages = [e.stage for e in discrepancy_timeline(audit_dir, KEY_A)]
    assert stages == ["创建", "归因草稿", "人工确认", "复核", "导出"]
    # 过程类事件（取数/口径）不进主线阶段，否则时序图会被取数撑满。
    open_chain(audit_dir, PERIOD)
    fetch = append_event(audit_dir, PERIOD, "CHANNEL_FETCHED", actor="reconq")
    assert fetch.stage == ""


def test_timeline_slices_by_period_inferred_from_the_key(audit_dir):
    """差异键形如 ``账期|渠道|口径路径``，这一查应当**走索引切片**。"""
    other_key = f"{OTHER_PERIOD}|alipay|taxonomy.missing:EX-1"
    _seed(audit_dir, PERIOD)
    open_chain(audit_dir, OTHER_PERIOD, actor="财务科·张")
    append_event(audit_dir, OTHER_PERIOD, "DIFF_CREATED", discrepancy_key=other_key)

    found = query_events(audit_dir, PERIOD, discrepancy_key=KEY_A)
    assert found.used_index
    assert found.bytes_scanned < os.path.getsize(os.path.join(audit_dir, EVENT_LOG_NAME))
    assert discrepancy_timeline(audit_dir, KEY_A)
    # 别的账期的差异不得被算进本账期时序。
    assert [e.period for e in discrepancy_timeline(audit_dir, other_key)] == [OTHER_PERIOD]


def test_timeline_rejects_empty_key(audit_dir):
    _seed(audit_dir, PERIOD)
    with pytest.raises(ReconqError) as excinfo:
        discrepancy_timeline(audit_dir, "")
    assert excinfo.value.code == "AUDIT_DIFFERENCE_KEY_REQUIRED"


def test_timeline_with_malformed_key_still_answers(audit_dir):
    """键的形状变了（老数据/别的来源）时退化为全量扫描并照常返回，不报错。"""
    _seed(audit_dir, PERIOD)
    assert discrepancy_timeline(audit_dir, "no-separator-key") == []


# —— 3. 篡改报错并指出序号 ————————————————————————————————————


def test_verify_chain_passes_on_an_untouched_chain(audit_dir):
    _seed(audit_dir, PERIOD)
    verdict = verify_chain(audit_dir, PERIOD)
    assert verdict.ok and verdict.checked == 7 and verdict.broken_seq == 0
    assert verdict.anchor_ok
    assert verdict.reason == "链完整"


def test_tampering_one_event_reports_its_sequence_number(audit_dir):
    """改中间任意一条的 payload，校验必须报出**它是第几条**。

    只报「链坏了」不够：几百条事件里让人自己去数，审计就变成了体力活。
    """
    _seed(audit_dir, PERIOD)
    events = _read_raw(audit_dir)
    events[2]["payload"] = {"lines": 999}
    _rewrite(audit_dir, events)

    verdict = verify_chain(audit_dir, PERIOD)
    assert not verdict.ok
    assert verdict.broken_seq == 3
    assert "第 3 条" in verdict.reason
    assert verdict.expected_hash != verdict.actual_hash
    assert verdict.checked == 2


def test_tampering_the_last_event_reports_the_last_sequence(audit_dir):
    _seed(audit_dir, PERIOD)
    events = _read_raw(audit_dir)
    events[-1]["payload"] = {"fmt": "csv"}
    _rewrite(audit_dir, events)
    verdict = verify_chain(audit_dir, PERIOD)
    assert not verdict.ok and verdict.broken_seq == 7


def test_deleting_a_middle_event_reports_a_sequence_gap(audit_dir):
    _seed(audit_dir, PERIOD)
    events = _read_raw(audit_dir)
    del events[2]
    _rewrite(audit_dir, events)
    rebuild_index(audit_dir)
    verdict = verify_chain(audit_dir, PERIOD)
    assert not verdict.ok
    assert verdict.broken_seq == 4
    assert "跳号" in verdict.reason


def test_broken_prev_hash_is_reported_as_a_join_failure(audit_dir):
    _seed(audit_dir, PERIOD)
    events = _read_raw(audit_dir)
    events[3]["prev_hash"] = "f" * 64
    _rewrite(audit_dir, events)
    verdict = verify_chain(audit_dir, PERIOD)
    assert not verdict.ok and verdict.broken_seq == 4
    assert "前驱指纹" in verdict.reason


def test_corrupt_line_points_at_the_file_not_the_row(audit_dir):
    _seed(audit_dir, PERIOD)
    with open(os.path.join(audit_dir, EVENT_LOG_NAME), "a", encoding="utf-8") as handle:
        handle.write("{ 这不是 JSON\n")
    with pytest.raises(ReconqError) as excinfo:
        query_events(audit_dir, PERIOD)
    assert excinfo.value.code == "AUDIT_EVENT_CORRUPT"
    assert "不要就地删行" in str(excinfo.value)


# —— 4. genesis 锚点固定 ————————————————————————————————————————


def test_anchor_is_the_first_events_hash_and_never_moves(audit_dir):
    first = open_chain(audit_dir, PERIOD, actor="财务科·张")
    assert first.seq == 1
    assert chain_anchor(audit_dir, PERIOD) == first.chain_hash
    for _ in range(5):
        append_event(audit_dir, PERIOD, "CHANNEL_FETCHED", payload={"x": 1})
    assert chain_anchor(audit_dir, PERIOD) == first.chain_hash
    assert verify_chain(audit_dir, PERIOD).ok


def test_anchor_survives_index_loss_and_is_rebuilt_from_the_log(audit_dir):
    """索引是可重建的加速器：删掉它，锚点必须从事件文件重算回来。"""
    _seed(audit_dir, PERIOD)
    before = chain_anchor(audit_dir, PERIOD)
    os.remove(os.path.join(audit_dir, EVENT_INDEX_NAME))
    assert load_index(audit_dir)["periods"] == {}
    assert chain_anchor(audit_dir, PERIOD) == before
    assert verify_chain(audit_dir, PERIOD).ok


def test_reconnecting_a_self_consistent_chain_is_caught_by_the_anchor(audit_dir):
    """删掉开头 2 条、把剩下的链**重新接成自洽的**。

    这正是链尾指纹抓不住的那一类：链身重算全程通过，可「这一期的事件链」
    其实已经换了一条。只有开链时固定下来的锚点能看出来 —— 只按链尾口径
    校验的实现会一路报「链完整」。
    """
    from reconq.audit.query import _event_chain_hash

    _seed(audit_dir, PERIOD)
    anchor_before = chain_anchor(audit_dir, PERIOD)
    kept = _read_raw(audit_dir)[2:]
    prev = "0" * 64
    for index, event in enumerate(kept, start=1):
        event["seq"] = index
        event["prev_hash"] = prev
        event["chain_hash"] = _event_chain_hash(
            index, event["period"], event["event_type"], event["at"],
            event.get("actor", ""), event.get("discrepancy_key", ""),
            event.get("payload") or {}, prev,
        )
        prev = event["chain_hash"]
    _rewrite(audit_dir, kept)
    rebuild_index(audit_dir)

    # 重建不重数锚点 —— 锚点是证据，不是可重算的量。
    assert chain_anchor(audit_dir, PERIOD) == anchor_before
    verdict = verify_chain(audit_dir, PERIOD)
    assert not verdict.ok and not verdict.anchor_ok
    assert verdict.broken_seq == 1
    assert "锚点" in verdict.reason
    assert verdict.checked == len(kept), "链身本身是自洽的，坏的是链头"


def test_open_chain_is_idempotent(audit_dir):
    """重复开链不再追加：否则链上凭空多一环「又开了一次链」。

    那个多出来的环在面板上只看得见一条，而「本期事件链环数」会多 1，
    且因为 append-only 删不掉。
    """
    first = open_chain(audit_dir, PERIOD, actor="财务科·张")
    again = open_chain(audit_dir, PERIOD, actor="财务科·张")
    assert again.seq == first.seq and again.chain_hash == first.chain_hash
    assert len(query_events(audit_dir, PERIOD)) == 1
    assert len(_read_raw(audit_dir)) == 1


def test_bootstrap_flag_cannot_be_used_to_inject_a_mid_chain_genesis(audit_dir):
    """普通追加抢不走链首位置（anchor 只认第 1 条）。"""
    open_chain(audit_dir, PERIOD)
    append_event(audit_dir, PERIOD, "CHANNEL_FETCHED", payload={"a": 1})
    append_event(audit_dir, PERIOD, "SCAN_STARTED", payload={"b": 2})
    verdict = verify_chain(audit_dir, PERIOD)
    assert verdict.ok and verdict.checked == 3
    events = query_events(audit_dir, PERIOD)
    assert chain_anchor(audit_dir, PERIOD) == events[0].chain_hash
    assert events[0].seq == 1


# —— 5. 未知类型告警但不丢 ————————————————————————————————————


def test_unknown_event_type_warns_but_is_returned(audit_dir, caplog):
    _seed(audit_dir, PERIOD)
    with caplog.at_level("WARNING", logger="reconq.audit"):
        append_event(audit_dir, PERIOD, "SOMETHING_NEW_IN_V2", actor="reconq")
        found = query_events(audit_dir, PERIOD)

    assert len(found) == 8, "未知类型必须原样返回，不许丢"
    assert found.of_type("SOMETHING_NEW_IN_V2")
    assert found.unknown_types == ("SOMETHING_NEW_IN_V2",)
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("SOMETHING_NEW_IN_V2" in m and "不丢弃" in m for m in messages), messages
    # 未登记类型的中文名要带原文，不能返回空串（空串与「没填」无法区分）。
    assert "SOMETHING_NEW_IN_V2" in found.of_type("SOMETHING_NEW_IN_V2")[0].event_zh


def test_known_types_produce_no_warning(audit_dir, caplog):
    _seed(audit_dir, PERIOD)
    with caplog.at_level("WARNING", logger="reconq.audit"):
        found = query_events(audit_dir, PERIOD)
    assert found.unknown_types == ()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_event_type_table_covers_every_documented_type(audit_dir):
    """受控词表必须含规格点名的 11 类，缺一即回归。"""
    expected = {
        "SCAN_STARTED", "CHANNEL_FETCHED", "CHECK_FAILED", "DIFF_CREATED",
        "MODEL_CALLED", "DRAFT_REJECTED", "DECIDED", "REVIEWED", "EXPORTED",
        "CONTRACT_CHANGED", "RUN_DEGRADED",
    }
    assert expected <= set(EVENT_TYPES)
    for name in expected:
        append_event(audit_dir, PERIOD, name)
    found = query_events(audit_dir, PERIOD)
    assert found.unknown_types == ()
    assert {e.event_type for e in found} >= expected


# —— 6. 脱敏 ————————————————————————————————————————————————


def test_patient_field_in_payload_is_refused_and_nothing_is_written(audit_dir):
    _seed(audit_dir, PERIOD)
    before = len(_read_raw(audit_dir))
    with pytest.raises(ReconqError) as excinfo:
        append_event(audit_dir, PERIOD, "DECIDED", actor="经办·李",
                     discrepancy_key=KEY_A, payload={"patient_name": "张三"})
    assert excinfo.value.code == "AUDIT_EVENT_NOT_DEIDENTIFIED"
    assert len(_read_raw(audit_dir)) == before, "拒写必须真的没落盘"
    assert verify_chain(audit_dir, PERIOD).ok


def test_patient_identifier_embedded_in_free_text_is_refused(audit_dir):
    """嵌在整句话里的形态也要挡住（与结论单同一套模式表）。"""
    with pytest.raises(ReconqError) as excinfo:
        append_event(audit_dir, PERIOD, "DECIDED", actor="经办·李",
                     payload={"note": "已联系住院号 110105199001011234 的家属确认"})
    assert excinfo.value.code == "AUDIT_EVENT_NOT_DEIDENTIFIED"
    assert "形态" in str(excinfo.value)


def test_phone_shaped_value_is_refused_but_order_numbers_pass(audit_dir):
    with pytest.raises(ReconqError):
        append_event(audit_dir, PERIOD, "CHANNEL_FETCHED", payload={"contact": "13800138000"})
    # 渠道单号长得也像长数字串，但它不是患者标识 —— 不能一并挡掉，
    # 挡掉的话这条差异根本没法与源文件对上行。
    event = append_event(audit_dir, PERIOD, "DIFF_CREATED", discrepancy_key=KEY_A,
                         payload={"order_no": "4200001234202609081756", "amount": "66.00"})
    assert event.payload["order_no"] == "4200001234202609081756"


def test_deidentification_check_covers_nested_structures(audit_dir):
    with pytest.raises(ReconqError) as excinfo:
        assert_event_deidentified({"lines": [{"refs": [{"inpatient_no": "A00123"}]}]})
    assert excinfo.value.code == "AUDIT_EVENT_NOT_DEIDENTIFIED"
    assert "inpatient_no" in str(excinfo.value)
    # 不含患者标识的结构必须原样通过（不能变成「什么都拒」）。
    assert_event_deidentified({"lines": [{"refs": [{"channel": "wechat", "row": 18}]}]})


# —— 索引自愈（崩溃恢复） ————————————————————————————————————


def test_index_tail_missing_after_crash_is_rescanned(audit_dir):
    """崩在「落事件」与「更新索引」之间：尾部未登记的字节要能补扫。

    丢掉的后果不是慢，是**查不到** —— 面板上最后那几条处置看起来像没发生过。
    """
    _seed(audit_dir, PERIOD)
    with open(os.path.join(audit_dir, EVENT_LOG_NAME), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"seq": 8, "period": PERIOD, "event_type": "EXPORTED", "at": "2026-09-07T11:00:00+08:00",
             "actor": "财务科·张", "discrepancy_key": KEY_A, "payload": {"fmt": "csv"},
             "prev_hash": "x" * 64, "chain_hash": "y" * 64},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    # 索引仍只登记到第 7 条。
    assert load_index(audit_dir)["periods"][PERIOD]["count"] == 7
    found = query_events(audit_dir, PERIOD)
    assert len(found) == 8, "尾部未登记的事件必须被补扫出来"
    assert load_index(audit_dir)["periods"][PERIOD]["count"] == 8


def test_truncated_log_forces_full_index_rebuild(audit_dir):
    """事件文件比索引短 = 被截断/被换过：**必须整份重建**。

    沿用旧索引会把别的账期的行按旧偏移读成这一期的 —— 那比慢严重得多，
    面板上会显示出一条根本没发生过的处置。这里两期事件**交错**写入
    （真实并发扫描就是这样），截断后两期都还剩事件，才看得出重建对不对。
    """
    other_key = f"{OTHER_PERIOD}|wechat|taxonomy.missing:EX-1"
    open_chain(audit_dir, PERIOD)
    open_chain(audit_dir, OTHER_PERIOD)
    for index in range(6):
        append_event(audit_dir, PERIOD, "DIFF_CREATED", discrepancy_key=KEY_A,
                     payload={"n": index})
        append_event(audit_dir, OTHER_PERIOD, "DIFF_CREATED", discrepancy_key=other_key,
                     payload={"n": index})

    log = os.path.join(audit_dir, EVENT_LOG_NAME)
    with open(log, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    with open(log, "w", encoding="utf-8") as handle:
        handle.writelines(lines[:-3])  # 截掉尾部 3 条，索引仍记着旧长度

    for period, key in ((PERIOD, KEY_A), (OTHER_PERIOD, other_key)):
        found = query_events(audit_dir, period, discrepancy_key=key)
        assert found.used_index, f"{period} 重建后应重新走索引"
        assert found.events, f"{period} 截断后仍有事件"
        assert all(e.period == period for e in found), "旧偏移会把别的账期的行读进来"
        seqs = [e.seq for e in found]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    # 砍掉的是**尾部**，链身仍自洽、锚点也还在原处 —— 尾部截断本来就该
    # 表现为「少了几条后续动作」，不是「链被篡改」。这一条把两种情况的
    # 判据分开：锚点管的是开头被换，不是结尾少了几行。
    assert verify_chain(audit_dir, PERIOD).ok


def test_head_truncation_is_caught_by_the_anchor(audit_dir):
    """砍掉**开链那条**并重排序号：链身自洽，只有锚点能看出来。

    这与尾部截断是两种不同的现场：尾部截断表现为「后面少了几步」，
    属可解释；开头被砍掉表现为「这一期的链从别的环节开始」，属篡改。
    两者若混为一谈，前者天天误报、后者永远漏报。
    """
    from reconq.audit.query import _event_chain_hash

    _seed(audit_dir, PERIOD)
    anchor_before = chain_anchor(audit_dir, PERIOD)
    events = _read_raw(audit_dir)
    kept = events[1:]
    prev = "0" * 64
    for index, event in enumerate(kept, start=1):
        event["seq"] = index
        event["prev_hash"] = prev
        event["chain_hash"] = _event_chain_hash(
            index, event["period"], event["event_type"], event["at"],
            event.get("actor", ""), event.get("discrepancy_key", ""),
            event.get("payload") or {}, prev,
        )
        prev = event["chain_hash"]
    _rewrite(audit_dir, kept)
    rebuild_index(audit_dir)

    # 重建**不会**把锚点改成新的第 1 条 —— 锚点是证据，重建只补可重数的量。
    assert chain_anchor(audit_dir, PERIOD) == anchor_before
    verdict = verify_chain(audit_dir, PERIOD)
    assert not verdict.ok and not verdict.anchor_ok
    assert verdict.broken_seq == 1 and "锚点" in verdict.reason


def test_in_place_edit_with_same_length_forces_rebuild(audit_dir):
    """**长度恰好不变**的就地篡改，索引偏移已失效，必须靠行边界核出来。

    只比字节数看不出这种改动（文件一样长），但偏移已经指到别的行上了。
    不核行边界的话，按旧偏移读出来的是别的账期的整行 —— 面板上会显示一条
    根本没发生过的处置，而校验还报「链完整」。
    """
    other_key = f"{OTHER_PERIOD}|wechat|taxonomy.missing:EX-1"
    open_chain(audit_dir, PERIOD)
    open_chain(audit_dir, OTHER_PERIOD)
    append_event(audit_dir, PERIOD, "DIFF_CREATED", discrepancy_key=KEY_A, payload={"n": 1})
    append_event(audit_dir, OTHER_PERIOD, "DIFF_CREATED", discrepancy_key=other_key,
                 payload={"n": 1})

    log = os.path.join(audit_dir, EVENT_LOG_NAME)
    with open(log, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    # 把别的账期那一行的 payload 改成长度相同的内容（"n":1 → "n":9）。
    rewritten = [line.replace('"n":1', '"n":9') if OTHER_PERIOD in line else line
                 for line in lines]
    assert len("".join(rewritten)) == len("".join(lines)), "本用例要求长度不变"
    with open(log, "w", encoding="utf-8") as handle:
        handle.writelines(rewritten)

    mine = query_events(audit_dir, PERIOD, discrepancy_key=KEY_A)
    assert mine.used_index
    assert [e.payload.get("n") for e in mine] == [1], "别的账期的行被读成本账期的了"
    theirs = query_events(audit_dir, OTHER_PERIOD, discrepancy_key=other_key)
    assert [e.payload.get("n") for e in theirs] == [9]


def test_append_repairs_a_stale_index_before_writing(audit_dir):
    """索引过期时先修再写：新事件接在**真实链尾**后面，而不是接在
    索引记的那个不存在的尾巴后面。"""
    _seed(audit_dir, PERIOD)
    with open(os.path.join(audit_dir, EVENT_LOG_NAME), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"seq": 8, "period": PERIOD, "event_type": "EXPORTED", "at": "2026-09-07T11:00:00+08:00",
             "actor": "", "discrepancy_key": "", "payload": {},
             "prev_hash": "x" * 64, "chain_hash": "y" * 64},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    ninth = append_event(audit_dir, PERIOD, "CHANNEL_FETCHED", payload={"n": 9})
    assert ninth.seq == 9


# —— 写入前置条件 ————————————————————————————————————————————


def test_append_requires_period_and_event_type(audit_dir):
    with pytest.raises(ReconqError) as no_period:
        append_event(audit_dir, "", "SCAN_STARTED")
    assert no_period.value.code == "AUDIT_PERIOD_REQUIRED"
    with pytest.raises(ReconqError) as no_type:
        append_event(audit_dir, PERIOD, "  ")
    assert no_type.value.code == "AUDIT_EVENT_TYPE_REQUIRED"


def test_log_dir_is_required(audit_dir):
    with pytest.raises(ReconqError) as excinfo:
        query_events("")
    assert excinfo.value.code == "AUDIT_LOG_DIR_REQUIRED"


def test_appended_event_round_trips_through_json(audit_dir):
    written = append_event(audit_dir, PERIOD, "DIFF_CREATED", actor="reconq",
                           discrepancy_key=KEY_A, payload={"lines": 2})
    restored = AuditEvent.from_json(written.to_json())
    assert restored == written
    assert restored.to_dict() == written.to_dict()
    assert restored.event_zh == "差异入账"


def test_event_slice_behaves_like_a_sequence(audit_dir):
    _seed(audit_dir, PERIOD)
    found = query_events(audit_dir, PERIOD)
    assert len(found) == len(list(found)) == 7
    assert found[0].seq == 1
    assert found[-1].event_type == "EXPORTED"
    assert found.to_dict()["count"] == 7


# —— 结论单引用链头锚点（Task 45 与 Task 44 的接缝） ————————————


@pytest.fixture()
def scanned_conn():
    """一个已扫描的账期库（结论单要读台账，空库读不出来）。"""
    import sqlite3

    from reconq.engine.period import finish_scan, init_scan_schema, start_scan

    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    init_scan_schema(connection)
    start_scan(connection, PERIOD, run_id="run-demo", channels_total=6)
    finish_scan(
        connection, PERIOD, channels_ok=5, channels_failed=1, rows_ingested=2000,
        checks_total=42, checks_failed=3, unconfirmed=0, pending_attribution=0,
        total_amount="390520.00",
    )
    yield connection
    connection.close()


def test_conclusion_sheet_cites_the_chain_anchor(audit_dir, scanned_conn):
    """结论单指纹头要印出审计链的**锚点**。

    结论单是打印出来签字归档的纸面件。「这一期的事件链没被动过」这句话要
    在纸上可核，就得在纸上写一个**不随追加而变**的值 —— 链尾每次追加都变，
    拿它当承诺等于没承诺。
    """
    from reconq.export.conclusion import build_conclusion, render_conclusion_markdown

    first = open_chain(audit_dir, PERIOD, actor="财务科·张")
    append_event(audit_dir, PERIOD, "DIFF_CREATED", discrepancy_key=KEY_A,
                 payload={"lines": 1})

    doc = build_conclusion(scanned_conn, PERIOD, audit_dir=audit_dir)
    assert doc.chain_anchor == first.chain_hash
    assert f"- 审计链头锚点：`{first.chain_hash}`" in render_conclusion_markdown(doc)

    # 追加若干条后锚点不变 —— 承诺的是「这条链从哪儿开始」，不是「现在多长」。
    for index in range(3):
        append_event(audit_dir, PERIOD, "REVIEWED", actor="主管·王",
                     discrepancy_key=KEY_A, payload={"n": index})
    assert build_conclusion(scanned_conn, PERIOD, audit_dir=audit_dir).chain_anchor == first.chain_hash
    assert doc.to_dict()["chain_anchor"] == first.chain_hash


def test_conclusion_sheet_without_audit_dir_says_so(scanned_conn):
    """没建审计链时指纹头写「未建审计链」，**不报错也不留空**。

    留空会让那一行在纸上消失；报错会让「没开审计」表现为「导不出结论单」，
    方向完全错。
    """
    from reconq.export.conclusion import build_conclusion, render_conclusion_markdown

    doc = build_conclusion(scanned_conn, PERIOD)
    assert doc.chain_anchor == ""
    assert "- 审计链头锚点：`（未建审计链）`" in render_conclusion_markdown(doc)

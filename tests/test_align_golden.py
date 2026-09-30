"""对齐黄金样例回归集（Task 40）。

三层断言，从外到里：

1. **主断言** —— 40+ 条用例逐条通过（``pytest -m align_golden`` 一次跑全）；
2. **门禁断言** —— 四形态覆盖门禁真的生效：删掉任一形态的用例，加载期即拒，
   且错误码点名是哪个形态、缺几条；**门禁不能被「先跑完再报」架空**；
3. **报告断言** —— ``out/align-regression.md`` 能生成，且报告里必须同时说清
   「跑了多少」与「这些数字不能说明什么」。

**登记规范的守卫**：新增形态必须先登记、必须先加用例才能改代码 —— 这条规范
本身也要能被测到，所以本文件里有专门用例断言「未登记的形态标签在加载期被拒」
以及「四形态少一种门禁就红」。否则规范只写在注释里，半年后没人记得。
"""

from __future__ import annotations

import os

import pytest
import yaml

from reconq.align.cases import (
    CASE_FORMS,
    DEFAULT_CASES_PATH,
    FOUR_AMBIGUITY_FORMS,
    FORM_ZH,
    MIN_CASES,
    MIN_CASES_PER_AMBIGUITY_FORM,
    AlignCase,
    CaseExpectation,
    CaseSet,
    load_case_set,
    render_regression_report,
    run_case,
    run_regression,
    write_regression_report,
)
from reconq.align.matcher import MatchStatus
from reconq.align.taxonomy import AmbiguityKind as TaxonomyKind
from reconq.errors import ERROR_CODES, ReconqError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_PATH = os.path.join(ROOT, DEFAULT_CASES_PATH)


def _load() -> CaseSet:
    return load_case_set(CASES_PATH)


# ══════════════════════ 一、加载期门禁 ══════════════════════


def test_case_set_loads_and_meets_the_declared_minimum():
    """回归集能加载，且条数达下限。"""
    case_set = _load()
    assert case_set.total >= MIN_CASES, f"只有 {case_set.total} 条，低于下限 {MIN_CASES}"
    assert case_set.set_version, "缺 set_version：半年后没人说得清这份回归集是哪一版"
    assert case_set.period, "缺 period：跨账期落点这一形态就没法判"
    # 标注元数据不是装饰 —— 没有出处的回归集不该被当成验收依据
    assert case_set.meta.get("annotated_source") not in (None, "", "（未记录）")
    assert case_set.meta.get("annotated_at")


def test_every_case_id_is_unique_and_forms_are_controlled():
    case_set = _load()
    ids = [c.case_id for c in case_set.cases]
    assert len(ids) == len(set(ids)), "case_id 重复：报告与失败信息就定位不到「哪一条错了」"
    for case in case_set.cases:
        assert case.form in CASE_FORMS
        # 受控词表里不得有「其他 / misc / unknown」这类兜底标签：
        # 兜底标签会让新形态悄悄塞进已有形态那一格，覆盖门禁跟着失效
        assert case.form not in {"other", "misc", "unknown", "其他"}
        assert FORM_ZH[case.form][0] and FORM_ZH[case.form][1], (
            f"形态 {case.form} 缺中文说明：报告里「坏了哪一类」要靠它"
        )


def test_four_ambiguity_forms_each_have_at_least_four_cases():
    """四形态覆盖门禁在**加载期**生效（不是跑完再报）。"""
    case_set = _load()
    counts = case_set.form_counts()
    for form in FOUR_AMBIGUITY_FORMS:
        assert counts.get(form, 0) >= MIN_CASES_PER_AMBIGUITY_FORM, (
            f"形态 {form}（{FORM_ZH[form][0]}）只有 {counts.get(form, 0)} 条，"
            f"低于下限 {MIN_CASES_PER_AMBIGUITY_FORM} 条"
        )


@pytest.mark.parametrize("dropped", FOUR_AMBIGUITY_FORMS)
def test_coverage_gate_rejects_a_set_missing_any_one_ambiguity_form(tmp_path, dropped):
    """**门禁真的生效**：抽掉任一四形态，加载期即拒并点名是哪个形态缺几条。

    逐个参数化而不是只测一个 —— 只测一种形态时，把门禁实现成「只查
    one_code_multi_settle」也能过，而另外三种形态的退化就再也拦不住了。
    这正是门禁本身要防的那类退化。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    doc["cases"] = [c for c in doc["cases"] if c.get("form") != dropped]
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(path))
    err = excinfo.value
    assert err.code == "ALIGN_FORM_COVERAGE_INSUFFICIENT"
    assert dropped in err.detail, "错误信息没点名缺的是哪个形态：经办人不知道该补哪一类"
    assert FORM_ZH[dropped][0] in err.detail, "错误信息没给中文形态名"
    assert str(MIN_CASES_PER_AMBIGUITY_FORM) in err.detail, "错误信息没写清下限是几条"
    # 错误码本身也要能被人读懂（面板拿它反查建议动作）
    meta = ERROR_CODES[err.code]
    assert meta.zh and meta.suggestion


def test_coverage_gate_is_checked_before_running_anything(tmp_path):
    """门禁在**加载期**拒，不是「跑完 40 条再报」。

    两者对使用者的影响完全不同：加载期拒意味着「这份回归集压根不能用」，
    跑完再报容易被当成「这次改动没问题，只是少覆盖了一种形态」—— 而后者
    恰恰是最危险的读法。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    doc["cases"] = [c for c in doc["cases"] if c.get("form") != "merged"]
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    # 只调用加载，不调用 run_regression：必须在这里就抛
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(path))
    assert excinfo.value.code == "ALIGN_FORM_COVERAGE_INSUFFICIENT"


def test_set_below_the_minimum_is_rejected(tmp_path):
    """四形态都达标、但总数不够 → 报「条数不足」而不是「形态缺失」。

    构造上刻意**保住**四种歧义形态各 4 条再截断：只有这样两条门禁才是各管
    各的。上一条门禁测试已经证明「形态缺失优先于条数不足」，两条一起才有意义。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    keep: list[dict] = []
    for form in FOUR_AMBIGUITY_FORMS:
        keep.extend(
            [c for c in doc["cases"] if c.get("form") == form][:MIN_CASES_PER_AMBIGUITY_FORM]
        )
    assert len(keep) < MIN_CASES, "构造的样本没截到下限以下，这条用例就失去意义"
    doc["cases"] = keep
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(path))
    err = excinfo.value
    assert err.code == "ALIGN_CASE_SET_TOO_SMALL"
    assert str(MIN_CASES) in err.detail
    # 诊断要说清「形态已达标、缺的是其余形态的厚度」，否则使用者会去补错的东西
    assert "四形态覆盖已达标" in err.detail


def test_missing_file_and_empty_set_report_distinct_codes(tmp_path):
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(tmp_path / "nope.yml"))
    assert excinfo.value.code == "ALIGN_CASE_SET_MISSING"

    empty = tmp_path / "empty.yml"
    empty.write_text("cases: []\n", encoding="utf-8")
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(empty))
    # 空集报 EMPTY 而不是 TOO_SMALL：两条码的区别是「被清空」与「不够多」，
    # 现场处置完全不同（前者去查文件，后者去补用例）
    assert excinfo.value.code == "ALIGN_CASE_SET_EMPTY"


def test_unregistered_form_tag_is_rejected_and_lists_the_controlled_vocabulary(tmp_path):
    """**登记规范的守卫**：未登记的形态标签在加载期被拒，并列出全部合法标签。

    没有这条守卫，「新增形态先塞进 single 那一格」就会悄悄发生 ——
    覆盖门禁看着是过的，那一种形态其实一条用例都没有。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    doc["cases"][0]["form"] = "some_new_shape"
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(path))
    err = excinfo.value
    assert err.code == "ALIGN_CASE_FORM_UNKNOWN"
    for form in CASE_FORMS:
        assert form in err.detail, f"错误信息没列出合法形态 {form}"
    assert "登记" in err.detail, "错误信息没告诉人新增形态要先登记"


def test_duplicate_case_id_is_rejected(tmp_path):
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    doc["cases"].append(dict(doc["cases"][0]))
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(path))
    assert excinfo.value.code == "ALIGN_CASE_INVALID"
    assert "重复" in excinfo.value.detail


@pytest.mark.parametrize(
    "mutate,expect_fragment",
    [
        (lambda d: d["cases"][0].pop("expect"), "expect"),
        (lambda d: d["cases"][0].update({"channel": ""}), "channel"),
        (lambda d: d["cases"][0]["channel_rows"][0].pop("amount"), "amount"),
        (lambda d: d["cases"][0]["channel_rows"][0].update({"trade_time": "2026-09-02 10:00:00"}), "时区"),
        (lambda d: d["cases"][0].update({"focus_index": 99}), "focus_index"),
        (lambda d: d["cases"][0]["expect"].update({"status": "not_a_status"}), "expect.status"),
        (lambda d: d["cases"][0]["expect"].update({"kind": "not_a_kind"}), "expect.kind"),
        (lambda d: d["cases"][0].update({"channel_rows": []}), "channel_rows"),
    ],
)
def test_malformed_cases_are_rejected_with_a_readable_error(tmp_path, mutate, expect_fragment):
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    mutate(doc)
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ReconqError) as excinfo:
        load_case_set(str(path))
    assert excinfo.value.code in {"ALIGN_CASE_INVALID", "ALIGN_CASE_SET_INVALID"}
    assert expect_fragment in excinfo.value.detail


# ══════════════════════ 二、逐条跑（主断言） ══════════════════════


def _all_cases() -> list[AlignCase]:
    return list(_load().cases)


@pytest.mark.align_golden
@pytest.mark.parametrize("case", _all_cases(), ids=lambda c: f"{c.case_id}-{c.form}")
def test_golden_case_matches_every_expected_field(case: AlignCase):
    """一条 case 的五项期望逐项比对：状态 / 歧义形态 / 成因 / 证据行号 /
    差额 / 反向单边账。

    逐项独立报而不是只报「结论不对」—— 「状态对但少给了一条候选行」这种
    改动必须被抓住，因为候选行号正是经办人逐条回查时最先用到的东西。
    """
    outcome = run_case(case)
    assert outcome.passed, (
        f"回归用例 {case.case_id}（形态 {case.form} / {FORM_ZH[case.form][0]}，"
        f"渠道 {case.channel}）不通过：{'；'.join(outcome.mismatches)}\n"
        f"用例意图：{case.note}"
    )


@pytest.mark.align_golden
def test_the_whole_golden_set_passes():
    """整批跑一遍并打印失败明细。

    单条参数化已经逐条断言过；这条额外保证**批量路径**（``run_regression``
    的逐条独立异常隔离）也全绿 —— 一条 case 崩了不该把整批报告带走。
    """
    report = run_regression(_load())
    assert report.ok, "失败用例：\n" + "\n".join(o.render() for o in report.failures)
    assert report.passed == report.total == len(_all_cases())


@pytest.mark.align_golden
def test_failed_case_names_the_case_id_and_the_form_tag():
    """失败信息必须带 case_id **与形态标签**。

    只给 case_id 时，看到「ACS-05 失败」的人得自己去翻 cases.yml 才知道那是
    哪一类差异；形态标签才是「改哪一处代码」的直接线索。
    """
    case = _load().by_id("ACS-01")
    broken = AlignCase(
        case_id=case.case_id,
        form=case.form,
        channel=case.channel,
        period=case.period,
        note=case.note,
        channel_rows=case.channel_rows,
        his_rows=case.his_rows,
        focus_index=case.focus_index,
        # 故意写一个错的期望：状态改成渠道有 HIS 无
        expect=CaseExpectation(status=MatchStatus.CHANNEL_ONLY.value),
    )
    outcome = run_case(broken)
    assert not outcome.passed
    rendered = outcome.render()
    assert "ACS-01" in rendered
    assert "single" in rendered
    assert FORM_ZH["single"][0] in rendered
    assert any("状态" in m for m in outcome.mismatches)


@pytest.mark.align_golden
def test_all_four_ambiguity_forms_reach_the_matcher_ambiguity_layer():
    """四种歧义形态**真的走到了歧义判定这一层**。

    门禁只数条数；这条保证「数出来的形态」不是被别处的分支提前短路掉的。
    四形态全部走到 AMBIGUOUS/CROSS_PERIOD/WEAK_PENDING 结论，才说明这些用例
    真的在守那四条判据。
    """
    ambiguity_statuses = {
        MatchStatus.AMBIGUOUS.value,
        MatchStatus.CROSS_PERIOD.value,
        MatchStatus.WEAK_PENDING.value,
        MatchStatus.DUPLICATE_CLAIM.value,
    }
    seen: dict[str, set[str]] = {}
    for case in _all_cases():
        outcome = run_case(case)
        if outcome.status in ambiguity_statuses:
            seen.setdefault(case.form, set()).add(outcome.status)
    for form in FOUR_AMBIGUITY_FORMS:
        assert seen.get(form), f"形态 {form} 没有任何一条走到歧义判定层"


@pytest.mark.align_golden
def test_taxonomy_kind_is_recorded_even_when_not_asserted():
    """``expect.taxonomy_kind`` 留空 = 不作为判据，**但照样记录**。

    留空若连记录一起省掉，报告里就看不出「这一类本来会判成什么」，
    下次有人给 single 形态加成因判据时就没有对照基线。
    """
    for case in _all_cases():
        outcome = run_case(case)
        if not case.expect.checks_taxonomy:
            # 非歧义结论不该硬凑一个成因
            if outcome.status not in {
                MatchStatus.AMBIGUOUS.value,
                MatchStatus.CROSS_PERIOD.value,
                MatchStatus.WEAK_PENDING.value,
                MatchStatus.DUPLICATE_CLAIM.value,
            }:
                assert outcome.taxonomy_kind == "", (
                    f"{case.case_id} 不是歧义结论，却算出了成因 {outcome.taxonomy_kind}"
                )


@pytest.mark.align_golden
def test_four_ambiguity_forms_cover_all_four_taxonomy_causes():
    """四条成因（一码付拆多笔 / 合并支付 / 跨账期落点 / 退费并存）都有样本。

    形态名与成因名是两套词（形态描述「现场长什么样」，成因描述「该查什么」），
    两者若对不上，覆盖门禁守的就不是真正想守的东西。
    """
    covered = set()
    for case in _all_cases():
        if case.form in FOUR_AMBIGUITY_FORMS:
            covered.add(case.expect.taxonomy_kind)
    for cause in (
        TaxonomyKind.SPLIT_PAYMENT.value,
        TaxonomyKind.MERGED_PAYMENT.value,
        TaxonomyKind.CROSS_PERIOD.value,
        TaxonomyKind.REFUND_COEXIST.value,
    ):
        assert cause in covered, f"成因 {cause} 在四形态里一条样本都没有"
    # 「不可判定」也必须有样本：只有正例的话，「判不出来」这条能力没人验过
    assert TaxonomyKind.UNKNOWN.value in covered, "unknown 成因一条样本都没有"


# ══════════════════════ 三、报告 ══════════════════════


def test_regression_report_is_written_with_counts_and_form_breakdown(tmp_path):
    """报告能落盘，且形态分布、通过/失败数逐形态可核。"""
    report = run_regression(_load())
    out = tmp_path / "align-regression.md"
    path = write_regression_report(str(out), report, written_at="2026-09-30T00:00:00+08:00")
    assert os.path.isfile(path)
    text = open(path, "r", encoding="utf-8").read()

    assert f"通过 {report.passed} / {report.total}" in text
    assert "## 1. 形态分布" in text
    for form in CASE_FORMS:
        assert form in text, f"报告的形态分布里缺 {form}"
    # 逐条结论表：每条 case 都要能被逐条定位
    for case in _all_cases():
        assert case.case_id in text, f"报告逐条结论里缺 {case.case_id}"


def test_report_states_what_the_numbers_cannot_show(tmp_path):
    """报告必须写清「这些数字不能说明什么」。

    只报一句「45 条全绿」而不划边界，等于让人拿它当覆盖率与召回率的证据 ——
    而它守的只是回归面。签验收用报告缺这一段就等于给了个可以误用的数字。
    """
    report = run_regression(_load())
    out = tmp_path / "align-regression.md"
    write_regression_report(str(out), report)
    text = open(out, "r", encoding="utf-8").read()
    assert "## 2. 这些数字不能说明什么" in text
    assert "回归面" in text
    assert "召回" in text and "误报" in text
    # 必须把指标那条腿指向另一份金标集，避免有人拿这份报告当指标依据
    assert "align-metrics" in text and "golden.jsonl" in text


def test_failed_case_shows_up_in_the_report_with_per_field_diff(tmp_path):
    """失败项在报告里带 case_id、形态与逐项差异，不是只给一个失败计数。"""
    case_set = _load()
    broken = tuple(
        AlignCase(
            case_id="ACS-01",
            form=c.form,
            channel=c.channel,
            period=c.period,
            note=c.note,
            channel_rows=c.channel_rows,
            his_rows=c.his_rows,
            focus_index=c.focus_index,
            expect=CaseExpectation(
                status=MatchStatus.AMBIGUOUS.value,
                kind="merged",
                evidence_rows=(999,),
                diff_amount="0.01",
            ),
        )
        for c in case_set.cases
    )
    report = run_regression(CaseSet(path="broken.yml", cases=broken, period=case_set.period))
    assert not report.ok
    text = render_regression_report(report, out_path="out/align-regression.md")
    assert "## 3. 失败明细" in text
    assert "ACS-01" in text
    assert "逐项差异" in text
    for fragment in ("状态", "歧义形态", "证据行号", "差额"):
        assert fragment in text, f"失败明细里没点名「{fragment}」这一项"
    assert "0 条" in text or "| 一对一 / 明确单边" in text  # 形态分布仍在


def test_report_form_matrix_counts_pass_and_fail_per_form():
    report = run_regression(_load())
    matrix = report.form_matrix()
    assert set(matrix) <= set(CASE_FORMS)
    assert sum(c["total"] for c in matrix.values()) == report.total
    assert sum(c["passed"] for c in matrix.values()) == report.passed
    assert sum(c["failed"] for c in matrix.values()) == report.failed
    for form, cell in matrix.items():
        assert cell["passed"] + cell["failed"] == cell["total"], f"形态 {form} 的通过/失败数对不上总数"


def test_run_regression_survives_one_case_raising():
    """一条 case 崩了整批仍要能出报告（异常被隔离在该条上）。"""
    case_set = _load()
    good = case_set.cases[:3]
    # 造一条必然抛异常的：把 focus_index 指到一个不存在的行
    broken = AlignCase(
        case_id="BOOM-01",
        form="single",
        channel="wechat",
        period=case_set.period,
        note="故意构造",
        channel_rows=good[0].channel_rows,
        his_rows=good[0].his_rows,
        focus_index=999,
        expect=good[0].expect,
    )
    report = run_regression(CaseSet(path="x.yml", cases=(*good, broken), period=case_set.period))
    assert report.total == 4
    assert report.passed == 3
    assert [o.case_id for o in report.failures] == ["BOOM-01"]
    assert "抛异常" in report.failures[0].mismatches[0]


# ══════════════════════ 四、脱敏与口径 ══════════════════════


def test_no_patient_identifiable_detail_in_the_case_set():
    """回归集里不得出现患者可识别字段。

    对账对齐只需要「谁、多少钱、什么时候、哪个单号」，不需要任何患者信息 ——
    样例里出现姓名 / 身份证 / 手机号即为越界（这份 YAML 是要进仓库的）。
    """
    forbidden = {"patient", "patient_name", "name", "id_card", "phone", "mobile", "住院号", "姓名", "身份证", "手机号"}
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    for case in doc["cases"]:
        for side in ("channel_rows", "his_rows"):
            for row in case[side]:
                for key in row:
                    assert key.lower() not in {f.lower() for f in forbidden}, (
                        f"用例 {case['case_id']} 出现了疑似患者可识别字段 {key}"
                    )


def test_amounts_are_written_as_decimal_strings_not_yaml_floats():
    """金额一律用字符串定点写。

    YAML 里裸写 ``amount: 0.1`` 会被解析成 float，0.1+0.2 不等于 0.3 ——
    对账系统里最不能出错的就是钱，这类用例必须挡住。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    for case in doc["cases"]:
        for side in ("channel_rows", "his_rows"):
            for i, row in enumerate(catter_case_rows(case, side)):
                for key in ("amount", "refund"):
                    if key in row and row[key] is not None:
                        assert isinstance(row[key], str), (
                            f"用例 {case['case_id']} 的 {side}[{i}].{key}={row[key]!r} "
                            "不是字符串定点，YAML float 会引入二进制误差"
                        )


def catter_case_rows(case: dict, side: str) -> list[dict]:
    return [r for r in (case.get(side) or []) if isinstance(r, dict)]


def test_order_numbers_are_placeholders_distinct_from_the_real_sample_bills():
    """回归集单号与 2026-W36 样例账单（EX-* / WXTXN-*）无交集。

    两套用例同属一个仓库，单号撞车会让「哪条 case 跑的是哪笔」说不清。
    """
    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    for case in doc["cases"]:
        for side in ("channel_rows", "his_rows"):
            for row in catter_case_rows(case, side):
                for key in ("order_no", "his_order_no"):
                    value = str(row.get(key) or "")
                    assert not value.startswith(("EX-", "WXTXN-", "WXREFUND")), (
                        f"用例 {case['case_id']} 的 {key}={value} 与真实样例账单单号同前缀"
                    )

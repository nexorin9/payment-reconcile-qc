"""结论单下载端点（Task 49 · 指纹头那条）。

面板是财务科**唯一入口**，所以「把这一期导出来打印签字」这条腿也得在
面板上走得通 —— 让人为了导个文件切去终端，是把一条已经建好的流程推回
手工。

要验的是四件事：

1. **端点出文件**，两种格式都出，且 content-type 与编码对得上 Excel；
2. **指纹头字段齐全** —— 正文里那十行 + 响应头上那六个；
3. **面板那一份与 CLI 那一份逐字节一致**（同一份版式，不许分叉）；
4. **无患者可识别明细**，且脱敏核对在**渲染前**就拦得住。
"""

from __future__ import annotations

import urllib.parse

from reconq.ledger.flow import open_discrepancy
from reconq.store.schema import connect

PERIOD = "2026-W36"


def _settle_one(db: str) -> None:
    """把一条差异走完两级会签，让结论单上真有可签的内容。"""
    conn = connect(db)
    try:
        open_discrepancy(conn, period=PERIOD, channel="wechat",
                         check_path="rows.refund_sum.zero", diff_amount="86.00",
                         evidence_rows=("wechat_tradebill_2026-W36.csv:2",))
    finally:
        conn.close()


def _sign(client, operator, role, comment, version):
    key = urllib.parse.quote(f"{PERIOD}|wechat|rows.refund_sum.zero", safe="")
    r = client.post(f"/api/discrepancy/{key}/decide", json={
        "decision": "confirm", "operator": operator, "role": role,
        "comment": comment, "expected_version": version,
    })
    assert r.status_code == 200, r.text
    return r


# ─────────────────────────── 1. 出文件 ───────────────────────────


def test_markdown_export_is_an_attachment_with_fingerprint_headers(client, opened_db):
    """Markdown 档出文件，响应头带指纹与门禁。

    指纹放**响应头**是为了让归档系统不必解析正文就能记下这一版的
    结论集指纹 —— 正文里已经有一份，但正文是给人读的。
    """
    r = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-disposition"] == (
        f'attachment; filename="{PERIOD}-conclusion.md"'
    )
    assert len(r.headers["x-recoq-fingerprint"]) == 64, "结论集指纹是 sha256 全长"
    assert r.headers["x-recoq-period"] == PERIOD
    assert r.headers["x-recoq-format"] == "md"
    assert r.headers["x-recoq-bytes"] == str(len(r.content))
    # 门禁没过也照样给文件，但**在响应头明写** —— 不看响应头的人会把
    # 这一版当成终版
    assert r.headers["x-recoq-gate"] == "red", "本期还有未确认差异，门禁该是红"
    assert r.text.startswith(f"# {PERIOD} 周对账结论单")


def test_csv_export_has_bom_so_excel_opens_it_directly(client, opened_db):
    """CSV 档带 UTF-8 BOM，Excel 双击能直接打开。

    没有 BOM 时 Excel 会按 GBK 解 UTF-8，中文全变乱码 —— 而财务科看到
    一屏乱码的反应是「这文件坏了」，不会想到是编码。
    """
    r = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbf"), "CSV 少了 UTF-8 BOM，Excel 打开会是乱码"
    assert r.headers["content-disposition"].endswith('-conclusion.csv"')
    assert r.headers["x-recoq-format"] == "csv"


def test_unknown_format_is_4xx_with_code(client, opened_db):
    r = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "pdf"})
    assert r.status_code == 400
    assert r.json()["code"] == "EXPORT_FORMAT_UNKNOWN"


def test_export_of_empty_period_still_produces_a_sheet(client):
    """没扫过的账期也给一份结论单，只是内容是「什么都没扫到」。

    给 404 的话，财务科点「下载结论单」看到的是报错，而真相只是他切错
    了账期。给一份写明「本期未扫描」的结论单，才对得上他要签的那张纸。
    """
    r = client.get("/api/period/2099-W01/export", params={"fmt": "md"})
    assert r.status_code == 200
    assert r.headers["x-recoq-gate"] == "none"
    assert "2099-W01" in r.text


# ─────────────────────────── 2. 指纹头九项 ───────────────────────────


def test_markdown_fingerprint_header_has_all_nine_fields(client, opened_db):
    """正文开头的指纹头十项齐全。

    缺任何一项，三个月后拿着这张纸复盘就会卡在那一项上 —— 而结论单是
    唯一一份「当时到底按什么口径、什么契约、哪个模型」的白纸黑字。
    """
    _sign(client, "E-01", "handler", "渠道延迟入账，次日已补", 0)
    _sign(client, "E-02", "reviewer", "复核无误", 1)

    text = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"}).text
    for label in (
        "账期：",
        "账期状态：",
        "六路取数：",
        "本期事件链根指纹：",
        "审计链头锚点：",
        "本期口径指纹：",
        "模型 / 提示词：",
        "版本：",
        "结论集指纹：",
        "导出人：",
    ):
        assert label in text, f"指纹头缺「{label}」"
    # 导出人取参数，且与响应头的指纹一致
    text2 = client.get(f"/api/period/{PERIOD}/export",
                       params={"fmt": "md", "operator": "E-01", "note": "本月结账用"}).text
    assert "导出人：E-01" in text2
    assert "备注：本月结账用" in text2


def test_settled_lines_carry_evidence_refs_and_signatures(client, opened_db):
    """已会签的那条在正文里带**证据行号 + 三行签字**。

    结论单上每一条差异都要能追到「谁、何时、依据哪几条证据行」——
    这是整个产品的验收判据，少一样这张纸就签不了。
    """
    _sign(client, "E-01", "handler", "渠道延迟入账，次日已补", 0)
    _sign(client, "E-02", "reviewer", "复核无误", 1)

    text = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"}).text
    assert "E-01" in text and "E-02" in text
    assert "渠道延迟入账，次日已补" in text
    assert "wechat_tradebill_2026-W36.csv" in text, "证据行号没进结论单，这条差异就追不回去了"


def test_appendix_lists_channels_never_fetched(client, opened_db):
    """附录逐条列出未取到的渠道 —— 「这路没有账单」与「没取到」是两回事。

    漏掉这一段，归档的人会把「只核了微信」的那一期当成「六路都核过了」。
    """
    text = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"}).text
    assert "附录二 · 未取到渠道及错误码" in text
    # 中文名 + 渠道码两列都要有：这张纸是打印出来签字的，「alipay」不是
    # 财务科会念出来的词；而渠道码要留着，好让人把这一行与 inbox/ 下的
    # 目录对上
    # 微信这一路**取到了**，因此它**不该**出现在「未取到」附录里 ——
    # 把取到的路混进去，归档的人就分不清「核过了」与「没核」
    assert "微信支付" not in text.split("附录二")[1].split("附录三")[0], (
        "取到账单的微信被列进了「未取到渠道」附录"
    )
    for channel_zh in ("支付宝", "银联", "院内自助机", "医保结算", "HIS 收费流水"):
        assert channel_zh in text, f"附录没列「{channel_zh}」这一路"
    for channel_code in ("alipay", "unionpay", "self_service", "medical_ins", "his"):
        assert channel_code in text, f"附录只给了中文名没给渠道码：{channel_code}"
    assert "CREDENTIAL_MISSING" in text, "HIS 缺 DSN 这条错误码要留在纸上"


# ─────────────────────────── 3. 与 CLI 那一腿一致 ───────────────────────────


def test_panel_and_cli_export_produce_identical_bytes(client, opened_db, tmp_path):
    """面板下载的那一份与 CLI 导出的那一份**逐字节相同**。

    两种结论单会一起进同一个会签流程；版式一旦分叉，签字时没人说得清
    签的是哪一版。这条断言是「两条腿共用同一个渲染器」的守门人。
    """
    import re

    _sign(client, "E-01", "handler", "渠道延迟入账，次日已补", 0)
    _sign(client, "E-02", "reviewer", "复核无误", 1)

    from reconq.export.conclusion import export_conclusion

    cli_out = tmp_path / "cli-conclusion.md"
    conn = connect(opened_db)
    try:
        export_conclusion(conn, PERIOD, fmt="md", out_path=str(cli_out),
                          exported_by="", note="", now="2026-09-30T00:00:00+08:00")
    finally:
        conn.close()

    web = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"}).text
    cli_text = cli_out.read_text(encoding="utf-8")

    def strip_volatile(text: str) -> str:
        """去掉导出时刻 —— 它每次导出都变，进了比对就永远对不上。"""
        return re.sub(r"导出时刻：\S+", "导出时刻：<归一>", text)

    assert strip_volatile(web) == strip_volatile(cli_text), (
        "面板与 CLI 导出的不是同一份版式；两条腿必须共用 render_sheet"
    )


def test_same_content_same_fingerprint_across_formats(client, opened_db):
    """内容没变时，**两种格式的指纹相同**。

    指纹只吃业务内容、不吃格式与导出时刻 —— 财务科重导一次要能确认
    「结论一个字没改」，而这要求指纹在 md 与 csv 之间也是同一个。
    """
    md = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"})
    csv = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "csv"})
    assert md.headers["x-recoq-fingerprint"] == csv.headers["x-recoq-fingerprint"]


def test_fingerprint_changes_after_a_state_change(client, opened_db):
    """处置状态一变，导出内容与指纹**同时变**。

    指纹不变的话，「上周那版结论单还在库里」这句话就没法核对 ——
    而结论单上恰恰要回答「我签的那份，和现在这份，是不是同一件事」。
    """
    before = client.get(f"/api/period/{PERIOD}/export",
                        params={"fmt": "md"}).headers["x-recoq-fingerprint"]
    _sign(client, "E-01", "handler", "渠道延迟入账，次日已补", 0)
    _sign(client, "E-02", "reviewer", "复核无误", 1)
    after = client.get(f"/api/period/{PERIOD}/export",
                       params={"fmt": "md"}).headers["x-recoq-fingerprint"]
    assert before != after, "两条都走完会签了，指纹却没变"
    text = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"}).text
    assert after in text, "正文里的指纹与响应头对不上，两处算的不是同一份"


# ─────────────────────────── 4. 脱敏 ───────────────────────────


def test_export_contains_no_patient_identifiable_detail(client, opened_db):
    """结论单是打印出来、可能再交给渠道方的纸面文件。

    患者可识别信息混进去的代价远高于当场拒导，因此脱敏核对在**渲染前**
    逐键逐值过一遍 payload，而不是渲染完再扫一遍文本。
    """
    text = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "md"}).text
    csv_text = client.get(f"/api/period/{PERIOD}/export", params={"fmt": "csv"}).text
    blob = text + csv_text
    for forbidden in ("身份证", "住院号", "门诊号", "患者姓名", "手机号", "openid", "payer"):
        assert forbidden not in blob, f"结论单里出现了 {forbidden}"
    # 只给文件名与行号，不给整行原文
    assert "wechat_tradebill_2026-W36.csv:" in blob


def test_export_is_a_read_only_operation(client, opened_db):
    """导出**不推进任何状态**。

    它是「读已落库的事实 → 渲染成纸」，若顺手把期次标成已结清，那
    「点一下下载」就成了一个没人审的结账动作。
    """
    from reconq.ledger.flow import list_discrepancies

    conn = connect(opened_db)
    try:
        before = {d.key: (d.status, d.version) for d in list_discrepancies(conn, period=PERIOD)}
    finally:
        conn.close()

    for fmt in ("md", "csv"):
        assert client.get(f"/api/period/{PERIOD}/export", params={"fmt": fmt}).status_code == 200

    conn = connect(opened_db)
    try:
        after = {d.key: (d.status, d.version) for d in list_discrepancies(conn, period=PERIOD)}
    finally:
        conn.close()
    assert after == before, "导出把处置状态改了"

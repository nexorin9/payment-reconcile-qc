"""面板页面与静态资源（Task 49 · 三栏结构这一条）。

三栏骨架由 Jinja2 模板给出，静态资源由 FastAPI 挂载。这里只钉三件事：
页面**真的返回 200**、三栏**都在**、静态资源**取得到**。

为什么单独一个文件而不是并进端点测试
------------------------------------
页面挂掉和端点挂掉是两种故障：页面 500 是模板/静态目录的问题，而
``/api/periods`` 挂掉是业务投影的问题。混在一起时，前者会被一屏 JSON
异常盖住，而面板在浏览器里首先被看到的是那一屏页面。
"""

from __future__ import annotations

import re


def test_index_returns_200_and_renders_three_columns(client):
    r = client.get("/")
    assert r.status_code == 200, f"面板首页返回 {r.status_code}：{r.text[:400]}"
    body = r.text
    for col in ("col-channels", "col-diff", "col-detail"):
        assert col in body, f"三栏结构缺 {col}；页面渲出来是一张白纸"
    # 页面标题要说的是本产品，不是某个上游项目名
    assert "支付对账数据质检系统" in body


def test_page_carries_every_control_the_panel_needs(client):
    """控件齐全：账期选择、三栏容器、筛选栏、分组栏、待办、结论单下载。

    一条一条点 id 而不是笼统断言「有 JS」：缺 ``#diff-list`` 时页面照样
    返回 200、静态资源照样 200，只是中栏永远是空的 —— 那正是最难在
    端到端验收里看出来的一类缺失。
    """
    body = client.get("/").text
    for control in (
        'id="period"',          # 账期切换
        'id="channel-list"',    # 左栏容器
        'id="diff-list"',       # 中栏容器
        'id="group-list"',      # 分组栏
        'id="detail"',          # 右栏容器
        'id="todo-role"',       # 待办按角色切
        'id="f-reset"',         # 筛选清空
        'id="export-md"',       # 结论单下载
        'id="export-csv"',
    ):
        assert control in body, f"面板缺控件 {control}"


def test_static_assets_load(client):
    for path, marker in (("/static/panel.css", ".three-col"), ("/static/panel.js", "refreshAll")):
        r = client.get(path)
        assert r.status_code == 200, f"{path} 返回 {r.status_code}"
        assert marker in r.text, f"{path} 内容不对（找 {marker}）"
        assert r.headers["content-type"].startswith(("text/css", "text/javascript")), (
            f"{path} 的 content-type 是 {r.headers['content-type']}；"
            "浏览器会因此拒绝执行样式与脚本"
        )


def test_page_declares_charset_and_viewport(client):
    """中文账单与中文成因要靠 ``<meta charset>`` 才不乱码。

    这一条看着琐碎，但缺了它时页面**照常 200、照常有三栏**，只是金额与
    成因名变成乱码 —— 而乱码的成因名在截图里比缺控件更难被归因。
    """
    body = client.get("/").text
    assert re.search(r'<meta\s+charset="utf-8"', body, re.I)
    assert 'name="viewport"' in body


def test_openapi_schema_advertises_the_write_endpoints(client):
    """``/api/docs`` 上要能看到两个写入口。

    面板是财务科唯一入口，「有哪些按钮能改状态」不写进 OpenAPI 的后果是
    半年后没人说得清 decide 与 review 的区别 —— 而它们是本产品仅有的
    两条状态推进路径。
    """
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/discrepancy/{key}/decide" in paths
    assert "/api/discrepancy/{key}/review" in paths
    # 读端点也要在：只登记写端点的 schema 会让人以为面板只有写没有读
    assert "/api/period/{period}/discrepancies" in paths
    assert "/api/period/{period}/export" in paths

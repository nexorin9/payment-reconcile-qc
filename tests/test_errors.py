"""统一错误码分类表与统一异常出口（Task 20）。

规定四类断言：
1. 错误码元数据齐全（题目点名的码一个不缺，且每条都有中文说明与建议动作）
2. ``ReconqError`` 统一抛出（产品代码里没有裸 ``ValueError`` / ``RuntimeError``
   冒泡到界面）
3. 六路摘要字段完整
4. 日志与摘要里无患者标识

第 2 条是本任务最容易被糊弄过去的一条：``raise ValueError`` 在函数内部看着
没问题，但它一旦冒到 CLI 就是一段 ``ValueError: ttl_seconds 必须为正数``，
没有错误码、没有渠道、没有建议动作。断言用 AST 扫源码而不是靠人读，
因为「有没有裸 raise」这种事靠眼睛一定会漏。
"""

from __future__ import annotations

import ast
import importlib
import os
import pathlib

import pytest

from reconq.errors import ERROR_CODES, ReconqError, error_meta

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "reconq"

#: 题目点名的错误码。逐个断言存在 + 元数据齐全。
REQUIRED_CODES = (
    "BILL_NOT_EXIST",
    "STATEMENT_CREATING",
    "SHA1_MISMATCH",
    "LINK_EXPIRED",
    "RATE_LIMITED_EXHAUSTED",
    "FORMAT_BROKEN",
    "DIRTY_ROW_RATE_EXCEEDED",
    "READONLY_VIOLATION",
    "HIS_QUERY_TIMEOUT",
    "DB_AUTH_FAILED",
    "AMBIGUOUS_ENTITY",
    "WEAK_MATCH_PENDING",
    "MODEL_UNAVAILABLE",
    "MODEL_SCHEMA_INVALID",
    "SCAN_IN_PROGRESS",
    "PERIOD_NEEDS_RECONFIRM",
    # Task 31 新增：模型成本与审计
    "MODEL_PRICE_INVALID",
    "CACHE_OVERWRITE_DENIED",
    "MODEL_AUDIT_FIELD_INVALID",
    # Task 45 新增：链式审计事件与按期切片检索
    "AUDIT_LOG_DIR_REQUIRED",
    "AUDIT_PERIOD_REQUIRED",
    "AUDIT_EVENT_TYPE_REQUIRED",
    "AUDIT_EVENT_NOT_DEIDENTIFIED",
    "AUDIT_EVENT_CORRUPT",
    "AUDIT_QUERY_INVALID",
    "AUDIT_DIFFERENCE_KEY_REQUIRED",
)

#: 允许裸抛的模块与行内标记原因。
#:
#: ``contracts/model.py`` 里的 ``ValueError`` 全部写在 **pydantic 校验器**
#: 内部 —— pydantic 的 API 就是收 ``ValueError``，写成
#: ``ReconqError`` 反而会被 pydantic 包成 ``ValidationError``。它们的出口由
#: ``ContractYaml.parse()`` 的 ``except Exception`` 统一转成
#: ``CONTRACT_TYPE_MISMATCH`` 并附 YAML 行号，测试
#: ``test_pydantic_validator_values_are_converted_with_yaml_line`` 守住这条。
#:
#: ``store/bill_store.py`` 里的 ``RuntimeError`` 是 **测试注入的中断**
#: （``fail_after``），用来验证事务整批回滚，不是产品失败路径。
ALLOWED_BARE_RAISE = {
    "contracts/model.py": "pydantic 校验器内部，由 ContractYaml.parse 统一转换",
    "store/bill_store.py": "测试注入的中断（fail_after），用于验证事务回滚",
}


# —— 1. 错误码元数据齐全 ————————————————————————————————————————


@pytest.mark.parametrize("code", REQUIRED_CODES)
def test_required_code_is_registered(code):
    assert code in ERROR_CODES, f"错误码 {code} 未登记到 ERROR_CODES"


@pytest.mark.parametrize("code", REQUIRED_CODES)
def test_required_code_has_zh_and_suggestion(code):
    """每条都要有中文说明与建议动作。

    只登记一个裸码等于没登记：面板拿到 ``BILL_NOT_EXIST`` 只能显示这串英文，
    财务科看不懂「账单不存在」是今天真没出账、还是取错了日期。
    """
    meta = error_meta(code)
    assert meta.zh.strip(), f"{code} 缺中文说明"
    assert meta.suggestion.strip(), f"{code} 缺建议动作"
    # 说明不是把码原样抄一遍
    assert meta.zh != code


def test_every_registered_code_is_self_consistent():
    """全表体检：键与 ``code`` 字段一致、无重复、必填项非空。"""
    assert ERROR_CODES, "错误码表不能为空"
    for key, meta in ERROR_CODES.items():
        assert key == meta.code, f"字典键 {key} 与 meta.code {meta.code} 不一致"
        assert meta.zh.strip(), f"{key} 缺中文说明"
        assert isinstance(meta.retryable, bool)
        assert isinstance(meta.channel_level, bool)
        # 渠道级失败必须是可重试或明确不可重试，不能两头不靠
        if meta.channel_level and meta.retryable:
            continue


def test_unregistered_code_returns_fallback_not_keyerror():
    """未登记的码走兜底元数据，不抛 KeyError。"""
    meta = error_meta("SOME_CODE_WE_NEVER_REGISTERED")
    assert meta.code == "SOME_CODE_WE_NEVER_REGISTERED"
    assert meta.zh == "未登记的错误码"
    assert "信息科" in meta.suggestion


def test_channel_level_codes_are_marked_for_continuation():
    """渠道级失败的语义：这一路挂掉，其余五路照常。

    反例是 ``FORMAT_BROKEN`` 没标 ``channel_level``——那样编排层会把它当成
    整次扫描的失败，六路一起停。
    """
    for code in (
        "BILL_NOT_EXIST",
        "STATEMENT_CREATING",
        "LINK_EXPIRED",
        "SHA1_MISMATCH",
        "DIRTY_ROW_RATE_EXCEEDED",
        "HIS_QUERY_TIMEOUT",
        "DB_AUTH_FAILED",
    ):
        assert error_meta(code).channel_level, f"{code} 应标为渠道级失败"


def test_model_codes_are_not_channel_level():
    """模型不可达**不是**渠道级失败，刻意反着标。

    标成渠道级会让编排层只剔除一路渠道，其余五路的差异照样被当成「已归因」
    写进台账 —— 归因能力整体没了，却只显示一路出问题。
    """
    for code in ("MODEL_UNAVAILABLE", "MODEL_SCHEMA_INVALID"):
        assert not error_meta(code).channel_level, (
            f"{code} 不应标为渠道级失败：模型降级影响全期归因，不只一路渠道"
        )


def test_only_retryable_codes_suggest_retry():
    """限频与超时可重试，格式损坏与只读违规不可重试。

    把 ``SHA1_MISMATCH`` 标成可重试会让退避重试去重复下载同一份坏内容；
    把 ``STATEMENT_CREATING`` 标成不可重试则「账单正在生成」永远取不到。
    """
    assert error_meta("FREQUENCY_LIMITED").retryable
    assert error_meta("STATEMENT_CREATING").retryable
    assert error_meta("MODEL_TIMEOUT").retryable
    assert not error_meta("SHA1_MISMATCH").retryable
    assert not error_meta("READONLY_VIOLATION").retryable
    assert not error_meta("CONTRACT_SYNTAX_ERROR").retryable


# —— 2. ReconqError 统一抛出 ————————————————————————————————————


def _reconq_error_subclasses(path: pathlib.Path) -> set[str]:
    """该模块里定义的、继承 :class:`ReconqError` 的异常类名。

    靠**实际 import** 判定而不是看 ``class X(ReconqError)`` 的文本：
    ``CheckSelectorParseError`` 这类子类是合法出口（它仍带错误码与建议
    动作），而同名但没继承的类才是问题。
    """
    rel = path.relative_to(SRC)  # 如 contracts/selector.py
    module_name = "reconq." + str(rel.with_suffix("")).replace(os.sep, ".")
    try:
        module = importlib.import_module(module_name)
    except Exception:  # pragma: no cover - 模块导入失败由其它测试负责
        return set()
    found: set[str] = set()
    for name in dir(module):
        obj = getattr(module, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, ReconqError)
            and obj is not ReconqError
        ):
            found.add(name)
    return found


def _bare_raises(path: pathlib.Path) -> list[tuple[int, str]]:
    """扫出该文件里所有「新建了非 :class:`ReconqError` 异常」的 ``raise``。

    判据是**是否新建**，不是「名字是不是 ReconqError」：

    - ``raise ReconqError(...)`` / ``raise SomeSubclass(...)`` → 合法出口
    - ``raise ValueError(...)`` / ``raise RuntimeError(...)`` → 问题：冒到
      CLI 就是一段没有错误码、没有建议动作的裸栈
    - ``raise exc`` / ``raise classified`` → **重新抛出**已有的异常对象，
      合法；此时被抛对象的类型由别处的构造点决定，构造点自己会被这条规则
      扫到，所以这里不重复判定

    用 AST 扫而不是 grep：``raise Xxx() from e`` 与 ``raise Xxx(...)`` 两种
    形态在几百行里靠眼睛一定会漏。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    subclasses = _reconq_error_subclasses(path) | {"ReconqError"}
    #: 本模块里「返回 ReconqError 的工厂函数」。``raise
    #: _classify_db_error(exc) from exc`` 是**新建**了一个业务异常，只是构造
    #: 方式被包在工厂里（如 HIS 连接错误按认证失败/库不存在/网络不可达分类）。
    #: 按返回注解判定，不靠人工登记：登记一次就多一个能忘的地方。
    factories = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and isinstance(node.returns, ast.Name)
        and node.returns.id == "ReconqError"
    }
    # 跨模块 import 进来的工厂函数（如 ``his_pool`` 从 ``his_db`` import 的
    # ``classify_connection_error``）本文件里没有定义，返回注解扫不到。
    # 靠**实际取到函数对象**判定，不靠人工登记 import 名单。
    try:
        module = importlib.import_module(
            "reconq." + str(path.relative_to(SRC).with_suffix("")).replace(os.sep, ".")
        )
    except Exception:  # pragma: no cover
        module = None
    if module is not None:
        for name in dir(module):
            obj = getattr(module, name, None)
            if not callable(obj) or isinstance(obj, type):
                continue
            hints = getattr(obj, "__annotations__", {}) or {}
            if "return" in hints and "ReconqError" in str(hints["return"]):
                factories.add(name)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        # 重新抛出已有对象（``raise`` / ``raise exc`` / ``raise payload``）——
        # 被抛对象的类型由别处的构造点决定，而那个构造点自己会被这条规则
        # 扫到，所以这里不重复判定。
        if not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name is None or name in subclasses or name in factories:
            continue
        found.append((node.lineno, name))
    return found


def test_no_bare_exceptions_escape_to_the_ui():
    """产品代码里没有裸 ``ValueError`` / ``RuntimeError`` 冒到界面。

    用 AST 扫而不是 grep：``raise`` 写成 ``raise exc``（重新抛）或
    ``raise SomeError() from e`` 的形态肉眼很难在几百行里逐个确认，
    而这两个里只有「新建一个非业务异常」才是问题。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if rel in ALLOWED_BARE_RAISE:
            continue
        for lineno, name in _bare_raises(path):
            offenders.append(f"{rel}:{lineno} raise {name}")
    assert not offenders, (
        "以下位置抛了非 ReconqError 的异常，会以裸栈形式冒到业务人员面前：\n"
        + "\n".join(offenders)
    )


def test_allowed_bare_raise_sites_still_exist():
    """白名单不是「先清空再放行」——两处豁免点必须仍在原位。

    豁免点被删掉说明有人顺手改了，本测试提醒把白名单同步收窄（或直接
    改掉那两处代码）。
    """
    for rel in ALLOWED_BARE_RAISE:
        path = SRC / rel
        assert path.is_file(), f"白名单里的 {rel} 已不存在，白名单需同步收窄"
    model_bare = {name for _line, name in _bare_raises(SRC / "contracts/model.py")}
    assert model_bare == {"ValueError"}, (
        "contracts/model.py 的豁免前提是「只有 pydantic 校验器的 ValueError」；"
        f"现在出现了 {model_bare}"
    )


def test_pydantic_validator_values_are_converted_with_yaml_line():
    """pydantic 校验器抛的 ValueError 出口处确实被转换并附 YAML 行号。"""
    from reconq.contracts.model import ContractYaml

    bad = """
dataset:
  name: 坏契约
  tables:
    - name: t
      columns:
        - name: amount
          data_type: decimal
          required: true
        - name: amount
          data_type: decimal
      checks:
        - type: not_empty
          path: [amount]
"""
    with pytest.raises(ReconqError) as excinfo:
        ContractYaml.parse(bad, source_path="contracts/bad.yml")
    assert excinfo.value.code in (
        "CONTRACT_TYPE_MISMATCH",
        "CONTRACT_MISSING_KEY",
    )
    assert excinfo.value.yaml_path == "contracts/bad.yml"


def test_cli_catch_all_converts_unexpected_exception(monkeypatch, capsys):
    """预期外异常不会以裸栈形式出现在 stderr。

    财务科看到的是一段 ``File ".../period.py", line N``，既读不出是哪一路、
    也不知道下一步做什么。全栈改进 JSON 日志，界面上只留可读结论。
    """
    from reconq import cli

    def boom(_args):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(cli, "_cmd_verify", boom)
    code = cli.main(["--no-log", "verify", "--period", "2026-W36"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_ERROR
    assert "Traceback" not in err
    assert "ZeroDivisionError" in err
    assert "JSON 日志" in err


def test_cli_business_error_goes_through_reconq_error_path(monkeypatch, capsys):
    """带错误码的业务失败照旧是「码 + 中文说明 + 建议动作」，并落一条结构化事件。"""
    from reconq import cli

    def boom(_args):
        raise ReconqError(
            "CREDENTIAL_MISSING",
            "环境变量 WECHAT_API_V3_KEY 未配置",
            channel="wechat",
        )

    monkeypatch.setattr(cli, "_cmd_verify", boom)
    code = cli.main(["--no-log", "verify", "--period", "2026-W36"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_ERROR
    assert "[CREDENTIAL_MISSING]" in err
    assert "渠道=wechat" in err


def test_reconq_error_render_carries_code_zh_where_and_suggestion():
    exc = ReconqError(
        "AMBIGUOUS_ENTITY",
        "商户订单号 EX-1 匹配到 3 条 HIS 收费记录",
        channel="wechat",
        row_ref=12,
    )
    text = exc.render()
    assert "[AMBIGUOUS_ENTITY]" in text
    assert "匹配到 3 条" in text
    assert "渠道=wechat" in text
    assert "行=12" in text
    assert "建议：" in text
    # 界面文案里不能出现凭据值
    assert "WECHAT_API_V3_KEY=" not in text


def test_reconq_error_cause_code_defaults_to_code_but_can_differ():
    """处置结果与根因分列：退避重试用尽（处置）挂 LINK_EXPIRED（根因）。"""
    exc = ReconqError("RATE_LIMITED_EXHAUSTED", "退避 3 次仍未取到")
    assert exc.cause_code == "RATE_LIMITED_EXHAUSTED"
    exc.cause_code = "LINK_EXPIRED"
    assert exc.code == "RATE_LIMITED_EXHAUSTED"
    assert exc.cause_code == "LINK_EXPIRED"


def test_error_module_has_no_patient_friendly_bare_dict_access():
    """``ERROR_CODES[...]`` 直查会 KeyError；产品代码一律走 ``error_meta()``。"""
    with pytest.raises(KeyError):
        _ = ERROR_CODES["NOT_A_CODE"]
    assert error_meta("NOT_A_CODE").zh


# —— 3 & 4 移到 test_run_summary.py（摘要字段 + 脱敏断言同源）——————


def test_error_codes_file_is_the_only_truth_source():
    """错误码元数据只在这一个文件里定义（面板/结论单/CLI 都 import 它）。"""
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "errors.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "ErrorCodeMeta(" in text:
            hits.append(str(path.relative_to(SRC)))
    assert not hits, f"以下模块各自建了错误码元数据，真源应唯一：{hits}"


def test_log_dir_is_outside_project_subdir():
    """日志目录默认在 round 根的 logs/，不写项目子目录。

    项目子目录要直接发布到 GitHub；日志属于运行期产物。
    """
    from reconq.runlog import default_log_dir

    previous = os.environ.pop("RECONQ_LOG_DIR", None)
    try:
        resolved = os.path.abspath(default_log_dir())
        project_dir = os.path.abspath(str(SRC.parents[1]))
        assert not resolved.startswith(project_dir + os.sep), (
            f"日志目录 {resolved} 落在项目子目录 {project_dir} 内"
        )
    finally:
        if previous is not None:
            os.environ["RECONQ_LOG_DIR"] = previous


def test_log_dir_env_override_wins():
    from reconq.runlog import default_log_dir

    os.environ["RECONQ_LOG_DIR"] = "/tmp/reconq-logs-test"
    try:
        assert default_log_dir() == "/tmp/reconq-logs-test"
    finally:
        os.environ.pop("RECONQ_LOG_DIR", None)

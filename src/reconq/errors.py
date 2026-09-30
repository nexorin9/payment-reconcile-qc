"""统一错误码与异常类型。

设计口径：任何面向用户的失败都必须是**可读错误码 + 中文说明 + 出处**，
不允许裸 ``Exception`` 冒泡到 CLI 面板。本轮（Task 2）先落契约引擎相关
的错误码与异常骨架，Task 20 补全渠道/网关/台账全量码表。

源产品对应：``soda-core/src/soda_core/cli/cli.py`` 用 ``exit_with_code()``
把 ``ExitCode`` 映射为进程退出码；本产品把同一语义改写为 ``ReconqError``：
调用方拿得到码、中文说明与出错位置，而不是一个数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "ErrorCodeMeta",
    "ERROR_CODES",
    "ReconqError",
    "error_meta",
]


@dataclass(frozen=True)
class ErrorCodeMeta:
    """一个错误码的元数据：码、中文说明、是否可重试、归属层级、建议动作。"""

    code: str
    zh: str
    retryable: bool = False
    channel_level: bool = False
    suggestion: str = ""


#: 契约引擎相关错误码。渠道 / 网关 / 台账码在后续任务补入同一张表。
ERROR_CODES: dict[str, ErrorCodeMeta] = {
    # —— 契约加载与解析 ——
    "CONTRACT_SYNTAX_ERROR": ErrorCodeMeta(
        code="CONTRACT_SYNTAX_ERROR",
        zh="契约 YAML 语法错误",
        suggestion="按提示的行号列号检查缩进与引号，修正后重新执行",
    ),
    "CONTRACT_MISSING_KEY": ErrorCodeMeta(
        code="CONTRACT_MISSING_KEY",
        zh="契约缺少必填键",
        suggestion="对照同渠道其他契约补齐该键；本产品不会用默认值蒙混",
    ),
    "CONTRACT_TYPE_MISMATCH": ErrorCodeMeta(
        code="CONTRACT_TYPE_MISMATCH",
        zh="契约字段类型与预期不符",
        suggestion="例如 columns 必须是列表、path 必须是列表或点分字符串",
    ),
    "CONTRACT_UNKNOWN_KEY": ErrorCodeMeta(
        code="CONTRACT_UNKNOWN_KEY",
        zh="契约出现未知键（疑似拼写错误）",
        suggestion="确认键名拼写；未定义的键不会被静默忽略",
    ),
    "CHECK_PATH_INVALID": ErrorCodeMeta(
        code="CHECK_PATH_INVALID",
        zh="check.path 非法",
        suggestion="dataset 级 check 的 path 至少一段；每段只能为字母数字下划线",
    ),
    "CHECK_TYPE_MISSING": ErrorCodeMeta(
        code="CHECK_TYPE_MISSING",
        zh="check 缺少 type",
        suggestion="type 决定由哪个算子执行，写在 check 顶层或 properties 内均可",
    ),
    "VARIABLE_MISSING": ErrorCodeMeta(
        code="VARIABLE_MISSING",
        zh="契约引用了未提供的变量",
        suggestion="用 --set 名称=值 注入，或在契约 variables 段给出默认值",
    ),
    "VARIABLE_BAD_FORMAT": ErrorCodeMeta(
        code="VARIABLE_BAD_FORMAT",
        zh="变量赋值格式错误",
        suggestion="变量必须写成 名称=值，且名称与值均非空",
    ),
    "CHECK_SELECTOR_INVALID": ErrorCodeMeta(
        code="CHECK_SELECTOR_INVALID",
        zh="check 选择器表达式非法",
        suggestion="写法为 key=value，可逗号分隔多条；多个 --check-filter 之间为并集",
    ),
    # —— check 执行与文件通道（Task 3 最小切片）——
    "CHECK_OPERATOR_UNSUPPORTED": ErrorCodeMeta(
        code="CHECK_OPERATOR_UNSUPPORTED",
        zh="该口径使用的算子本轮尚未实现",
        suggestion="未实现的算子不会被静默当作通过；用 --check 先只跑已实现的算子，"
        "或等待该算子在后续任务落地",
    ),
    "BILL_FILE_NOT_FOUND": ErrorCodeMeta(
        code="BILL_FILE_NOT_FOUND",
        zh="账单文件不存在或不可读",
        suggestion="确认 --source 指向的目录里有 <period>/<channel>/ 的账单导出文件",
    ),
    "BILL_ENCODING_UNRESOLVED": ErrorCodeMeta(
        code="BILL_ENCODING_UNRESOLVED",
        zh="账单文件编码无法识别",
        suggestion="请让信息科按 UTF-8 或 GBK 重新导出账单",
    ),
    "BILL_COLUMN_MISSING": ErrorCodeMeta(
        code="BILL_COLUMN_MISSING",
        zh="账单表头与契约列名对不上",
        suggestion="对照错误详情里的实际表头与契约列，补 alias 映射或改用正确导出版本",
    ),
    "AMOUNT_UNPARSABLE": ErrorCodeMeta(
        code="AMOUNT_UNPARSABLE",
        zh="金额列的值不是合法数字",
        suggestion="金额一律按定点数处理，出现非数字通常是导出时带了单位或千分位以外的符号",
    ),
    "FORMAT_BROKEN": ErrorCodeMeta(
        code="FORMAT_BROKEN",
        zh="账单文件结构损坏",
        suggestion="让导出方重新导出该账期账单；本产品不会拿半截文件对账",
    ),
    # —— 连接属性与时区归一（Task 4）——
    "CREDENTIAL_MISSING": ErrorCodeMeta(
        code="CREDENTIAL_MISSING",
        zh="必填凭据位未配置",
        suggestion="按报错点名的环境变量名配置；凭据只从环境变量读，不落在配置文件里",
    ),
    "CHANNEL_UNKNOWN": ErrorCodeMeta(
        code="CHANNEL_UNKNOWN",
        zh="未知渠道",
        suggestion="渠道名须在已登记的六路之内（wechat/alipay/unionpay/self_service/his/medical_ins）",
    ),
    "TIMEZONE_UNRESOLVED": ErrorCodeMeta(
        code="TIMEZONE_UNRESOLVED",
        zh="会话时区为空或未配置",
        suggestion="显式配置时区（如 RECONQ_SESSION_TZ=Asia/Shanghai）；"
        "本产品拒绝静默按 UTC 切账期，避免把交易时间默默挪 8 小时",
    ),
    "TIMEZONE_INVALID": ErrorCodeMeta(
        code="TIMEZONE_INVALID",
        zh="会话时区无法解析或越界",
        suggestion="支持 IANA 名（Asia/Shanghai）、数值偏移（+08:00）、UTC/GMT/Z、localtime；"
        "不接受 +05:99 这类越界值",
    ),
    "PERIOD_INVALID": ErrorCodeMeta(
        code="PERIOD_INVALID",
        zh="账期格式非法",
        suggestion="账期写作 2026-W36 这样的 ISO 周格式，周次范围 1–53",
    ),
    # —— 渠道账单取数（Task 5）——
    "BILL_DATE_INVALID": ErrorCodeMeta(
        code="BILL_DATE_INVALID",
        zh="账单日期格式非法",
        suggestion="渠道接口按**自然日**取账单，日期写作 YYYY-MM-DD；账期 2026-W36 会先换算成账单日",
    ),
    "CHANNEL_REQUEST_FAILED": ErrorCodeMeta(
        code="CHANNEL_REQUEST_FAILED",
        zh="渠道接口请求失败",
        suggestion="按详情里的 HTTP 状态码与响应体 code 排查；本产品不会拿错误响应当账单对账",
    ),
    "BILL_NOT_EXIST": ErrorCodeMeta(
        code="BILL_NOT_EXIST",
        zh="渠道侧没有该账单日的账单",
        suggestion="确认该渠道该账单日是否已出账（微信/支付宝账单通常次日早间才可取）",
        channel_level=True,
    ),
    "STATEMENT_CREATING": ErrorCodeMeta(
        code="STATEMENT_CREATING",
        zh="渠道账单正在生成",
        suggestion="稍后重试该渠道；本期扫描可先跑其余五路",
        retryable=True,
        channel_level=True,
    ),
    "FREQUENCY_LIMITED": ErrorCodeMeta(
        code="FREQUENCY_LIMITED",
        zh="渠道接口限频",
        suggestion="已按指数退避重试；仍被限说明本轮取件过密，请缩小并发或延后到次日",
        retryable=True,
        channel_level=True,
    ),
    "RATE_LIMITED_EXHAUSTED": ErrorCodeMeta(
        code="RATE_LIMITED_EXHAUSTED",
        zh="退避重试用尽仍未取到账单",
        suggestion="退避次数已达上限，不硬重试；该渠道本期标未取到，其余五路照常",
        channel_level=True,
    ),
    "LINK_EXPIRED": ErrorCodeMeta(
        code="LINK_EXPIRED",
        zh="账单下载链接已过期",
        suggestion="下载链接是极短有效期的取件凭证（微信 5 分钟 / 支付宝 30 秒）；"
        "必须取得即下载，过期后重新取件而不是重试旧链接",
        retryable=True,
        channel_level=True,
    ),
    "BILL_DOWNLOAD_FAILED": ErrorCodeMeta(
        code="BILL_DOWNLOAD_FAILED",
        zh="账单文件下载失败",
        suggestion="按详情里的 HTTP 状态码排查；不下载到内容就不入库",
        channel_level=True,
    ),
    "SHA1_MISMATCH": ErrorCodeMeta(
        code="SHA1_MISMATCH",
        zh="账单文件 SHA1 校验不符",
        suggestion="下载内容与渠道声明的 hash 不一致（多半是下载被截断或中间设备改动）；"
        "拒绝入库，重新取件一次",
        channel_level=True,
    ),
    "BILL_DECRYPT_FAILED": ErrorCodeMeta(
        code="BILL_DECRYPT_FAILED",
        zh="账单文件解密失败",
        suggestion="账单以 APIv3 密钥做 AES-256-GCM 加密；确认 WECHAT_API_V3_KEY 与商户一致",
        channel_level=True,
    ),
    "FORMAT_AMBIGUOUS": ErrorCodeMeta(
        code="FORMAT_AMBIGUOUS",
        zh="账单文件分隔符无法判定",
        suggestion="多个分隔符切出的列数打平，本产品拒绝猜；请让信息科按 CSV 重新导出",
    ),
    "COLUMN_COUNT_MISMATCH": ErrorCodeMeta(
        code="COLUMN_COUNT_MISMATCH",
        zh="账单数据行列数与表头不一致",
        suggestion="错误详情里给了行号与实际列数；该行是脏行，不会被静默丢弃",
        channel_level=True,
    ),
    # —— HIS 只读库流式取数（Task 6）——
    "HIS_DB_DSN_MISSING": ErrorCodeMeta(
        code="HIS_DB_DSN_MISSING",
        zh="HIS 只读库连接串未配置",
        suggestion="设置环境变量 HIS_DB_DSN（本产品只读，绝不写 HIS 账）；"
        "连接串须使用只读账号",
        channel_level=True,
    ),
    "HIS_DB_AUTH_FAILED": ErrorCodeMeta(
        code="HIS_DB_AUTH_FAILED",
        zh="HIS 只读库认证失败",
        suggestion="核对 HIS_DB_DSN 里的账号密码，以及该账号是否已被授予目标库的只读权限",
        channel_level=True,
    ),
    "HIS_DB_NOT_EXIST": ErrorCodeMeta(
        code="HIS_DB_NOT_EXIST",
        zh="HIS 库或其中的收费表不存在",
        suggestion="核对 HIS_DB_DSN 里的库名；院方库名常见 HIS/CHARGE，"
        "先由信息科确认本院实际库表命名",
        channel_level=True,
    ),
    "HIS_DB_UNREACHABLE": ErrorCodeMeta(
        code="HIS_DB_UNREACHABLE",
        zh="HIS 只读库网络不可达",
        suggestion="确认本机到 HIS 数据库网段是否放行；对账服务常部署在财务科内网，"
        "跨网段需申请白名单",
        channel_level=True,
    ),
    "HIS_DB_DRIVER_MISSING": ErrorCodeMeta(
        code="HIS_DB_DRIVER_MISSING",
        zh="缺少该 DSN 对应的数据库驱动",
        suggestion="PostgreSQL 装 psycopg、MySQL 装 pymysql；"
        "驱动没装时本产品不会偷偷改用别的方言去连",
        channel_level=True,
    ),
    # —— 渠道账单落库（Task 7）——
    "BILL_ORDER_NO_MISSING": ErrorCodeMeta(
        code="BILL_ORDER_NO_MISSING",
        zh="账单行没有渠道单号，无法进入幂等唯一键",
        suggestion="空单号会让同批所有缺单行互相冲突（症状是「20 行只进 1 行」）；"
        "请让导出方补齐商户订单号列，本产品不会替它编一个单号",
        channel_level=True,
    ),
    "BILL_DATE_MISSING": ErrorCodeMeta(
        code="BILL_DATE_MISSING",
        zh="账单行既无账单日期也无交易时间",
        suggestion="账单日期是幂等键的一部分；请让导出方提供账单日期或交易时间列",
        channel_level=True,
    ),
    "TIMESTAMP_UNPARSABLE": ErrorCodeMeta(
        code="TIMESTAMP_UNPARSABLE",
        zh="交易时间无法解析",
        suggestion="支持 ISO 8601 与 yyyy-MM-dd HH:mm:ss 等写法；"
        "请让导出方按带秒的完整时间导出，不要只给日期",
        channel_level=True,
    ),
    "AMOUNT_FLOAT_REJECTED": ErrorCodeMeta(
        code="AMOUNT_FLOAT_REJECTED",
        zh="金额以 float 传入落库层被拒绝",
        suggestion="float 一旦进来就再也追不回原值（0.1 在二进制里不是 0.1）；"
        "请在取数阶段就解析成 Decimal 定点数",
    ),
    "HIS_STREAM_UNSUPPORTED": ErrorCodeMeta(
        code="HIS_STREAM_UNSUPPORTED",
        zh="该 HIS 方言不支持服务端游标，无法按字节预算流式取数",
        suggestion="该方言会退回客户端缓冲游标并显式告警："
        "峰值内存将升到整批结果集；建议改走 PostgreSQL 侧的服务端游标",
        channel_level=True,
    ),
    # —— 渠道签名与响应验签（Task 8）——
    "RESPONSE_SIGNATURE_INVALID": ErrorCodeMeta(
        code="RESPONSE_SIGNATURE_INVALID",
        zh="渠道响应验签失败",
        suggestion="验签不过说明响应内容在传输中被改动，或平台证书/支付宝公钥不是当前那个。"
        "本产品不提供「只签名不验签」模式：宁可这一路标未取到，也不拿未验签的下载链接对账",
        channel_level=True,
    ),
    "PRIVATE_KEY_UNREADABLE": ErrorCodeMeta(
        code="PRIVATE_KEY_UNREADABLE",
        zh="商户私钥文件不可读或 PEM 解析失败",
        suggestion="确认环境变量指向的文件路径正确、权限可读、且是 PKCS#8 或 PKCS#1 的 PEM；"
        "报错信息里会带出具体是哪个变量名",
        channel_level=True,
    ),
    # —— 下载链接有效期窗口（Task 9）——
    "LINK_ALREADY_USED": ErrorCodeMeta(
        code="LINK_ALREADY_USED",
        zh="账单下载链接被重复使用",
        suggestion="download_url / bill_url 是一次性取件凭证（微信 5 分钟 / 支付宝 30 秒），"
        "不能当稳定地址缓存；请在取得后立即下载一次",
        channel_level=True,
    ),
    "BILL_CONTENT_MISMATCH": ErrorCodeMeta(
        code="BILL_CONTENT_MISMATCH",
        zh="下载到的账单内容与请求的账单日对不上",
        suggestion="多半是渠道把上一期文件返回、链接失效返回了错误页或空响应体；"
        "本产品拒绝入库，重新取件而不是把错文件拿去对账",
        channel_level=True,
    ),
    "CHANNEL_NOT_FETCHED": ErrorCodeMeta(
        code="CHANNEL_NOT_FETCHED",
        zh="该渠道本期未取到账单",
        suggestion="重新取件额度已用尽；本期该路标未取到，其余五路照常，"
        "真实成因见 cause_code",
        channel_level=True,
    ),
    "SIGN_ERROR": ErrorCodeMeta(
        code="SIGN_ERROR",
        zh="渠道拒绝了本产品的请求签名",
        suggestion="微信：检查签名串是否含完整 query、是否五行齐全、nonce 是否复用；"
        "支付宝：检查参数是否剔空、是否按字典序、biz_content 的 JSON 序列化是否稳定",
        channel_level=True,
    ),
    # —— 脏行隔离区（Task 11）——
    "DIRTY_REASON_UNKNOWN": ErrorCodeMeta(
        code="DIRTY_REASON_UNKNOWN",
        zh="未登记的脏行原因码",
        suggestion="原因码必须是受控集合（errors.py / quarantine.REASON_CODES），"
        "自由文本会让脏行无法按成因聚合统计",
    ),
    "DIRTY_ROW_RATE_EXCEEDED": ErrorCodeMeta(
        code="DIRTY_ROW_RATE_EXCEEDED",
        zh="脏行率超过阈值，整份文件判为结构损坏",
        suggestion="脏行成片出现说明问题在**导出方式**（列错位、分隔符猜错、编码坏了），"
        "不在那几行数据上；此时干净行不入库，该渠道本期标未取到，"
        "其余五路照常。请让导出方重导该账期并看隔离台账里的行号",
        channel_level=True,
    ),
    "QUARANTINE_NOT_INITIALIZED": ErrorCodeMeta(
        code="QUARANTINE_NOT_INITIALIZED",
        zh="隔离区尚未建表",
        suggestion="先跑一次 reconq quarantine scan（会建表并写入隔离记录），"
        "或用 reconq.store.quarantine.init_dirty_schema() 建表",
    ),
    # —— HIS 只读连接保护（Task 12）——
    "READONLY_VIOLATION": ErrorCodeMeta(
        code="READONLY_VIOLATION",
        zh="拦到写 HIS 的语句",
        suggestion="本产品只读 HIS 账，这条语句在发到数据库之前就被拦下了。"
        "要改 HIS 数据请走院内既有的收费/退费流程，不要在对账侧改",
    ),
    "HIS_SELECT_STAR_REJECTED": ErrorCodeMeta(
        code="HIS_SELECT_STAR_REJECTED",
        zh="拒绝 SELECT *",
        suggestion="院内生产库上 SELECT * 会把契约用不到的列也拖过来，"
        "多一份 IO 也多一分撞慢查询的机会；请显式列出所需列",
    ),
    "HIS_READONLY_ASSERT_FAILED": ErrorCodeMeta(
        code="HIS_READONLY_ASSERT_FAILED",
        zh="连接建立后未进入只读事务，拒绝取数",
        suggestion="请确认 HIS_DB_DSN 用的账号确实只有该库的只读权限；"
        "若该方言的只读设置未生效，请由信息科补上只读设置语句。"
        "本产品宁可这一路标未取到，也不以读写身份连上生产库",
        channel_level=True,
    ),
    "HIS_READONLY_UNSUPPORTED": ErrorCodeMeta(
        code="HIS_READONLY_UNSUPPORTED",
        zh="该方言未登记只读设置与只读探针",
        suggestion="未登记意味着本产品无法证明这条连接是只读的，因此拒绝发业务查询；"
        "请信息科确认该库的只读接入方式后补上对应方言的设置与探针语句",
        channel_level=True,
    ),
    "HIS_QUERY_TIMEOUT": ErrorCodeMeta(
        code="HIS_QUERY_TIMEOUT",
        zh="HIS 只读查询超时",
        suggestion="该渠道本期标未取到，其余五路照常；"
        "请核对账期跨度过大或 charge_time 缺索引，"
        "必要时调大 HIS_DB_QUERY_TIMEOUT_SECONDS 或 HIS_DB_STATEMENT_TIMEOUT_MS",
        channel_level=True,
    ),
    "HIS_POOL_CONFIG_INVALID": ErrorCodeMeta(
        code="HIS_POOL_CONFIG_INVALID",
        zh="连接池配置项非法",
        suggestion="HIS_DB_POOL_SIZE / HIS_DB_POOL_TIMEOUT / "
        "HIS_DB_QUERY_TIMEOUT_SECONDS 必须是正数，值不合法时本产品不会用一个"
        "自己猜的默认值去连生产库",
        channel_level=True,
    ),
    "HIS_SESSION_CLOSED": ErrorCodeMeta(
        code="HIS_SESSION_CLOSED",
        zh="只读会话已关闭后仍被使用",
        suggestion="本产品按需建连、逐账期用完即关；请重新取一条会话再取数",
        channel_level=True,
    ),
    "HIS_POOL_TIMEOUT": ErrorCodeMeta(
        code="HIS_POOL_TIMEOUT",
        zh="HIS 连接池取连接超时",
        suggestion="池已被占满且新连接建不起来；请核对是否有上一次扫描未正常结束，"
        "或调大 HIS_DB_POOL_SIZE（注意院内 HIS 的 max_connections 余量）",
        channel_level=True,
    ),
    # —— 契约静态校验（Task 16）——
    # —— 契约指纹、快照与回归基线（Task 17）——
    "CONTRACT_SNAPSHOT_NOT_INITIALIZED": ErrorCodeMeta(
        code="CONTRACT_SNAPSHOT_NOT_INITIALIZED",
        zh="契约快照表尚未建表",
        suggestion="先跑一次 reconq contract snapshot（会建表并记录当前契约指纹），"
        "或用 reconq.store.snapshots.init_snapshot_schema() 建表",
    ),
    "CONTRACT_SNAPSHOT_NOT_FOUND": ErrorCodeMeta(
        code="CONTRACT_SNAPSHOT_NOT_FOUND",
        zh="指定的契约快照不存在",
        suggestion="用 reconq contract snapshot --list 查看该渠道有哪些快照",
    ),
    "CONTRACT_BASELINE_NOT_INITIALIZED": ErrorCodeMeta(
        code="CONTRACT_BASELINE_NOT_INITIALIZED",
        zh="回归基线表尚未建表，或记录基线时未给账期",
        suggestion="基线的键是「账期 + 渠道 + check path」；"
        "先跑 reconq contract baseline --period <账期> --channel <渠道> 落一条基线",
    ),
    "PERIOD_BINDING_NOT_INITIALIZED": ErrorCodeMeta(
        code="PERIOD_BINDING_NOT_INITIALIZED",
        zh="账期契约绑定表尚未建表，或该账期渠道尚无绑定记录",
        suggestion="用 reconq.store.snapshots.init_snapshot_schema() 建表；"
        "绑定记录由扫描时写入，确认状态只能人工触发",
    ),
    "PERIOD_NEEDS_RECONFIRM": ErrorCodeMeta(
        code="PERIOD_NEEDS_RECONFIRM",
        zh="本期口径已变更，结论需重新确认",
        suggestion="口径指纹变了，上一版口径下的差异结论不再自动有效；"
        "经办人逐条复核后由主管确认，系统不会自动沿用旧结论",
    ),
    # —— 账期扫描实例状态机（Task 18）——
    "PERIOD_SCAN_NOT_INITIALIZED": ErrorCodeMeta(
        code="PERIOD_SCAN_NOT_INITIALIZED",
        zh="该账期尚无扫描实例记录",
        suggestion="先跑 reconq scan --period <账期>；扫描实例是「这期账现在到哪一步」的"
        "唯一权威，缺了它面板与结论单都无法回答能不能结账",
    ),
    "PERIOD_STATE_INVALID": ErrorCodeMeta(
        code="PERIOD_STATE_INVALID",
        zh="目标状态不是合法状态",
        suggestion="合法状态：unscanned（未扫描）/ scanning（扫描中）/ "
        "pending_confirm（待确认）/ confirmed（已确认）/ disputed（已存疑）",
    ),
    "PERIOD_STATE_TRANSITION_DENIED": ErrorCodeMeta(
        code="PERIOD_STATE_TRANSITION_DENIED",
        zh="账期扫描实例的状态迁移被白名单拒绝",
        suggestion="状态迁移不是随便设值：没扫过不能说已确认，已确认不能说没扫过。"
        "要重扫一个已确认的账期，请先走 reconq scan --reopen 由人工撤回成「待确认」",
    ),
    "SCAN_IN_PROGRESS": ErrorCodeMeta(
        code="SCAN_IN_PROGRESS",
        zh="该账期已有扫描在跑",
        suggestion="同一账期同时只允许一个扫描/重跑；等上一次结束，或先按 "
        "reconq scan --status 确认上一次是否已中断",
    ),
    # —— 同根因聚合与组级处置（Task 36）——
    "GROUP_ACTION_UNKNOWN": ErrorCodeMeta(
        code="GROUP_ACTION_UNKNOWN",
        zh="未登记的组级处置动作",
        suggestion="组级处置只有三种：confirm（确认）/ reject（驳回）/ "
        "withdraw（撤回）；「已确认 → 已驳回」这种直跳被禁，请先撤回再重办",
    ),
    "GROUP_ACTION_DENIED": ErrorCodeMeta(
        code="GROUP_ACTION_DENIED",
        zh="组级处置被状态白名单拒绝，整组未推进",
        suggestion="确认与驳回只能从「待确认」出发，撤回只能从「已确认 / 已驳回」出发。"
        "逐条提示里写明了每条卡在哪个状态 —— 整组不动是刻意的：部分成功会让"
        "经办人以为整单办完了",
    ),
    "GROUP_ACTION_OPERATOR_INVALID": ErrorCodeMeta(
        code="GROUP_ACTION_OPERATOR_INVALID",
        zh="操作人不是自然人标识，拒绝推进处置状态",
        suggestion="模型草稿、规则基线与降级路径一律不得把差异推成已确认。"
        "请传入经办人 / 审核人 / 主管的工号或姓名",
    ),
    "GROUP_MEMBER_MISSING": ErrorCodeMeta(
        code="GROUP_MEMBER_MISSING",
        zh="处置单成员在台账上已不存在",
        suggestion="多半是复扫后该差异被消解或改判。请重新拉一次处置单"
        "（reconq groups list）再操作",
    ),
    "GROUP_MEMBER_CAUSE_CHANGED": ErrorCodeMeta(
        code="GROUP_MEMBER_CAUSE_CHANGED",
        zh="处置单成员的 Top-1 成因已与本单不一致",
        suggestion="重跑归因后成因会变（渠道延迟入账 → HIS 未落单 是常事）。"
        "请重新拉处置单 —— 拿旧成因确认会把上一轮的结论盖到这一轮的账上",
    ),
    "GROUP_MEMBER_KIND_CHANGED": ErrorCodeMeta(
        code="GROUP_MEMBER_KIND_CHANGED",
        zh="处置单成员的歧义形态已与本单不一致",
        suggestion="同根因聚合按「成因 + 渠道 + 形态 + 金额分桶」分组，形态变了"
        "就该换一张单。请重新拉处置单（reconq groups list）",
    ),
    "AMOUNT_BUCKET_INVALID": ErrorCodeMeta(
        code="AMOUNT_BUCKET_INVALID",
        zh="金额分桶宽度非法",
        suggestion="桶宽必须是正数。桶宽为 0 或负会让所有金额落进同一个桶，"
        "等于没有分桶 —— 那会把不同金额混进同一张交办单，签了字的结论单"
        "会挂着一笔查不出来的差额",
    ),
    "AMOUNT_FLOAT_REJECTED": ErrorCodeMeta(
        code="AMOUNT_FLOAT_REJECTED",
        zh="差额以 float 传入，已拒绝",
        suggestion="账单金额一律用定点字符串或 Decimal 传入。二进制浮点尾数会让"
        "本该同金额的两条差异分不成同一张处置单",
    ),
    "CHECK_SELECTOR_EMPTY": ErrorCodeMeta(
        code="CHECK_SELECTOR_EMPTY",
        zh="口径选择器一条都没命中",
        suggestion="核对选择器表达式（row_count / type=not_empty / 列名.*）与契约里的 "
        "check path；本产品不会把「一条都没跑到」当作「全部通过」——"
        "口径名打错一个字符，门禁照样显绿是最危险的假绿",
    ),
    "CONTRACT_LINT_FAILED": ErrorCodeMeta(
        code="CONTRACT_LINT_FAILED",
        zh="契约静态校验未通过，已在取数前拦下",
        suggestion="按错误详情里的 YAML 行号改契约：列名拼错、check 挂在不存在的列上、"
        "同一 path 写了两遍、引用了没声明的变量 —— "
        "本产品不会先取回六路账单再发现是契约自己写错了",
    ),
    # —— 统一码表补全（Task 20）——
    # 前面的码是随各任务**用到时**补进来的；这里把「后面几个任务一定会用到、
    # 但本轮尚未触发」的码一次登记齐。提前登记的意义是：面板与结论单可以
    # 拿码表反查「这个码属不属于渠道级失败、该不该重试、建议动作是什么」，
    # 而不用等到那个码第一次出现时才发现表里查不到、只能显示一个裸英文串。
    "DB_AUTH_FAILED": ErrorCodeMeta(
        code="DB_AUTH_FAILED",
        zh="数据库认证失败",
        suggestion="核对连接串里的账号密码与该账号的库权限；"
        "HIS 那一路会报更具体的 HIS_DB_AUTH_FAILED（含认证失败/库不存在/网络不可达的区分）",
        channel_level=True,
    ),
    "AMBIGUOUS_ENTITY": ErrorCodeMeta(
        code="AMBIGUOUS_ENTITY",
        zh="一笔交易在候选单里匹配到多条，拒绝归并",
        suggestion="一个渠道单号匹配到多条 HIS 收费记录（拆笔、合并支付、跨账期落点、退费与原单并存）时，"
        "本产品**不猜**哪条是对的：把候选逐条列出交人工判定。"
        "猜错一条会让整段金额归到错的费用项目上，比留待确认危险得多",
    ),
    "WEAK_MATCH_PENDING": ErrorCodeMeta(
        code="WEAK_MATCH_PENDING",
        zh="弱匹配候选待人工确认",
        suggestion="金额相同但单号对不上、疑似同一笔的候选会进二级候选列表；"
        "需经办人逐条确认后由主管复核，系统不自动归并",
    ),
    # 刻意**不**标 channel_level：模型服务不可达不是「某一路渠道挂了、
    # 其余五路照常」，而是全期归因能力一起降级。若标成渠道级，编排层会
    # 只剔除一路渠道，其余五路的差异照样被当成「已归因」写进台账。
    "MODEL_UNAVAILABLE": ErrorCodeMeta(
        code="MODEL_UNAVAILABLE",
        zh="模型服务不可达",
        suggestion="核对 RECONQ_MODEL_BASE_URL / API_KEY / NAME 与院内网络；"
        "不可达时降级为「只报确定性口径结论 + 全部差异标待人工判定」，"
        "台账状态不因模型不可达而误推进",
    ),
    "MODEL_SCHEMA_INVALID": ErrorCodeMeta(
        code="MODEL_SCHEMA_INVALID",
        zh="模型返回结构不符合约定 Schema",
        suggestion="成因草稿须为 {cause, confidence, evidence_refs[], explanation_zh}；"
        "重试用尽仍不合规则丢弃该条草稿并标待人工判定，绝不把不合规文本当结论落台账",
    ),
    "MODEL_TIMEOUT": ErrorCodeMeta(
        code="MODEL_TIMEOUT",
        zh="模型调用读超时（连上了但不回话）",
        suggestion="按 RECONQ_MODEL_TIMEOUT 调整读超时。超时同样走确定性降级路径，"
        "不阻塞扫描。本码与 MODEL_CONNECT_TIMEOUT 分开是因为两者的处置动作"
        "不同：读超时说明**连上了但队列不回话**，先查 GPU 队列与模型是否加载完"
        "（连不上先查院内网络与推理服务进程是否在监听）",
        retryable=True,
    ),
    "MODEL_CONFIG_INVALID": ErrorCodeMeta(
        code="MODEL_CONFIG_INVALID",
        zh="模型网关配置缺位或非法",
        suggestion="四个凭据位（RECONQ_MODEL_BASE_URL / API_KEY / NAME / TIMEOUT）"
        "任一缺位或取值非法都会在**发请求前**被拒；本产品不提供"
        "「缺 base_url 就走默认端点」的兜底 —— 那样会把一份草稿发到"
        "不知道是哪个服务的模型上",
    ),
    "RUN_SUMMARY_INVALID": ErrorCodeMeta(
        code="RUN_SUMMARY_INVALID",
        zh="运行摘要装配参数非法",
        suggestion="build_run_summary(degradation=...) 只接受 "
        "reconq.gateway.degradation.DegradationLedger（需有 "
        "to_run_summary_fields()）；不传则摘要不带降级字段，"
        "不影响其余汇总项",
    ),
    "MODEL_RESPONSE_INVALID": ErrorCodeMeta(
        code="MODEL_RESPONSE_INVALID",
        zh="模型响应不符合 OpenAI-compatible 响应结构",
        suggestion="响应须含 choices[0].message.content；缺 content 多半是"
        "内容过滤或该端点并不真的兼容 OpenAI 协议。协议错误已归一为"
        "本码并附 HTTP 状态码与响应体摘要（摘要已脱敏，不含患者信息）",
    ),
    # —— 网关失败处理链（Task 30）——
    "MODEL_CONNECT_TIMEOUT": ErrorCodeMeta(
        code="MODEL_CONNECT_TIMEOUT",
        zh="模型服务连接超时（连都没连上）",
        suggestion="按 RECONQ_MODEL_CONNECT_TIMEOUT 调整连接超时。"
        "本码与 MODEL_TIMEOUT 分开是因为两者的处置动作不同：连接超时先查"
        "院内网络与推理服务进程是否在监听（连不上），读超时先查队列与"
        "模型是否加载（连上了但不回话）",
        retryable=True,
    ),
    "MODEL_CIRCUIT_OPEN": ErrorCodeMeta(
        code="MODEL_CIRCUIT_OPEN",
        zh="熔断已开启，未发任何请求",
        suggestion="连续失败达到阈值后熔断，避免每条差异都等满超时把扫描"
        "从 40 秒拖成 40 分钟。冷却期到会放一次试探：成功即恢复，失败重新"
        "计时。本期差异全部标待人工判定，台账状态不推进",
    ),
    "MODEL_RETRY_EXHAUSTED": ErrorCodeMeta(
        code="MODEL_RETRY_EXHAUSTED",
        zh="重试次数用尽仍失败，已降级",
        suggestion="按重试矩阵核对：连接失败/超时/5xx 各有自己的次数与"
        "退避节奏，4xx 业务与配置错误一次都不重试。次数用尽后本期归因"
        "降级为待人工判定，确定性口径结论照常出",
    ),
    "ATTRIBUTION_EVIDENCE_REF_INVALID": ErrorCodeMeta(
        code="ATTRIBUTION_EVIDENCE_REF_INVALID",
        zh="归因草稿引用了证据包之外的行",
        suggestion="evidence_refs 只能写证据包里真实存在的行标识（ref）。"
        "模型引用了不存在的行，说明它在按记忆编而不是在读证据 —— "
        "这类草稿整条作废并标待人工判定，不做「就近匹配」式的修补",
    ),
    "ATTRIBUTION_DRAFT_INVALID": ErrorCodeMeta(
        code="ATTRIBUTION_DRAFT_INVALID",
        zh="归因草稿字段不合约定",
        suggestion="草稿须为 {cause, confidence∈[0,1], evidence_refs[](非空), "
        "explanation_zh(非空)}。不合约定的草稿被丢弃并留痕，"
        "**不会**被「尽量修一修」后当成结论落台账",
    ),
    "ATTRIBUTION_DEGRADED": ErrorCodeMeta(
        code="ATTRIBUTION_DEGRADED",
        zh="归因已降级为待人工判定",
        suggestion="模型不可达 / 超时 / 重试用尽 / 响应不合协议时，本条差异"
        "只保留确定性口径结论，全部成因标待人工判定。"
        "**台账状态不因降级推进**，已确认只能由人工点产生",
    ),
    # —— 网关结构化输出分层校验（Task 29）——
    # 四条码各对应校验链的一层。**刻意不共用 ATTRIBUTION_DRAFT_INVALID**：
    # 「置信度越界」与「引用了不存在的行」要让人做的动作完全不同 ——
    # 前者让模型改口径（重试即可），后者要重读证据（重试大概率还是编）。
    # 共用一个码等于把「卡在哪一层」这个排障入口又关上了。
    "MODEL_OUTPUT_UNPARSEABLE": ErrorCodeMeta(
        code="MODEL_OUTPUT_UNPARSEABLE",
        zh="模型响应里提取不出唯一一份 JSON",
        suggestion="允许剥 ```json 围栏、允许前后是纯说明文字；但正文里出现"
        "**两份以上** JSON（哪怕只是句子里举的格式示例）一律拒收 —— "
        "那种响应说明模型在讲解而不是在作答，捡哪一份都是编的",
    ),
    "ATTRIBUTION_CAUSE_UNKNOWN": ErrorCodeMeta(
        code="ATTRIBUTION_CAUSE_UNKNOWN",
        zh="成因短语不在受控成因字典内",
        suggestion="自由文本成因无法归组、无法统计命中率，同根因聚合也认不出来。"
        "请改用受控字典里的成因短语；未配置字典时该层记为「跳过」而非「通过」",
    ),
    "ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE": ErrorCodeMeta(
        code="ATTRIBUTION_CONFIDENCE_OUT_OF_RANGE",
        zh="置信度越界（约定 0~1 的小数）",
        suggestion="最常见的是**百分制**：模型写 85 表示 85%，约定要写 0.85。"
        "本产品不会悄悄除以 100 —— 那会让同一份输入两次归因得到两个置信度，"
        "「成因 Top-1 命中率」这类指标就失去意义",
    ),
    "ATTRIBUTION_EXPLANATION_INVALID": ErrorCodeMeta(
        code="ATTRIBUTION_EXPLANATION_INVALID",
        zh="中文说明为空或超长",
        suggestion="explanation_zh 须非空且不超过 500 字。超长通常是模型把整份"
        "证据包复述了一遍 —— 该写的是「哪几行支持这个成因」，不是把证据再抄一次",
    ),
    # —— 模型调用审计与成本计量（Task 31）——
    "MODEL_PRICE_INVALID": ErrorCodeMeta(
        code="MODEL_PRICE_INVALID",
        zh="模型单价配置非法",
        suggestion="核对 RECONQ_MODEL_PRICE_PROMPT_PER_1K 与 "
        "RECONQ_MODEL_PRICE_COMPLETION_PER_1K：单位是元/千 token、"
        "须为非负数字。**两个位都要配** —— 只配一个会让成本合计漏掉另一侧，"
        "而漏掉的那侧往往不是 0。非法值显式报错，不静默回落 0："
        "回落 0 会让「这一期花了 0 元」看起来像一个真实结论",
    ),
    "CACHE_OVERWRITE_DENIED": ErrorCodeMeta(
        code="CACHE_OVERWRITE_DENIED",
        zh="覆盖已有缓存被拒（未给出更正理由）",
        suggestion="同一 (证据包指纹, 提示词版本, 模型) 已缓存且本次草稿与之不同。"
        "确实要换请显式传 correction_reason —— 覆盖会留痕到 model_call_corrections。"
        "**不要为了「让这期跑通」去改缓存**：缓存一旦被污染，同期每一条差异"
        "都会跟着给出错答案，且没有任何降级提示",
    ),
    "MODEL_AUDIT_SCHEMA_MISSING": ErrorCodeMeta(
        code="MODEL_AUDIT_SCHEMA_MISSING",
        zh="账单库里没有模型调用审计表",
        suggestion="审计表由 reconq.audit.init_audit_schema() 建。"
        "请确认 --db 指向的是本产品写的账单库；审计缺失不影响对账结论，"
        "但会让「这一期花了多少钱」答不出来",
    ),
    "MODEL_AUDIT_FIELD_INVALID": ErrorCodeMeta(
        code="MODEL_AUDIT_FIELD_INVALID",
        zh="模型调用审计记录的字段不合约定",
        suggestion="审计表只接受受控词表里的短码（status / error_code）与统计量。"
        "错误详情、prompt 原文、证据包正文一律不进审计表 —— "
        "它们进事件链（JSON 日志统一脱敏），审计表只留码与体量。"
        "要把「当时哪一路失败了」说清楚，凭 request_fingerprint 去快照取",
    ),
    "MODEL_AUDIT_CHAIN_BROKEN": ErrorCodeMeta(
        code="MODEL_AUDIT_CHAIN_BROKEN",
        zh="模型调用审计链校验未通过",
        suggestion="说明该账期的审计记录被改过或删过（指纹链断点）。"
        "结论单上的成本与 token 数因此不可信，请信息科按备份还原该账期的"
        "model_calls 表；本产品不会自动重算覆盖",
    ),
    # —— 链式审计事件与按期切片检索（Task 45）——
    # 五个码一律**不标** channel_level：审计链是全期一条，坏了就是这一期
    # 的「谁在什么时候做了什么」整体不可信，剔除任何一路渠道都修不好它。
    "AUDIT_LOG_DIR_REQUIRED": ErrorCodeMeta(
        code="AUDIT_LOG_DIR_REQUIRED",
        zh="未指定审计目录",
        suggestion="传 reconq.audit.append_event() 的 log_dir（存 events.jsonl 的"
        "目录）。本产品不默认往当前工作目录写审计文件 —— 换个目录跑就以为链"
        "在那儿，是这类链最容易出的错",
    ),
    "AUDIT_PERIOD_REQUIRED": ErrorCodeMeta(
        code="AUDIT_PERIOD_REQUIRED",
        zh="事件未带账期",
        suggestion="审计链按账期分片，缺账期的事件挂不到任何一条链上，"
        "「这一期发生了什么」会凭空少一截",
    ),
    "AUDIT_EVENT_TYPE_REQUIRED": ErrorCodeMeta(
        code="AUDIT_EVENT_TYPE_REQUIRED",
        zh="事件类型为空",
        suggestion="类型取 reconq.audit.EVENT_TYPES 里的受控值（如 SCAN_STARTED / "
        "DECIDED / REVIEWED）。面板按类型分组、按类型排时序，"
        "空类型的事件上链后谁也查不出来",
    ),
    "AUDIT_EVENT_NOT_DEIDENTIFIED": ErrorCodeMeta(
        code="AUDIT_EVENT_NOT_DEIDENTIFIED",
        zh="事件 payload 中出现患者可识别信息",
        suggestion="审计链是 append-only 的，写进去就删不掉，因此这里**抛错拒写**"
        "而不是静默替换（与日志不同）。请把 payload 改成只带金额、单号、账期、"
        "证据行号与处理人；核对口径见 reconq.audit.assert_event_deidentified",
    ),
    "AUDIT_EVENT_CORRUPT": ErrorCodeMeta(
        code="AUDIT_EVENT_CORRUPT",
        zh="事件行无法解析",
        suggestion="events.jsonl 是 append-only 的，一行坏掉说明写入被中断过。"
        "请把该文件与 events.index.json 一起交信息科，**不要就地删行** —— "
        "删掉的那一环正是校验要指出来的",
    ),
    "AUDIT_QUERY_INVALID": ErrorCodeMeta(
        code="AUDIT_QUERY_INVALID",
        zh="审计切片查询参数非法",
        suggestion="since / until 须是完整 ISO 8601 时刻（2026-09-07T00:00:00+08:00）；"
        "since 晚于 until 会返回空集，而空集在面板上与「这段时间真的没事件」"
        "无法区分，故显式报错",
    ),
    "AUDIT_DIFFERENCE_KEY_REQUIRED": ErrorCodeMeta(
        code="AUDIT_DIFFERENCE_KEY_REQUIRED",
        zh="查差异时序未给差异键",
        suggestion="差异键形如 2026-W36|wechat|taxonomy.missing:EX-9。空键会匹配到"
        "链上所有没有差异键的事件 —— 那不是一条差异的时序，是整期流水",
    ),
    # —— 面板筛选入参（Task 48）——
    # 三个码都刻意是 4xx 而不是「筛不出就返回空列表」：空列表在面板上
    # 与「本期没有差异」长得一模一样，经办人会据此认为本期干净了。
    "WEB_FILTER_STATE_UNKNOWN": ErrorCodeMeta(
        code="WEB_FILTER_STATE_UNKNOWN",
        zh="筛选用的处置状态未登记",
        suggestion="可筛的是：待归因 / 待确认 / 待主管复核 / 已确认 / 已驳回 / 未开单。"
        "状态名写错会静默返回空列表，看起来像「本期没有差异」",
    ),
    "WEB_FILTER_CHANNEL_UNKNOWN": ErrorCodeMeta(
        code="WEB_FILTER_CHANNEL_UNKNOWN",
        zh="筛选用的渠道未登记",
        suggestion="渠道须是六路受控渠道之一（微信 / 支付宝 / 银联 / 自助机 / HIS / 医保）；"
        "连坐结论记在全局渠道上，不按单路渠道筛",
    ),
    "WEB_FILTER_CAUSE_UNKNOWN": ErrorCodeMeta(
        code="WEB_FILTER_CAUSE_UNKNOWN",
        zh="筛选用的成因码未登记",
        suggestion="成因码取自受控成因字典（reconq align-causes 可列全）；"
        "写错会让分组数字与台账总数对不上",
    ),
    "WEB_FILTER_AMOUNT_INVALID": ErrorCodeMeta(
        code="WEB_FILTER_AMOUNT_INVALID",
        zh="筛选用的金额区间不合法",
        suggestion="金额须为定点数且下限不高于上限（如 amount_min=100&amount_max=500）。"
        "本产品不把带单位或千分位杂物的值猜成数字",
    ),
    "SCAN_STALE": ErrorCodeMeta(
        code="SCAN_STALE",
        zh="上一次扫描已中断",
        suggestion="心跳超过阈值未更新；请人工决定重扫（reconq scan --resume）"
        "或作废（reconq scan --reopen），系统不会自动重跑",
    ),
    # —— 弱匹配打分（Task 23）——
    # 刻意**不**标 channel_level：打分配置是六路共用的，参数配错不是「某一路
    # 挂了、其余五路照常」，而是整期弱匹配候选全部掉档或误判。
    "SCORING_CONFIG_INVALID": ErrorCodeMeta(
        code="SCORING_CONFIG_INVALID",
        zh="弱匹配打分配置非法",
        suggestion="核对 contracts/_scoring.yml：权重与阈值必须写成带引号的字符串"
        "（如 \"0.45\"，写成裸小数会解析成浮点、在门槛比较时产生边界翻转）、"
        "五个因子权重不得缺失或出现表外名字、分档门槛需满足 "
        "0 ≤ weak_min ≤ strong_min ≤ 1。非法值显式报错而不静默回落默认——"
        "回落默认会让你以为调过参数，实际按内置值在跑",
    ),
    # —— 对齐倒排索引（Task 22）——
    # 刻意**不**标 channel_level：索引是六路共用的，预算配错不是「某一路
    # 挂了、其余五路照常」，而是整期对齐都建不起来。
    "INDEX_BUDGET_INVALID": ErrorCodeMeta(
        code="INDEX_BUDGET_INVALID",
        zh="对齐索引的内存预算配置非法",
        suggestion="核对 RECONQ_INDEX_BUDGET_MB（只填数字，单位是 MB，"
        "填 0 等于没有上限）、RECONQ_INDEX_SPILL_ENTRIES 与 "
        "RECONQ_INDEX_CACHE_SIZE；非法值会显式报错，不静默回落默认值，"
        "否则你以为放开到无限、实际按 64MB 在跑",
    ),
    # —— 跨账期与跨渠道连坐追查（Task 24）——
    # 三条链上结论**刻意不标 channel_level**：跨渠道重复涉及两路渠道、
    # 跨账期重复涉及两个账期，标成「渠道级」会让编排层只剔除一路渠道，
    # 而另一路的合计照样被算进本期 —— 恰恰是这条结论要防的事。
    "DUPLICATE_ACROSS_PERIOD": ErrorCodeMeta(
        code="DUPLICATE_ACROSS_PERIOD",
        zh="同一笔 HIS 收费被两个账期各认领一次",
        suggestion="常见于周一零点前后的落点差异（渠道按结算日记账、HIS 按收费时间记账）。"
        "请确认这一笔应计入哪一期，并在另一期做冲销 —— "
        "**不要**为了两期都对平而在两期都留；本产品只报结论、不替财务科改账",
    ),
    "DUPLICATE_ACROSS_CHANNEL": ErrorCodeMeta(
        code="DUPLICATE_ACROSS_CHANNEL",
        zh="同一渠道单号被两路渠道各认领一次",
        suggestion="常见于自助机与微信/支付宝本就是同一条通道，或导出方把一笔拆成两路各发一次。"
        "请确认是导出重复还是真的收了两遍；本产品只认领一次，"
        "台账里该结论挂在全局（渠道记 *）而不是任一路渠道名下",
    ),
    "AMOUNT_INCONSISTENT": ErrorCodeMeta(
        code="AMOUNT_INCONSISTENT",
        zh="链上金额关系不成立（退款为负 / 合并支付为分摊和）",
        suggestion="红冲节点应等于被红冲那笔的相反数、渠道一笔应等于 HIS 侧几笔之和。"
        "本产品给出差额与全部链上节点，**不决定改哪一边**；"
        "系统侧拿不到「谁分摊给谁」的上下文，多渠道节点之间一律不判",
    ),
    "TRACE_WINDOW_INVALID": ErrorCodeMeta(
        code="TRACE_WINDOW_INVALID",
        zh="连坐追查窗口配置非法",
        suggestion="追查窗口以账期为单位展开，window_periods 不能为负；"
        "0 表示只看 focus 账期本身",
    ),
    "TRACE_PERIOD_REQUIRED": ErrorCodeMeta(
        code="TRACE_PERIOD_REQUIRED",
        zh="连坐追查未给账期",
        suggestion="连坐追查按 (单号, 账期) 复合索引取数，空账期等于全表扫。"
        "请用 --periods 2026-W35,2026-W36 给出追查范围，"
        "或用 --period 以它为中心展开窗口",
    ),
    # —— 对齐证据快照与复算回放（Task 26）——
    # 三条码都**不标** channel_level：快照冲突与脱敏命中都不是「某一路挂了」，
    # 剔除渠道只会让一条本该被人看见的结论继续躺在台账上。
    "SNAPSHOT_CONFLICT": ErrorCodeMeta(
        code="SNAPSHOT_CONFLICT",
        zh="同一结论已有内容不同的证据快照",
        suggestion="说明这条差异在两次扫描之间变了 —— 数据改了或口径改了。"
        "两份快照都已保留（只追加、不覆盖）。请用 "
        "`reconq verify --replay <结论键>` 复算，看清是口径还是数据；"
        "**不要手工删掉其中一份**：上一期的结论单可能正引用着它",
    ),
    "SNAPSHOT_NOT_FOUND": ErrorCodeMeta(
        code="SNAPSHOT_NOT_FOUND",
        zh="找不到该结论的证据快照",
        suggestion="快照按 (账期, 结论键) 定位。结论键形如 渠道:账期:单号，"
        "可用 `reconq verify --replay-list --period <账期>` 列出本期全部快照；"
        "若本期确实还没拍过快照，先跑一次扫描（reconq scan）",
    ),
    "SNAPSHOT_NOT_DEIDENTIFIED": ErrorCodeMeta(
        code="SNAPSHOT_NOT_DEIDENTIFIED",
        zh="快照中出现患者可识别信息",
        suggestion="本产品的快照只允许金额、订单号、凭证号、账期、科室与时间。"
        "患者姓名/身份证/手机号/住院号一律不落快照（结论单附录用脱敏样例即可回查）；"
        "请确认该字段是订单号而不是个人信息",
    ),
    "SNAPSHOT_FIELD_NOT_ALLOWED": ErrorCodeMeta(
        code="SNAPSHOT_FIELD_NOT_ALLOWED",
        zh="快照行引用出现未登记字段",
        suggestion="快照字段用白名单（SNAPSHOT_ALLOWED_FIELDS）约束而非黑名单 —— "
        "黑名单要靠人记得列出所有不该存的字段，漏一个就落进库了。"
        "新增字段请同步改白名单与脱敏检查",
    ),
    # —— 归因证据包（Task 27）——
    # 三条码同样**不标** channel_level：证据包构造失败意味着归因这条路走不通，
    # 剔除渠道只会让一条本该被人看的差异继续躺在台账上没人解释。
    "BUNDLE_INPUT_INVALID": ErrorCodeMeta(
        code="BUNDLE_INPUT_INVALID",
        zh="证据包输入类型不支持",
        suggestion="证据包只接受三类差异来源：对齐结论（MatchOutcome）、"
        "连坐结论（TraceConclusion）、差异台账条目（DiffItem）。"
        "请从对齐编排或台账取数后传入，不要自己拼一个 dict",
    ),
    "BUNDLE_FIELD_NOT_ALLOWED": ErrorCodeMeta(
        code="BUNDLE_FIELD_NOT_ALLOWED",
        zh="证据包中出现未登记字段",
        suggestion="证据包字段用白名单（BUNDLE_ROW_FIELDS）约束。"
        "证据包会随结论单流转很久，患者姓名/身份证/手机号/住院号一律不落；"
        "新增字段请同步改白名单与脱敏检查",
    ),
    "BUNDLE_WINDOW_INVALID": ErrorCodeMeta(
        code="BUNDLE_WINDOW_INVALID",
        zh="证据包的历史窗口或候选上限配置非法",
        suggestion="window_periods 与 candidate_cap 以「条」为单位，不能为负。"
        "0 是合法取值（0 = 不查往期 / 不限制候选条数），负数会让「先验频次」"
        "与「裁剪留痕」两处都失去意义",
    ),
    # —— 归因草稿反幻觉核对（Task 32）——
    # 与 Task 28 的 ATTRIBUTION_EVIDENCE_REF_INVALID 分开：那一码是「引用了
    # 根本不存在的行」，这一码是「引用了真实存在的行、但对着它说错了话」。
    # 后者更危险 —— ref 越界会被拒掉，而这一条带着正确行号走进经办确认流，
    # 经办人看到的是「有据可查」，于是签字。
    # 同样**不标** channel_level：草稿被拒意味着归因这条路走不通，剔除渠道
    # 只会让一条本该被人看的差异继续躺在台账上没人解释。
    "ATTRIBUTION_EVIDENCE_MISMATCH": ErrorCodeMeta(
        code="ATTRIBUTION_EVIDENCE_MISMATCH",
        zh="归因草稿的说法与所引证据行对不上",
        suggestion="草稿里的金额 / 时间 / 单号逐项与它引用的那些行对账不过，"
        "或声明的成因与证据行形态矛盾。系统不改写模型文本、也不推进任何状态，"
        "该差异标「待人工判定」；请按面板上列出的核对项调证据包组织方式或提示词",
    ),
    # —— 受控成因字典（Task 33）——
    # 与 Schema 链的 ATTRIBUTION_CAUSE_UNKNOWN **刻意分开**：那一码是
    # 「网关拿到的响应里 cause 字段不在字典内」（模型侧的判据），这一码是
    # 「调用方要渲染中文说明，槽位没给齐或给了这个模板不支持的槽位」
    # （模板侧的判据）。两者发生在链路的不同位置、错法也不同，合码会让排障
    # 时分不清该改提示词还是该改调用方传参。
    "CAUSE_TEMPLATE_SLOT_UNKNOWN": ErrorCodeMeta(
        code="CAUSE_TEMPLATE_SLOT_UNKNOWN",
        zh="成因说明模板的槽位缺项或不受支持",
        suggestion="说明模板的句式来自受控字典，模型只提供填空内容（金额 / 单号 / "
        "证据行 / 条数）。缺项时必须报错而不是渲染半句 —— 半句会被当成"
        "「系统没查」；金额槽位请传 Decimal 字符串，float 会让 1020.00 "
        "渲染成 1020.0",
    ),
    "CAUSE_DICT_VERSION_INVALID": ErrorCodeMeta(
        code="CAUSE_DICT_VERSION_INVALID",
        zh="成因字典版本记账参数非法",
        suggestion="字典版本要挂到某个账期的事件链上才能复算；不指定账期等于"
        "记到虚空，三个月后无法回答「上周那期用的是哪一版字典」",
    ),
    # —— 提示词与 Schema 版本化 + 离线回放（Task 34）——
    # 这一组码的共同点：**都在「改提示词 / 改 Schema」这条路上**。
    # 它们的症状都是「改了之后行为变了但说不清为什么」，所以每一条的
    # suggestion 都指回同一个动作：先把版本对上，再看回放。
    "PROMPT_VERSION_UNKNOWN": ErrorCodeMeta(
        code="PROMPT_VERSION_UNKNOWN",
        zh="提示词版本号未登记",
        suggestion="版本串进缓存键与审计链，拼错等于静默吃旧答案。"
        "已登记的版本见 reconq attrib-replay --list-versions；"
        "新增一版请用 reconq.align.prompts.register_prompt 并写清 change_note",
    ),
    "PROMPT_DIGEST_MISMATCH": ErrorCodeMeta(
        code="PROMPT_DIGEST_MISMATCH",
        zh="提示词正文与其登记的指纹对不上",
        suggestion="改了模板正文却没 bump 版本号。后果是同一版本串对应两份"
        "措辞：缓存键不变、审计链不变，而输出已经变了。"
        "请同步更新 PROMPT_VERSION_DIGEST 与 PROMPT_VERSION",
    ),
    "PROMPT_REGISTRY_INCONSISTENT": ErrorCodeMeta(
        code="PROMPT_REGISTRY_INCONSISTENT",
        zh="提示词注册表自相矛盾",
        suggestion="注册表键与模板自报版本不一致、cause_form 非法，"
        "或某一版没写 change_note（没写变更说明，回放报告里那个"
        "命中率变化就永远没有归属）",
    ),
    "PROMPT_CHANNEL_SPECIALCASE": ErrorCodeMeta(
        code="PROMPT_CHANNEL_SPECIALCASE",
        zh="提示词模板里出现了渠道特例",
        suggestion="渠道取件口径属于证据包（channel_meta.bill_type），不属于提示词。"
        "把渠道知识写进模板，会让换渠道 / 改渠道参数都变成一次提示词改版，"
        "而回放集的期望是按旧措辞录的。请把渠道相关内容移到证据包或契约里",
    ),
    "SCHEMA_FINGERPRINT_MISMATCH": ErrorCodeMeta(
        code="SCHEMA_FINGERPRINT_MISMATCH",
        zh="归因草稿 Schema 正文与其登记的指纹对不上",
        suggestion="改了 ATTRIBUTION_SCHEMA 的字段 / 必填 / 枚举却没有 bump "
        "SCHEMA_VERSION。后果与提示词漏改一样：版本串进缓存键与审计链，"
        "不 bump 就继续吃改之前的缓存答案",
    ),
    "SCHEMA_VERSION_UNKNOWN": ErrorCodeMeta(
        code="SCHEMA_VERSION_UNKNOWN",
        zh="归因草稿 Schema 版本号未登记",
        suggestion="本产品当前只有一版 Schema。回放对比只切提示词版本："
        "同时切 Schema 会让「哪个改动带来的提升」无法归因",
    ),
    "REPLAY_SET_EMPTY": ErrorCodeMeta(
        code="REPLAY_SET_EMPTY",
        zh="归因回放集为空或目录不存在",
        suggestion="用 PYTHONPATH=src python3 tests/fixtures/attrib/"
        "generate_replay_set.py 生成。空回放集跑出来的「命中率 0%」"
        "不是模型不行，是压根没喂东西",
    ),
    "REPLAY_SET_TOO_SMALL": ErrorCodeMeta(
        code="REPLAY_SET_TOO_SMALL",
        zh="归因回放集条数低于下限",
        suggestion="下限 12 条。一条差异的结论偶然对一次是常事，"
        "条数不够就分不出「真变好了」与「这次运气好」",
    ),
    "REPLAY_CASE_INVALID": ErrorCodeMeta(
        code="REPLAY_CASE_INVALID",
        zh="回放用例结构不合法",
        suggestion="每条用例须带 case_id、bundle（逐字节固定的证据包 payload）"
        "与 expect（可接受成因码集合 / 必需引用行号 / 最少引用行数）；"
        "响应须给 drafts（结构化）或 content（原样文本）之一",
    ),
    "REPLAY_RESPONSE_MISSING": ErrorCodeMeta(
        code="REPLAY_RESPONSE_MISSING",
        zh="回放集缺某一版提示词的录好响应",
        suggestion="没有录好的响应就没有回放 —— 重新调模型测的是端点当天的状态，"
        "不是提示词版的差别。缺一版会让版本对比缺一条基线，"
        "而「基线缺失」在报告上看起来和「这一版没提上来」一模一样",
    ),
    "REPLAY_CASE_NOT_FOUND": ErrorCodeMeta(
        code="REPLAY_CASE_NOT_FOUND",
        zh="回放报告里没有指定的用例",
        suggestion="两版回放必须跑在同一份用例集上；某一版缺用例时"
        "先补录该用例在该版下的响应",
    ),
    "REPLAY_COMPARE_ARGS_INVALID": ErrorCodeMeta(
        code="REPLAY_COMPARE_ARGS_INVALID",
        zh="版本对比参数格式不对",
        suggestion="--compare 要两版，格式 <旧版本>,<新版本>。只给一版比不了 ——"
        "「变好还是变坏」是相对基线说的",
    ),
    # —— 对齐质量指标（Task 38）——
    "GOLDEN_SET_MISSING": ErrorCodeMeta(
        code="GOLDEN_SET_MISSING",
        zh="找不到对齐金标集",
        suggestion="没有金标集就没有可核对的指标；先补 tests/fixtures/align/golden.jsonl",
    ),
    "GOLDEN_SET_EMPTY": ErrorCodeMeta(
        code="GOLDEN_SET_EMPTY",
        zh="对齐金标集一条 case 都没有",
        suggestion="检查文件是否被清空；空集跑出来的「零漏判」是假的",
    ),
    "GOLDEN_SET_TOO_SMALL": ErrorCodeMeta(
        code="GOLDEN_SET_TOO_SMALL",
        zh="对齐金标集条数低于下限",
        suggestion=f"下限 {60} 条。一条差异偶然对一次是常事，条数不够就分不出"
        "「真变好了」与「这次运气好」；宁可先补标注，"
        "也不要用小样本报一个好看的数字",
    ),
    "GOLDEN_CASE_INVALID": ErrorCodeMeta(
        code="GOLDEN_CASE_INVALID",
        zh="金标用例结构不合法",
        suggestion="每条 case 须有 case_id / channel / period / expect_label 与"
        "至少一行渠道行；行内每条须有 amount 与带时区偏移的 trade_time ——"
        "缺时刻就判不了跨账期落点",
    ),
    "ATTRIB_EVAL_SET_TOO_SMALL": ErrorCodeMeta(
        code="ATTRIB_EVAL_SET_TOO_SMALL",
        zh="归因评测集条数低于下限",
        suggestion="下限 40 条总体、5 条 UNKNOWN 期望。UNKNOWN 样本是"
        "反幻觉拒绝率的分母 —— 2 条里对 1 条是 50%，那是抛硬币，"
        "不是拒答能力。宁可先补标注，也不用小样本报一个好看的命中率",
    ),
    "ATTRIB_EVAL_CASE_INVALID": ErrorCodeMeta(
        code="ATTRIB_EVAL_CASE_INVALID",
        zh="归因评测样本结构不合法",
        suggestion="每条须有 case_id / channel / bundle / 非空 expect_codes；"
        "expect_codes 恰好为 ['undetermined'] 时 expect_unknown 必须为真 —— "
        "两者标错一个会让反幻觉拒绝率的分母算错",
    ),
    "ATTRIB_EVAL_FIXTURE_MISSING": ErrorCodeMeta(
        code="ATTRIB_EVAL_FIXTURE_MISSING",
        zh="归因评测夹具不存在",
        suggestion="跑 PYTHONPATH=src python3 "
        "tests/fixtures/attrib/golden/generate_eval_set.py 重新生成",
    ),
    "ATTRIB_EVAL_FIXTURE_INVALID": ErrorCodeMeta(
        code="ATTRIB_EVAL_FIXTURE_INVALID",
        zh="归因评测夹具内容不合法",
        suggestion="cases.jsonl 须每行一条合法 JSON 且 case_id 不重复；"
        "重复会让同一条被计两次，两次还可能算出两个不同结论",
    ),
    "ATTRIB_EVAL_RESPONSE_MISSING": ErrorCodeMeta(
        code="ATTRIB_EVAL_RESPONSE_MISSING",
        zh="评测样本没有对应的录制响应",
        suggestion="responses.json 须覆盖 cases.jsonl 的每一条。"
        "缺一条会让模型档少算一条，而「少算的那条恰好是对的」在报告上"
        "与「这一版没提上来」长得一模一样",
    ),
    "ATTRIB_EVAL_CASE_NOT_FOUND": ErrorCodeMeta(
        code="ATTRIB_EVAL_CASE_NOT_FOUND",
        zh="评测集里没有这条样本",
        suggestion="case_id 与 cases.jsonl 不一致；重跑生成脚本",
    ),
    "RULE_BASELINE_FAILED": ErrorCodeMeta(
        code="RULE_BASELINE_FAILED",
        zh="规则基线档在一条样本上异常",
        suggestion="该条按不可评计入（不是「答错」），并在报告第 5 节点名。"
        "规则档崩了说明归因兜底路径有问题，先查 rank_causes_by_rules",
    ),
    # —— 对齐黄金样例回归集（Task 40）——
    "ALIGN_CASE_SET_MISSING": ErrorCodeMeta(
        code="ALIGN_CASE_SET_MISSING",
        zh="找不到对齐黄金样例回归集",
        suggestion="先补 tests/fixtures/align/cases.yml；没有回归集就没有"
        "「这次改动有没有弄坏原来判对的」这个问题的答案",
    ),
    "ALIGN_CASE_SET_INVALID": ErrorCodeMeta(
        code="ALIGN_CASE_SET_INVALID",
        zh="回归集文件结构不合法",
        suggestion="cases.yml 顶层须是含 cases 列表的映射；缩进错位最常见",
    ),
    "ALIGN_CASE_SET_EMPTY": ErrorCodeMeta(
        code="ALIGN_CASE_SET_EMPTY",
        zh="回归集一条 case 都没有",
        suggestion="检查文件是否被清空；空集跑出来的「全绿」是假的",
    ),
    "ALIGN_CASE_SET_TOO_SMALL": ErrorCodeMeta(
        code="ALIGN_CASE_SET_TOO_SMALL",
        zh="回归集条数低于下限",
        suggestion="下限 40 条。一条差异偶然对一次是常事，条数不够就分不出"
        "「真没弄坏」与「这次没碰到那处代码」；宁可先补用例，"
        "也不要用小样本报一句「全绿」",
    ),
    "ALIGN_CASE_FORM_UNKNOWN": ErrorCodeMeta(
        code="ALIGN_CASE_FORM_UNKNOWN",
        zh="回归用例的形态标签不在受控词表内",
        suggestion="形态是受控词表（reconq.align.cases.CASE_FORMS）。"
        "新增形态必须先在词表与 FORM_ZH 登记 —— 塞进已有形态那一格会让"
        "四形态覆盖门禁失效",
    ),
    "ALIGN_FORM_COVERAGE_INSUFFICIENT": ErrorCodeMeta(
        code="ALIGN_FORM_COVERAGE_INSUFFICIENT",
        zh="四形态覆盖门禁未过",
        suggestion="一码付拆多笔 / 合并支付 / 跨账期落点 / 退费与原单并存，"
        "每种至少 4 条。少一种就意味着「为一种形态调细判据、另一种悄悄判错」"
        "没人拦得住。补用例，不要放宽门禁",
    ),
    "ALIGN_CASE_INVALID": ErrorCodeMeta(
        code="ALIGN_CASE_INVALID",
        zh="回归用例结构不合法",
        suggestion="每条须有 case_id / form / channel / expect.status 与至少一行"
        "渠道行；行内 amount 必填（用字符串定点写，别用 YAML 浮点）、"
        "trade_time 给了就必须带时区偏移；case_id 不得重复",
    ),
    "GOLDEN_LABEL_UNKNOWN": ErrorCodeMeta(
        code="GOLDEN_LABEL_UNKNOWN",
        zh="金标期望标签不在受控标签空间内",
        suggestion="标签须取自 MatchStatus 七个值加 his_only；"
        "标签空间受控才不会让「同一个词两种算法」",
    ),
    # —— 先例库（Task 41）——
    "PRECEDENT_SIGNATURE_INVALID": ErrorCodeMeta(
        code="PRECEDENT_SIGNATURE_INVALID",
        zh="先例特征签名无法计算",
        suggestion="签名取自渠道 / 交易类型 / 是否跨日边界 / 候选条数 / "
        "金额是否相等 / 单号前缀形态六项可比较特征。缺项时显式报出缺哪一项，"
        "不接受用空值凑出一个「碰巧能对上」的签名",
    ),
    "PRECEDENT_CHANNEL_UNKNOWN": ErrorCodeMeta(
        code="PRECEDENT_CHANNEL_UNKNOWN",
        zh="先例检索的渠道不在六路受控渠道内",
        suggestion="渠道须取自六路受控渠道（微信 / 支付宝 / 银联 / 自助机 / "
        "HIS / 医保）。渠道是签名的一半，写错渠道等于把先例存到了另一个地方",
    ),
    "PRECEDENT_VERSION_UNKNOWN": ErrorCodeMeta(
        code="PRECEDENT_VERSION_UNKNOWN",
        zh="先例登记的版本对缺失或不合法",
        suggestion="先例必须带当时的成因字典版本与提示词版本；缺版本就无法"
        "判断改版后该降权还是该照用，只能拒收而不是猜",
    ),
    "PRECEDENT_DISPOSITION_INVALID": ErrorCodeMeta(
        code="PRECEDENT_DISPOSITION_INVALID",
        zh="只有人工确认的差异才能沉淀先例",
        suggestion="已驳回与仍待确认的差异不进先例库 —— 把经办人判为「不是这个原因」"
        "的样本当成先例，会让下一期给出恰好被否掉的建议",
    ),
    "PRECEDENT_OPERATOR_INVALID": ErrorCodeMeta(
        code="PRECEDENT_OPERATOR_INVALID",
        zh="先例登记的操作人不是自然人",
        suggestion="先例是「人判过的」才叫先例。模型草稿、规则基线与降级路径"
        "都不得登记先例",
    ),
    "PRECEDENT_HIT_MISSING": ErrorCodeMeta(
        code="PRECEDENT_HIT_MISSING",
        zh="先例命中记录指向的先例不存在",
        suggestion="命中留痕是引用而非拷贝：先例行被删后审计链上留下一条指向"
        "空处的记录比没有记录更难查。先例行不应被删",
    ),
    # —— 对齐主路径编排（Task 42）——
    "ALIGN_PIPELINE_STAGE_UNKNOWN": ErrorCodeMeta(
        code="ALIGN_PIPELINE_STAGE_UNKNOWN",
        zh="主路径阶段名不在受控阶段表内",
        suggestion="阶段名取自 reconq.align.pipeline.STAGES。面板按阶段名显示"
        "进度条，新增阶段必须先在 STAGES 与 STAGE_ZH 登记 —— 否则面板上"
        "这一段既没有名字也没有顺序",
    ),
    "ALIGN_PIPELINE_PERIOD_REQUIRED": ErrorCodeMeta(
        code="ALIGN_PIPELINE_PERIOD_REQUIRED",
        zh="跑主路径必须给账期",
        suggestion="reconq align-pipeline --period 2026-W36。账期是账期实例、"
        "台账键与处置单键的共同主键，不给账期就没有唯一的一份「本期」",
    ),
    "ALIGN_PIPELINE_BILL_TABLE_MISSING": ErrorCodeMeta(
        code="ALIGN_PIPELINE_BILL_TABLE_MISSING",
        zh="账单库还没建表，主路径无从取行",
        suggestion="先跑一次 reconq scan --period <账期>（取数并幂等落库），"
        "主路径读的是账单库里已落的那份行，不自己再去读渠道文件",
    ),
    # —— 差异处置单状态机与三角色确认流（Task 43）——
    "LEDGER_SCHEMA_MISSING": ErrorCodeMeta(
        code="LEDGER_SCHEMA_MISSING",
        zh="差异处置单表尚未建表",
        suggestion="先跑 reconq ledger open --period <账期> 把本期差异开成待办。"
        "本产品不会静默建表 ——「这期压根没开单」与「这期已处理完」在"
        "结论单上长得一样，不能把前者显示成后者",
    ),
    "LEDGER_DISCREPANCY_NOT_FOUND": ErrorCodeMeta(
        code="LEDGER_DISCREPANCY_NOT_FOUND",
        zh="处置单不存在",
        suggestion="它得先由 reconq ledger open 开成待办。按（账期, 渠道, 口径）"
        "核对键；口径名打错一个字符就会查不到",
    ),
    "LEDGER_STATE_INVALID": ErrorCodeMeta(
        code="LEDGER_STATE_INVALID",
        zh="目标状态不是合法状态",
        suggestion="合法状态：待归因 / 待确认 / 待主管复核 / 已确认 / 已驳回",
    ),
    "LEDGER_TRANSITION_DENIED": ErrorCodeMeta(
        code="LEDGER_TRANSITION_DENIED",
        zh="处置单的状态迁移被白名单拒绝",
        suggestion="改判必须先撤回重办（已确认 → 已驳回、已确认 → 待归因 都是直跳，"
        "一律被禁）—— 结论单上要留得住中间那次「撤回」，否则半年后复盘"
        "看到的是一条凭空的翻案。报错详情里写明了当前可去的落点",
    ),
    "LEDGER_ACTION_UNKNOWN": ErrorCodeMeta(
        code="LEDGER_ACTION_UNKNOWN",
        zh="未登记的处置动作",
        suggestion="本产品只有四种：提交处置结论 / 通过 / 驳回 / 撤回",
    ),
    "LEDGER_ROLE_INVALID": ErrorCodeMeta(
        code="LEDGER_ROLE_INVALID",
        zh="处置角色不在受控三岗内",
        suggestion="只有三种：经办 / 审核 / 主管复核",
    ),
    "LEDGER_ROLE_DENIED": ErrorCodeMeta(
        code="LEDGER_ROLE_DENIED",
        zh="该状态下的这个动作不由这个岗位执行",
        suggestion="待确认上的通过只能由审核做，待主管复核上的通过只能由主管做。"
        "主管复核的意义是多一道**独立**签字 —— 主管顺手把审核那一关也签了，"
        "这一关就等于不存在",
    ),
    "LEDGER_ROLE_SEPARATION_DENIED": ErrorCodeMeta(
        code="LEDGER_ROLE_SEPARATION_DENIED",
        zh="同一人试图在一条处置单上占两个岗位",
        suggestion="结论单上「经办人经办、审核人审核、主管复核」那三行签字"
        "必须是三个人。请换一位同事点这一关",
    ),
    "LEDGER_OPERATOR_INVALID": ErrorCodeMeta(
        code="LEDGER_OPERATOR_INVALID",
        zh="操作人不是自然人标识，拒绝推进处置状态",
        suggestion="模型草稿、规则基线与降级路径一律不得把差异推成已确认。"
        "请传入经办人 / 审核人 / 主管的工号或姓名",
    ),
    "LEDGER_VERSION_CONFLICT": ErrorCodeMeta(
        code="LEDGER_VERSION_CONFLICT",
        zh="处置单已被别人改过，本次操作未生效",
        suggestion="你手上是打开面板那一刻的版本号，中间有人已经推进了这条。"
        "请刷新后重看再操作 —— 无声覆盖的后果不是「数据错了」，"
        "是「谁签的这句话查不到了」",
    ),
    "LEDGER_VERSION_REQUIRED": ErrorCodeMeta(
        code="LEDGER_VERSION_REQUIRED",
        zh="单条处置没有带版本号",
        suggestion="单条入口必须传 expected_version（面板按钮自动带）。"
        "没有版本号的确认会无声盖掉别人刚写下的结论；批量入口可以不带，"
        "它逐条自己读当前版本",
    ),
    "LEDGER_CONCLUSION_REQUIRED": ErrorCodeMeta(
        code="LEDGER_CONCLUSION_REQUIRED",
        zh="提交处置结论时没写结论",
        suggestion="结论单上「经办人结论」那一栏就是从这里取的 —— "
        "空着的结论单没法签。要写要点，证据行号写进证据栏",
    ),
    "LEDGER_CONCLUSION_TOO_LONG": ErrorCodeMeta(
        code="LEDGER_CONCLUSION_TOO_LONG",
        zh="处置结论超过长度上限",
        suggestion="留痕表按 512 字符存。请把要点写进结论、证据行号写进证据栏",
    ),
    "LEDGER_AMOUNT_FLOAT_REJECTED": ErrorCodeMeta(
        code="LEDGER_AMOUNT_FLOAT_REJECTED",
        zh="处置单差额以 float 传入，已拒绝",
        suggestion="账单金额一律用定点字符串或 Decimal 传入。处置单的差额会算进"
        "结论单的合计，而合计是对着财务科台账核的，尾数差一分钱就要有人"
        "回来解释",
    ),
    # —— 结论单导出（Task 44）——
    "EXPORT_PERIOD_REQUIRED": ErrorCodeMeta(
        code="EXPORT_PERIOD_REQUIRED",
        zh="导出结论单未给账期",
        suggestion="补上 --period（形如 2026-W36）。结论单是一期一份，"
        "不指定账期就只能全库扫，那是几十万行而不是一份单子",
    ),
    "EXPORT_FORMAT_UNKNOWN": ErrorCodeMeta(
        code="EXPORT_FORMAT_UNKNOWN",
        zh="结论单格式不受支持",
        suggestion="只支持 md（打印签字）与 csv（回填既有电子台账）。"
        "要别的版式请在 export/conclusion.py 的 CONCLUSION_FORMATS 里登记，"
        "并在 spec.md 的开源对照里写清它对应源产品的哪条主路径",
    ),
    "SHEET_NOT_DEIDENTIFIED": ErrorCodeMeta(
        code="SHEET_NOT_DEIDENTIFIED",
        zh="结论单里出现患者可识别信息，已拒绝导出",
        suggestion="结论单会打印出来交给财务科并可能转交渠道方。请把经办结论、"
        "渠道说明里的患者标识（姓名、住院号、身份证、手机号）改写成"
        "「已与收费处核对差异原因」这类不含患者标识的表述；"
        "证据回查请走「文件名:行号」，不要把整行原文贴进结论",
    ),
    # —— 企业微信推送（Task 46）——
    "WECOM_THRESHOLD_INVALID": ErrorCodeMeta(
        code="WECOM_THRESHOLD_INVALID",
        zh="企微推送阈值配置非法",
        suggestion="条数阈值写非负整数（RECONQ_WECOM_UNBALANCED_COUNT_THRESHOLD），"
        "金额阈值写定点数且不带千分位与货币符号"
        "（RECONQ_WECOM_UNBALANCED_AMOUNT_THRESHOLD，如 5000.00）。"
        "阈值写错会静默变成「永不推送」或「每期都推」，因此这里显式报错",
    ),
    "WECOM_WEBHOOK_KEY_MISSING": ErrorCodeMeta(
        code="WECOM_WEBHOOK_KEY_MISSING",
        zh="企业微信机器人 webhook key 未配置",
        suggestion="在环境变量 WECOM_WEBHOOK_KEY 里配置群机器人地址末尾的 key。"
        "未配置时超阈值不推送（不是推送一条空消息），面板会显示「待推送」",
    ),
    "WECOM_PAYLOAD_REJECTED": ErrorCodeMeta(
        code="WECOM_PAYLOAD_REJECTED",
        zh="企微推送内容越界，已拒发",
        suggestion="推送只允许出现：差异条数、超阈值类型、面板地址与深链。"
        "请勿把金额明细、单号全文或任何患者可识别信息放进 msg —— "
        "企微群消息会被转发与截图，明细应留在面板与结论单里",
    ),
    "WECOM_SEND_FAILED": ErrorCodeMeta(
        code="WECOM_SEND_FAILED",
        zh="企微推送请求失败",
        suggestion="按详情里的 HTTP 状态码与 errcode 排查。超时会自动入队重试；"
        "若反复出现请确认本机可出网到 qyapi.weixin.qq.com",
        retryable=True,
    ),
    "WECOM_WEBHOOK_INVALID": ErrorCodeMeta(
        code="WECOM_WEBHOOK_INVALID",
        zh="企微 webhook key 无效或机器人已不在群里",
        suggestion="errcode 93000/40001/40014 一类。这**不是**网络问题，重试不会好："
        "请确认 WECOM_WEBHOOK_KEY 是当前群机器人的 key（群机器人被移出群后 key 立即失效），"
        "重新添加机器人后取新 key。本期状态会显示「推送失败」，需人工点「重新推送」",
    ),
    "WECOM_RATE_LIMITED": ErrorCodeMeta(
        code="WECOM_RATE_LIMITED",
        zh="企微机器人触发频率限制",
        suggestion="同一机器人每分钟 20 条上限。本产品已把该条入 pending 队列，"
        "按指数退避重发；重试用尽仍未送达则状态显示「推送失败」而不是「已推送」",
        retryable=True,
    ),
    "WECOM_RETRY_EXHAUSTED": ErrorCodeMeta(
        code="WECOM_RETRY_EXHAUSTED",
        zh="企微推送重试用尽仍未送达",
        suggestion="已重试 3 次。面板上本期状态为「推送失败」，请人工点「重新推送」，"
        "或先确认机器人仍在群里、key 仍然有效",
    ),
    "WECOM_STATE_CORRUPT": ErrorCodeMeta(
        code="WECOM_STATE_CORRUPT",
        zh="企微推送状态文件无法解析",
        suggestion="wecom_state.json 是去重与重试队列的唯一真源，坏掉会导致"
        "重复推送或丢失待办。请把它连同审计目录一起交信息科，**不要就地删行**",
    ),
    "WECOM_PUSH_NOT_ALLOWED": ErrorCodeMeta(
        code="WECOM_PUSH_NOT_ALLOWED",
        zh="非自然人触发的企微推送被拒",
        suggestion="「重新推送」是人工动作，只能由人显式发起。定时任务、"
        "扫描编排与模型均不得代为调用（与台账状态机同一道边界）",
    ),
}


def error_meta(code: str) -> ErrorCodeMeta:
    """取错误码元数据；未登记的码返回一条兜底元数据而不是 KeyError。"""
    return ERROR_CODES.get(
        code,
        ErrorCodeMeta(code=code, zh="未登记的错误码", suggestion="请把该码反馈给信息科并登记到 errors.py"),
    )


class ReconqError(Exception):
    """本产品唯一的业务异常类型。

    必带错误码；可选带渠道、证据行引用与 YAML 出处（路径 + 行号），
    使「谁、何时、依据哪几行」在排障时能直接落到界面上。
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        channel: Optional[str] = None,
        row_ref: Optional[Any] = None,
        yaml_path: Optional[str] = None,
        yaml_line: Optional[int] = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.channel = channel
        self.row_ref = row_ref
        self.yaml_path = yaml_path
        self.yaml_line = yaml_line
        #: 真实成因码（如退避重试用尽时的 ``LINK_EXPIRED``）。默认与 ``code``
        #: 相同；只有「处置结果」与「根因」不同时才由调用方改写。
        self.cause_code = code
        #: 逐路失败明细。仅 ``registry.initialize_all`` 在「全部渠道被剔除」
        #: 时挂上来，供编排层按**本次实际范围**改写报错视角与成因码。
        self.failures: list[Any] = []
        super().__init__(self.render())

    def render(self) -> str:
        """渲染成一行可读文本：错误码 + 中文说明 + 详情 + 出处。"""
        meta = error_meta(self.code)
        parts = [f"[{self.code}] {meta.zh}：{self.detail}"]
        where: list[str] = []
        if self.channel:
            where.append(f"渠道={self.channel}")
        if self.row_ref is not None:
            where.append(f"行={self.row_ref}")
        if self.yaml_path:
            where.append(f"契约={self.yaml_path}" + (f":{self.yaml_line}" if self.yaml_line else ""))
        if where:
            parts.append("(" + "，".join(where) + ")")
        if meta.suggestion:
            parts.append(f"建议：{meta.suggestion}")
        return " ".join(parts)

    def __str__(self) -> str:  # pragma: no cover - 直接复用 render
        return self.render()

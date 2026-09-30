"""对账契约引擎。

一份契约（``contracts/<channel>.yml``）= 账单表列定义（dataset）
+ 若干对账口径（check）。财务科 / 信息科改口径只改 YAML，不改代码。
"""

from __future__ import annotations

from .loader import (
    apply_variables,
    collect_variable_placeholders,
    load_contract,
    parse_variable_assignments,
    parse_variable_value,
)
from .fingerprint import (
    BaselineComparison,
    BaselineFlip,
    CheckChange,
    ColumnChange,
    ContractDiff,
    ContractFingerprint,
    ImpactScope,
    canonical_contract,
    canonical_source,
    compare_baseline,
    diff_fingerprints,
    fingerprint_contract,
    fingerprint_source,
    impact_scope,
    render_markdown,
)
from .model import CheckYaml, ColumnYaml, ContractYaml, DatasetYaml, TableYaml, check_path_str
from .selector import CheckSelector, CheckSelectorParseError, filter_checks

__all__ = [
    "BaselineComparison",
    "BaselineFlip",
    "CheckChange",
    "CheckSelector",
    "CheckSelectorParseError",
    "CheckYaml",
    "ColumnChange",
    "ColumnYaml",
    "ContractDiff",
    "ContractFingerprint",
    "ContractYaml",
    "DatasetYaml",
    "ImpactScope",
    "TableYaml",
    "apply_variables",
    "canonical_contract",
    "canonical_source",
    "check_path_str",
    "collect_variable_placeholders",
    "compare_baseline",
    "diff_fingerprints",
    "filter_checks",
    "fingerprint_contract",
    "fingerprint_source",
    "impact_scope",
    "load_contract",
    "parse_variable_assignments",
    "parse_variable_value",
    "render_markdown",
]

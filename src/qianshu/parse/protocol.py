"""0.3.0 申请解析的模型声明与程序核对结果。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from qianshu.draft import YesNo

PROTOCOL_VERSION = "0.3.0"

type Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class StatementStatus(StrEnum):
    CERTAIN = "确定"
    UNWRITTEN = "原文未写"
    AMBIGUOUS = "歧义"
    CONFLICT = "冲突"
    CONDITIONAL = "带条件"
    UNSUPPORTED = "缺证据"


class ValueSource(StrEnum):
    ORIGINAL = "原文"
    DEFAULT = "默认规则"
    LOOKUP = "查表派生"


class DocumentStatus(StrEnum):
    NO_POLICY = "经检查无政策"
    UNSPLIT = "可能有政策但无法切分"
    INSUFFICIENT = "材料不足"


type ClauseStatus = Literal["确定", "歧义", "冲突", "带条件", "缺证据"]
type CoverageUse = Literal["政策条款", "续行", "前言", "批次说明", "备注", "其他文本", "待确认范围"]


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class Span(Declaration, frozen=True):
    """以模型输入块为基准的 Unicode 半开区间。"""
    source_id: Identifier
    start: int
    end: int


class Evidence(Declaration):
    clause_id: Identifier
    span: Span
    text: str


class ProposedValue[T](Declaration):
    value: T
    evidence: list[Evidence]


class Finding[T](Declaration):
    status: StatementStatus
    reason_code: Identifier
    value: T | None
    candidates: list[ProposedValue[T]]
    evidence: list[Evidence]
    checked: list[Span]


class Duration(Declaration):
    quantity: int
    unit: Literal["天", "月"]


class RequestedPackage(Declaration):
    current_package: str
    gift_package: str | None


class Findings(Declaration):
    policy_name: Finding[str]
    maintain_mutual_exclusion_group: Finding[YesNo]
    country: Finding[str]
    company: Finding[str]
    company_keyword: Finding[list[str]]
    policy_type: Finding[str]
    start_date: Finding[str]
    end_date: Finding[str]
    benefit_type: Finding[str]
    trigger_once: Finding[YesNo]
    cancel_on_downgrade: Finding[YesNo]
    recharge_days: Finding[Duration]
    gift_days: Finding[Duration]
    repeat_bonus: Finding[YesNo]
    effective_multiplier: Finding[int | float]
    user_tags: Finding[list[str]]
    packages: Finding[list[RequestedPackage]]
    user_scope: Finding[str]
    approval_url: Finding[str]
    approval_attachments: Finding[list[str]]


class Policy(Declaration):
    id: Identifier
    main: Span
    continuations: list[Span]
    shared_clause_ids: list[Identifier]
    has_title: bool
    fields: Findings


class DateTarget(Declaration):
    country: str | None
    company: str | None


class DateUse(Declaration):
    role: Literal["起日", "止日", "申请周期", "其他"]
    usable: bool
    targets: list[DateTarget] | None


class Clause(Declaration):
    id: Identifier
    kind: Literal["正文", "续行", "备注", "前言", "批次说明"]
    spans: list[Span]
    field_ids: list[Identifier]
    status: ClauseStatus
    disposition: Literal["生效", "询问", "备选", "带条件", "作废", "冲突", "不明"]
    scope: Literal["局部", "共享"]
    policy_ids: list[Identifier]
    date: DateUse | None


class Coverage(Declaration):
    span: Span
    use: CoverageUse


class FieldImpact(Declaration):
    policy_id: Identifier
    field_ids: list[Identifier]


class PendingRange(Declaration):
    span: Span
    reason_code: Identifier
    policy_ids: list[Identifier]
    fields_by_policy: list[FieldImpact]


class DocumentFinding(Declaration):
    status: DocumentStatus
    reason_code: Identifier


class ApplicationReply(Declaration):
    policies: list[Policy]
    clauses: list[Clause]
    coverage: list[Coverage]
    pending: list[PendingRange]
    document: DocumentFinding | None


@dataclass(frozen=True, repr=False)
class SourceBlock:
    """模型输入块及其相对于来源文本的映射。"""
    id: str
    text: str
    source_id: str
    start: int = 0


@dataclass(frozen=True, repr=False)
class FieldResult:
    """原文状态单独保留。has_value 区分明示空值和当前无值。"""
    statement: Finding
    value: object = None
    has_value: bool = False
    source: ValueSource | None = None
    needs_confirmation: bool = True
    issues: tuple[str, ...] = ()
    input_fields: tuple[str, ...] = ()
    rule: str | None = None
    evidence: tuple[Evidence, ...] = ()

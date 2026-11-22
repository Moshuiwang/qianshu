"""0.3.0 申请解析的模型声明与程序核对结果。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.json_schema import SkipJsonSchema

from qianshu.draft import YesNo

PROTOCOL_VERSION = "0.3.0"
REPLY_STRUCTURE = "thin"
LEGACY_REPLY_STRUCTURE = "w1d"

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


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class Span(Declaration, frozen=True):
    """以模型输入块为基准的 Unicode 半开区间。"""
    source_id: Identifier
    start: int
    end: int
    text: SkipJsonSchema[str | None] = Field(default=None, exclude_if=lambda value: value is None)


class PolicyAnchor(Span):
    text: str | None = Field(
        default=None, exclude_if=lambda value: value is None, description="逐字抄录政策主段或续行。",
    )


class Evidence(Declaration):
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
    explanation: str | None = Field(default=None, exclude_if=lambda value: value is None)


class Duration(Declaration):
    quantity: int
    unit: Literal["天", "月"]


class RequestedPackage(Declaration):
    current_package: str
    gift_package: str | None


# 缺少完整候选或坐标时，Findings 将短名降为缺证据。
# 工具允许模型省略这些子项，其他字段仍使用原有的必填证据结构。
def _optional_properties(schema: dict) -> None:
    schema.pop("required", None)


class ShortNameSpan(Span):
    model_config = ConfigDict(json_schema_extra=_optional_properties)


class ShortNameEvidence(Evidence):
    model_config = ConfigDict(json_schema_extra=_optional_properties)
    span: ShortNameSpan


class ShortNameCandidate(ProposedValue[str]):
    model_config = ConfigDict(json_schema_extra=_optional_properties)
    evidence: list[ShortNameEvidence]


class ShortNameFinding(Finding[str]):
    status: StatementStatus = StatementStatus.UNSUPPORTED
    reason_code: Identifier = "short_name"
    value: str | None = None
    candidates: list[ShortNameCandidate] = Field(default_factory=list)
    evidence: list[ShortNameEvidence] = Field(default_factory=list)


class Findings(Declaration):
    policy_name: Finding[str]
    policy_short_name: ShortNameFinding = Field(default_factory=ShortNameFinding)
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

    @field_validator("policy_short_name", mode="wrap")
    @classmethod
    def _optional_short_name(cls, value, handler):
        try:
            return handler(value)
        except ValidationError:
            return ShortNameFinding()


class Policy(Declaration):
    id: Identifier
    main: PolicyAnchor
    continuations: list[PolicyAnchor]
    has_title: bool
    fields: Findings


class UnusedPassage(Declaration):
    text: str = Field(min_length=1, repr=False)
    reason: str = Field(min_length=1, repr=False)


class ApplicationReply(Declaration):
    policies: list[Policy]
    unused_passages: list[UnusedPassage] | None = None

    @field_validator("unused_passages", mode="before")
    @classmethod
    def _display_passages(cls, value: object) -> list[UnusedPassage] | None:
        if not isinstance(value, list):
            return None
        passages = []
        for item in value:
            try:
                passage = UnusedPassage.model_validate(item)
            except ValidationError:
                continue
            if passage.text.strip() and passage.reason.strip():
                passages.append(passage)
        return passages


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

    @property
    def evidence_verified(self) -> bool:
        return self.has_value and not self.issues and not self.needs_confirmation

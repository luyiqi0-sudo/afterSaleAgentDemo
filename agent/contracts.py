from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    conditions: dict[str, str] = Field(default_factory=dict)
    tenant: str = Field(default="demo", min_length=1, max_length=128)


class ModelDraft(BaseModel):
    """Untrusted model output. No fields in this hierarchy convey authority."""

    model_config = ConfigDict(extra="forbid", strict=True)


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Question = Annotated[str, Field(min_length=1, max_length=1200)]
GoalKind = Literal["policy", "sql", "order", "logistics", "ticket", "other"]
ActionType = Literal["retrieve_policy", "query_sql", "query_order", "query_logistics",
                     "request_ticket", "ask_user", "finish"]


class SourceSpan(ModelDraft):
    start: int = Field(ge=0, le=5_000)
    end: int = Field(gt=0, le=5_000)

    @model_validator(mode="after")
    def nonempty(self):
        if self.end <= self.start:
            raise ValueError("empty span")
        return self


class GoalCandidate(ModelDraft):
    id: Identifier
    description: str = Field(min_length=1, max_length=300)
    kind: GoalKind
    subject_candidate: Identifier | None = None
    source_span: SourceSpan | None = None
    deps: list[Identifier] = Field(default_factory=list, max_length=3)


class GoalDraft(ModelDraft):
    goals: list[GoalCandidate] = Field(max_length=4)
    clarification: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def finite_dependencies(self):
        by_id = {goal.id: goal for goal in self.goals}
        if len(by_id) != len(self.goals) or not (self.goals or self.clarification):
            raise ValueError("missing or repeated goal")

        def check(goal, active):
            if goal.id in active:
                raise ValueError("goal dependency cycle")
            if len(set(goal.deps)) != len(goal.deps) or not set(goal.deps) <= by_id.keys():
                raise ValueError("unknown or repeated dependency")
            for dep in goal.deps:
                check(by_id[dep], active | {goal.id})

        for goal in self.goals:
            check(goal, set())
        return self


class ParameterSource(ModelDraft):
    type: Literal["message", "object", "observation"]
    span: SourceSpan | None = None
    ref_id: Identifier | None = None
    field: Identifier | None = None

    @model_validator(mode="after")
    def matching_reference(self):
        valid = {
            "message": self.span is not None and self.ref_id is None and self.field is None,
            "object": self.span is None and self.ref_id is not None and self.field is None,
            "observation": self.span is None and self.ref_id is not None and self.field is not None,
        }
        if not valid[self.type]:
            raise ValueError("source reference does not match its type")
        return self


class TopicParameters(ModelDraft):
    question: Question


class OrderParameters(ModelDraft):
    order_id: str = Field(min_length=1, max_length=64)


class TicketParameters(OrderParameters):
    reason: str | None = Field(default=None, max_length=300)


class AskParameters(ModelDraft):
    question: Question
    field: Identifier


class FinishParameters(ModelDraft):
    pass


_PARAMETERS = {
    "retrieve_policy": TopicParameters, "query_sql": TopicParameters,
    "query_order": OrderParameters, "query_logistics": OrderParameters,
    "request_ticket": TicketParameters, "ask_user": AskParameters, "finish": FinishParameters,
}


class ActionProposal(ModelDraft):
    action_type: ActionType
    goal_ids: list[Identifier] = Field(min_length=1, max_length=4)
    parameters: TopicParameters | OrderParameters | TicketParameters | AskParameters | FinishParameters
    parameter_sources: dict[str, ParameterSource] = Field(max_length=4)
    based_on_observation_ids: list[Identifier] = Field(max_length=12)
    public_summary: str = Field(min_length=1, max_length=240)

    @classmethod
    def schema_for_actions(cls, allowed: list[str] | None) -> dict:
        schema = cls.model_json_schema()
        if allowed is None:
            return schema
        if not allowed:
            raise ValueError("empty allowed actions")
        properties = schema["properties"]
        properties["action_type"]["enum"] = allowed
        models = {f"#/$defs/{_PARAMETERS[action].__name__}" for action in allowed}
        refs = [{"$ref": name} for name in sorted(models)]
        properties["parameters"] = refs[0] if len(refs) == 1 else {"anyOf": refs}
        if not set(allowed) & {"query_order", "query_logistics", "request_ticket"}:
            # These actions carry a business question, not an entity argument
            # requiring message/object provenance. Do not invite fake refs.
            properties["parameter_sources"] = {"type": "object", "properties": {},
                                                "additionalProperties": False, "maxProperties": 0}
        for name in {t.__name__ for t in _PARAMETERS.values()} - {r.rsplit("/", 1)[-1] for r in models}:
            schema["$defs"].pop(name, None)
        return schema

    @model_validator(mode="before")
    @classmethod
    def parse_action_parameters(cls, value):
        if isinstance(value, dict) and isinstance(value.get("action_type"), str):
            action_type = value["action_type"]
            if action_type in _PARAMETERS and "parameters" in value:
                value = dict(value)
                value["parameters"] = _PARAMETERS[action_type].model_validate(value["parameters"])
        return value

    @model_validator(mode="after")
    def finite_sources(self):
        params = self.parameters.model_dump(exclude_none=True)
        if not self.parameter_sources.keys() <= params.keys():
            raise ValueError("source for unknown parameter")
        if "order_id" in params and "order_id" not in self.parameter_sources:
            raise ValueError("missing order source")
        for ids in (self.goal_ids, self.based_on_observation_ids):
            if len(ids) != len(set(ids)):
                raise ValueError("repeated reference")
        return self


class AnswerClaim(ModelDraft):
    text: str = Field(min_length=1, max_length=450)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=6)


class GroundedAnswer(ModelDraft):
    claims: list[AnswerClaim] = Field(max_length=6)


class IntentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[Literal["rag", "sql", "query_order", "query_logistics", "create_ticket"]] = Field(default_factory=list, max_length=4)
    order_id: str | None = Field(default=None, max_length=64)
    explanation: str = Field(default="", max_length=300)


class ToolResult(BaseModel):
    status: Literal["SUCCESS", "FAILED", "UNKNOWN"]
    data: dict[str, Any] | None = None
    error_code: str | None = None
    attempts: int = 0
    operation_id: str | None = None


class SQLResult(BaseModel):
    status: Literal["SUCCESS"] = "SUCCESS"
    columns: list[str]
    rows: list[dict[str, Any]]
    executed_sql: str
    duration_ms: float
    truncated: bool = False


class ModelProvider(Protocol):
    async def understand_goal(self, question: str, context: dict) -> GoalDraft: ...
    async def decide_next(self, context: dict) -> ActionProposal: ...
    async def sql(self, question: str, schema: str, error: str | None = None) -> str: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

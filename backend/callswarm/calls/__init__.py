"""Call layer: provider abstraction, fake provider, gates, scoring, schema, strategy."""

from callswarm.calls.fake import FakeCallProvider, FakeScript
from callswarm.calls.gates import CallGate
from callswarm.calls.provider import (
    AuthorizedPlan,
    CallBudgetExceeded,
    CallEvent,
    CallExecutionProvider,
    CallNotAuthorized,
    CallPlan,
    CallProviderError,
    CallProviderNotAvailable,
    CallResult,
    EventPage,
    GatedCallProvider,
    IntentAlreadyExecuted,
    LiveCallsDisabled,
    QuietHours,
    RecipientNotAllowed,
    RecipientSuppressed,
    idempotency_key,
)
from callswarm.calls.schema import (
    ResultSchemaInvalid,
    generate_result_schema,
    validate_result_against_schema,
    validate_result_schema,
)
from callswarm.calls.scoring import (
    CallSelection,
    PriorityWeights,
    RejectedIntent,
    compute_priority,
    select_calls,
)
from callswarm.calls.service import CallExecutionResult, CallService, select_call_provider
from callswarm.calls.strategy import CallPlanResult, CallStrategy

__all__ = [
    "AuthorizedPlan",
    "CallBudgetExceeded",
    "CallEvent",
    "CallExecutionProvider",
    "CallExecutionResult",
    "CallGate",
    "CallNotAuthorized",
    "CallPlan",
    "CallPlanResult",
    "CallProviderError",
    "CallProviderNotAvailable",
    "CallResult",
    "CallSelection",
    "CallService",
    "CallStrategy",
    "EventPage",
    "FakeCallProvider",
    "FakeScript",
    "GatedCallProvider",
    "IntentAlreadyExecuted",
    "LiveCallsDisabled",
    "PriorityWeights",
    "QuietHours",
    "RecipientNotAllowed",
    "RecipientSuppressed",
    "RejectedIntent",
    "ResultSchemaInvalid",
    "compute_priority",
    "generate_result_schema",
    "idempotency_key",
    "select_call_provider",
    "select_calls",
    "validate_result_against_schema",
    "validate_result_schema",
]

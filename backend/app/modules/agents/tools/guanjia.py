"""ERP-009 trusted Host adapter. No configuration lookup or model transport.

The caller passes authenticated identities and a separately constructed frozen
ObservationHost. The JSON argument schema cannot contain any binding fields.
"""
from backend.app.modules.integrations.guanjia.read_service import (
    ConfirmedProduct, ObservationHost, ObservationDenied, ObservationAuditUnavailable, observe,
)
from backend.app.modules.integrations.guanjia.read_schemas import (
    ObservationInput, ObservationOutput, PriceInput, StockInput,
)


def invoke(session, requester, agent, payload, *, host):
    return observe(session, requester, agent, payload=payload, host=host)

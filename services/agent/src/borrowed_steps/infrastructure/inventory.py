"""Server-bound, read-only inventory view for the intake assistant."""

from __future__ import annotations

from collections import Counter

from borrowed_steps.application.interpreter import InventoryCount
from borrowed_steps.application.ports import Store

__all__ = ["StoreInventoryReader"]


class StoreInventoryReader:
    """Counts equipment by kind and readiness state inside one workspace.

    The workspace is bound here, by the server, from the session cookie. There is
    no workspace argument anywhere the model can reach, and the result carries no
    equipment identity, borrower, request or loan.

    Each call opens and closes its own short read transaction, so no transaction
    is ever held open across an inference.
    """

    __slots__ = ("_store", "_workspace_id")

    def __init__(self, store: Store, workspace_id: str) -> None:
        self._store = store
        self._workspace_id = workspace_id

    def kind_state_counts(self) -> list[InventoryCount]:
        with self._store.transaction(self._workspace_id, write=False) as uow:
            items = uow.list_equipment()
        tally = Counter((item.kind.value, item.state.value) for item in items)
        return [
            InventoryCount(kind=kind, state=state, count=count)
            for (kind, state), count in sorted(tally.items())
        ]

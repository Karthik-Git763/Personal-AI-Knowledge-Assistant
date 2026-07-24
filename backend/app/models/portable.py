from typing import Any

from sqlalchemy import event, inspect


def _prevent_portable_id_update(
    target: Any, value: Any, oldvalue: Any, _initiator: Any
) -> Any:
    if inspect(target).persistent and value != oldvalue:
        raise ValueError("portable_id is immutable after persistence")
    return value


class PortableIdMixin:
    """Protect stable backup identifiers after their rows are persisted."""

    @classmethod
    def __declare_last__(cls) -> None:
        event.listen(
            getattr(cls, "portable_id"),
            "set",
            _prevent_portable_id_update,
            retval=True,
            active_history=True,
        )

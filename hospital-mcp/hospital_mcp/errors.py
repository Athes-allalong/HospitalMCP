"""Stable error codes. Transports turn a HospitalError into their own error shape
(MCP: ToolError with a "[CODE] message Hint: ..." text) so the model can recover."""
import json
from typing import Optional


class Codes:
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    DUPLICATE = "DUPLICATE"
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
    DOCTOR_INACTIVE = "DOCTOR_INACTIVE"
    DOCTOR_ON_LEAVE = "DOCTOR_ON_LEAVE"
    SCHEDULE_CONFLICT = "SCHEDULE_CONFLICT"
    HAS_FUTURE_APPOINTMENTS = "HAS_FUTURE_APPOINTMENTS"
    DEPARTMENT_HEAD = "DEPARTMENT_HEAD"
    LAST_ADMIN = "LAST_ADMIN"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    IN_PROGRESS = "IN_PROGRESS"
    RATE_LIMITED = "RATE_LIMITED"


class HospitalError(Exception):
    def __init__(self, code: str, message: str, hint: Optional[str] = None, data: Optional[dict] = None):
        self.code, self.message, self.hint, self.data = code, message, hint, data or {}
        super().__init__(str(self))

    def __str__(self) -> str:
        s = f"[{self.code}] {self.message}"
        return f"{s} Hint: {self.hint}" if self.hint else s

    def to_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.data:
            out["details"] = self.data
        return out

    def for_model(self) -> str:
        """One string with everything the model needs to recover (used for MCP ToolError)."""
        s = str(self)
        return f"{s} Details: {json.dumps(self.data, default=str)}" if self.data else s

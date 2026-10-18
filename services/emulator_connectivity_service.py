import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, isfinite

from core.adb_service import AdbService


@dataclass(frozen=True)
class EmulatorConnectivityResult:
    status: str
    device_id: str
    checked_at: str
    detail: str | None = None

    @property
    def probe_succeeded(self) -> bool | None:
        if self.status == "validated":
            return True
        if self.status in {"unvalidated", "no_internet", "no_network"}:
            return False
        return None


class EmulatorConnectivityService:
    """Read Android's live network-validation state through ADB."""

    DEFAULT_TIMEOUT_SECONDS = 8
    MAX_TIMEOUT_SECONDS = 30

    def __init__(self, adb_service: AdbService):
        self.adb_service = adb_service

    def probe(
        self,
        device_id: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> EmulatorConnectivityResult:
        if (
            not isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > self.MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "timeout_seconds must be greater than zero and at most 30"
            )

        checked_at = datetime.now(timezone.utc).isoformat()
        device_id = str(device_id or "").strip()
        if not device_id:
            return EmulatorConnectivityResult(
                status="invalid_device",
                device_id="",
                checked_at=checked_at,
                detail="An ADB device ID is required.",
            )

        result = self.adb_service.shell_result(
            device_id,
            ["dumpsys", "connectivity"],
            timeout=ceil(timeout_seconds),
        )
        if result is None:
            return EmulatorConnectivityResult(
                status="adb_error",
                device_id=device_id,
                checked_at=checked_at,
                detail="ADB shell timed out or could not be executed.",
            )

        if result.returncode != 0:
            return EmulatorConnectivityResult(
                status="adb_error",
                device_id=device_id,
                checked_at=checked_at,
                detail="Android could not report its connectivity state.",
            )

        status = self._read_status(result.stdout)
        details = {
            "validated": "Android reports an Internet-capable validated network.",
            "unvalidated": "Android reports Internet capability without validation.",
            "no_internet": "Android reports a connected network without Internet capability.",
            "no_network": "Android reports no connected network.",
            "unknown": "Android connectivity output has no parseable active network state.",
        }
        return EmulatorConnectivityResult(
            status=status,
            device_id=device_id,
            checked_at=checked_at,
            detail=details[status],
        )

    @staticmethod
    def _read_status(output: str) -> str:
        records = re.split(r"(?=NetworkAgentInfo\{)", output)
        network_records = [
            record for record in records if record.startswith("NetworkAgentInfo{")
        ]
        if not network_records:
            return "unknown"

        connected_capabilities = []
        parsed_network_state = False
        for record in network_records:
            state_match = re.search(r"ni\{([^}]*)\}", record)
            if not state_match:
                continue

            parsed_network_state = True
            state_tokens = set(re.findall(r"[A-Z_]+", state_match.group(1).upper()))
            if "CONNECTED" not in state_tokens or "DISCONNECTED" in state_tokens:
                continue

            capabilities_match = re.search(
                r"Capabilities:\s*([^\]]*)\]",
                record,
                re.IGNORECASE,
            )
            if not capabilities_match:
                connected_capabilities.append(None)
                continue

            capabilities = {
                token.strip().upper()
                for token in capabilities_match.group(1).split("&")
            }
            connected_capabilities.append(capabilities)

        if not parsed_network_state:
            return "unknown"
        if not connected_capabilities:
            return "no_network"
        if any(
            capabilities is not None
            and {"INTERNET", "VALIDATED"}.issubset(capabilities)
            for capabilities in connected_capabilities
        ):
            return "validated"
        if any(capabilities is None for capabilities in connected_capabilities):
            return "unknown"
        if any(
            capabilities is not None and "INTERNET" in capabilities
            for capabilities in connected_capabilities
        ):
            return "unvalidated"
        return "no_internet"

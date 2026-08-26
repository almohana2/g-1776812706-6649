"""تحقّق من صيغة استجابات Hydrawise قبل استخدامها (SRS §14).

الـAPI متساهل في الأنواع: ``run`` قد يصل نصًا أو رقمًا، وقد تختفي حقول
كاملة بين الإصدارات. لذلك كل حقل هنا اختياري بقيمة افتراضية آمنة،
و``extra="allow"`` يحفظ ما لا نعرفه بدل رفض الاستجابة كلها.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: قيمة Hydrawise التي تعني "لا يوجد تشغيل قادم" (نحو خمسين عامًا).
NOT_SCHEDULED_SECONDS = 1_576_800_000

#: حسب SRS §9.2: ``relay.time == 1`` تعني أن المحبس يعمل الآن.
RUNNING_SENTINEL = 1


def _to_int(value: Any) -> int | None:
    """يحوّل قيمة API إلى ``int``، ويعيد ``None`` لما ليس رقمًا."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


class LenientModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ControllerPayload(LenientModel):
    """عنصر من ``customerdetails.controllers[]``."""

    controller_id: int | None = None
    name: str | None = None
    serial_number: str | None = None
    status: str | None = None
    last_contact: int | None = None

    @field_validator("controller_id", "last_contact", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _to_int(value)


class CustomerDetailsPayload(LenientModel):
    """استجابة ``customerdetails.php``."""

    customer_id: int | None = None
    controller_id: int | None = None
    current_controller: str | None = None
    controllers: list[ControllerPayload] = Field(default_factory=list)

    @field_validator("customer_id", "controller_id", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _to_int(value)


class RelayPayload(LenientModel):
    """عنصر من ``statusschedule.relays[]``."""

    relay_id: int | None = None
    relay: int | None = None
    name: str = ""
    time: int | None = None
    run: int | None = None
    timestr: str | None = None
    nicetime: str | None = None
    lastwater: str | None = None
    period: int | None = None
    type: int | None = None

    @field_validator("relay_id", "relay", "time", "run", "period", "type", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _to_int(value)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @property
    def is_running(self) -> bool:
        """SRS §9.2 — ``time == 1`` هي علامة التشغيل الجاري."""
        return self.time == RUNNING_SENTINEL

    @property
    def is_scheduled(self) -> bool:
        return (
            self.time is not None
            and not self.is_running
            and self.time < NOT_SCHEDULED_SECONDS
        )


class RunningPayload(LenientModel):
    """عنصر من ``statusschedule.running[]`` عندما يوفره الخادم.

    ليست مذكورة في SRS §9.2 لأن القاعدة هناك تعتمد ``relay.time``، لكنها
    تُستخدم هنا كمصدر مساند: عندما تصل، تعطينا المدة المخططة والمتبقية
    معًا، فتفتح الحدث بثقة أعلى دون انتظار عينة سابقة.
    """

    relay_id: int | None = None
    relay: int | None = None
    name: str | None = None
    time_left: int | None = None
    run: int | None = None

    @field_validator("relay_id", "relay", "time_left", "run", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _to_int(value)


class SensorPayload(LenientModel):
    input: int | None = None
    type: int | None = None
    mode: int | None = None
    name: str | None = None

    @field_validator("input", "type", "mode", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _to_int(value)


class StatusSchedulePayload(LenientModel):
    """استجابة ``statusschedule.php``."""

    controller_id: int | None = None
    customer_id: int | None = None
    name: str | None = None
    status: str | None = None
    message: str | None = None
    time: int | None = None
    nextpoll: int | None = None
    relays: list[RelayPayload] = Field(default_factory=list)
    running: list[RunningPayload] = Field(default_factory=list)
    sensors: list[SensorPayload] = Field(default_factory=list)

    @field_validator("controller_id", "customer_id", "time", "nextpoll", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _to_int(value)

    @field_validator("relays", "running", "sensors", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> Any:
        # بعض الاستجابات تُرسل كائنًا فارغًا بدل مصفوفة فارغة.
        return value if isinstance(value, list) else []

    def running_by_relay(self) -> dict[int, RunningPayload]:
        return {
            item.relay_id: item for item in self.running if item.relay_id is not None
        }

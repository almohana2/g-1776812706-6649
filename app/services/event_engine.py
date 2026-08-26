"""آلة حالات أحداث التشغيل — منطق خالص بلا قاعدة بيانات (SRS §9).

الفكرة: كل عينة ``statusschedule`` تُترجم إلى ملاحظة لكل محبس، ثم تُقارن
الملاحظة بالحالة المحفوظة للمحبس فينتج قرار واحد: افتح حدثًا، أو مدّد
المفتوح، أو أغلقه. فصل القرار عن الكتابة يجعل السيناريوهات الصعبة —
البدء في منتصف التشغيل، إعادة تشغيل الـWorker، الانقطاع — قابلة للاختبار
دون قاعدة بيانات.

مصطلحات الـAPI المهمة:

``relay.time == 1``
    المحبس يعمل الآن (SRS §9.2).
``relay.run`` أثناء التشغيل
    الثواني المتبقية.
``relay.run`` أثناء التوقف
    مدة التشغيل القادم — وهي ما نستخدمه لاحقًا كـ"المدة المخططة".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.enums import Confidence
from app.schemas.hydrawise import StatusSchedulePayload

__all__ = [
    "ZoneObservation",
    "OpenRunState",
    "StartRun",
    "ExtendRun",
    "CloseRun",
    "observations_from_payload",
    "plan_start",
    "plan_extend",
    "plan_close",
    "plan_stale_close",
    "gap_threshold_seconds",
]

#: أقصى مدة تشغيل معقولة لحدث واحد؛ ما تجاوزها يدل على خلل لا على ريّ.
MAX_REASONABLE_RUNTIME_SECONDS = 12 * 3600


@dataclass(frozen=True)
class ZoneObservation:
    """حالة محبس واحد في لحظة عينة واحدة."""

    relay_id: int
    physical_number: int | None
    name: str
    is_running: bool
    #: الثواني المتبقية من التشغيل الجاري، إن أعلنها الخادم.
    remaining_seconds: int | None = None
    #: المدة الكاملة للتشغيل — الجاري إن عُرفت، أو القادم عند التوقف.
    planned_seconds: int | None = None
    #: الثواني حتى التشغيل القادم كما وصلت في ``relay.time``.
    seconds_until_next_run: int | None = None
    next_run_text: str | None = None
    last_water_text: str | None = None


@dataclass(frozen=True)
class OpenRunState:
    """ما نعرفه عن حدث مفتوح — مرآة لصف قاعدة البيانات."""

    started_at: datetime
    last_running_at: datetime
    planned_runtime_seconds: int | None = None
    last_remaining_seconds: int | None = None
    confidence: Confidence = Confidence.MEDIUM


@dataclass(frozen=True)
class StartRun:
    """قرار فتح حدث جديد."""

    started_at: datetime
    planned_runtime_seconds: int | None
    last_remaining_seconds: int | None
    confidence: Confidence


@dataclass(frozen=True)
class ExtendRun:
    """قرار تمديد حدث مفتوح — لا يمس ``started_at`` (SRS §9.5)."""

    last_running_at: datetime
    last_remaining_seconds: int | None
    planned_runtime_seconds: int | None


@dataclass(frozen=True)
class CloseRun:
    """قرار إغلاق حدث مفتوح."""

    ended_at: datetime
    runtime_seconds: int
    confidence: Confidence


def observations_from_payload(
    payload: StatusSchedulePayload,
) -> list[ZoneObservation]:
    """يستخرج ملاحظة لكل محبس من عينة واحدة.

    القاعدة الأساسية هي قاعدة SRS §9.2 (``time == 1``). ومصفوفة
    ``running[]`` — حين يرسلها الخادم — تُستخدم كمصدر مساند لأنها تحمل
    المتبقي والمدة الكاملة معًا؛ وجود المحبس فيها يُعدّ تشغيلًا أيضًا،
    وهو توسيع محافظ لا يخالف القاعدة بل يغطي الحالة التي يتأخر فيها
    ``time`` عن التحديث.
    """
    running_map = payload.running_by_relay()
    observations: list[ZoneObservation] = []

    for relay in payload.relays:
        if relay.relay_id is None:
            continue
        running_entry = running_map.get(relay.relay_id)
        is_running = relay.is_running or running_entry is not None

        remaining: int | None = None
        planned: int | None = None
        if is_running:
            if running_entry is not None:
                remaining = running_entry.time_left
                planned = running_entry.run
            if remaining is None:
                # أثناء التشغيل تحمل ``run`` الثواني المتبقية (SRS §9.2).
                remaining = relay.run
        else:
            planned = relay.run

        observations.append(
            ZoneObservation(
                relay_id=relay.relay_id,
                physical_number=relay.relay,
                name=relay.name or f"محبس {relay.relay or relay.relay_id}",
                is_running=is_running,
                remaining_seconds=remaining if remaining and remaining > 0 else None,
                planned_seconds=planned if planned and planned > 0 else None,
                seconds_until_next_run=relay.time,
                next_run_text=relay.nicetime or relay.timestr,
                last_water_text=relay.lastwater,
            )
        )

    # محبس يعمل لكنه غائب عن ``relays[]`` لسبب ما — لا يضيع.
    known = {item.relay_id for item in observations}
    for relay_id, entry in running_map.items():
        if relay_id in known:
            continue
        observations.append(
            ZoneObservation(
                relay_id=relay_id,
                physical_number=entry.relay,
                name=entry.name or f"محبس {entry.relay or relay_id}",
                is_running=True,
                remaining_seconds=entry.time_left,
                planned_seconds=entry.run,
            )
        )
    return observations


def plan_start(
    observation: ZoneObservation,
    *,
    observed_at: datetime,
    previous_planned_seconds: int | None = None,
    previous_nextpoll_seconds: int | None = None,
) -> StartRun:
    """يحسب بداية حدث جديد عند الانتقال ``Idle → Running`` (SRS §9.4).

    عندما نعرف المدة المخططة والمتبقي، نعرف كم مضى من التشغيل:

    ``elapsed = planned - remaining``

    ونطرحه من لحظة الرصد. لكن هذا الطرح مقيَّد بضعف فترة الاستطلاع
    السابقة، لأن ما قبل ذلك كنا سنراه في العينة الماضية — والتقييد هو ما
    يمنع اختراع زمن تشغيل غير مرصود بعد انقطاع طويل.

    * لم يُقيَّد الطرح ⇒ الثقة ``high``.
    * قُيّد (التشغيل أقدم مما يمكن أن نكون قد فوّتناه) ⇒ ``medium``.
    * لا معلومات كافية، كالتشغيل اليدوي المفاجئ ⇒ البداية هي لحظة الرصد
      والثقة ``low``.
    """
    planned = observation.planned_seconds or previous_planned_seconds
    remaining = observation.remaining_seconds

    if planned is None or remaining is None or remaining > planned:
        return StartRun(
            started_at=observed_at,
            planned_runtime_seconds=planned,
            last_remaining_seconds=remaining,
            confidence=Confidence.LOW,
        )

    elapsed = planned - remaining
    limit = 2 * (previous_nextpoll_seconds or 0)
    clamped = max(0, min(elapsed, limit)) if limit > 0 else 0
    confidence = Confidence.HIGH if clamped == elapsed else Confidence.MEDIUM
    return StartRun(
        started_at=observed_at - timedelta(seconds=clamped),
        planned_runtime_seconds=planned,
        last_remaining_seconds=remaining,
        confidence=confidence,
    )


def plan_extend(
    state: OpenRunState, observation: ZoneObservation, *, observed_at: datetime
) -> ExtendRun:
    """تحديث حدث مفتوح ما زال يعمل (SRS §9.5)."""
    return ExtendRun(
        last_running_at=observed_at,
        last_remaining_seconds=observation.remaining_seconds
        if observation.remaining_seconds is not None
        else state.last_remaining_seconds,
        planned_runtime_seconds=observation.planned_seconds
        or state.planned_runtime_seconds,
    )


def plan_close(
    state: OpenRunState,
    *,
    observed_at: datetime,
    gap_since_last_seen: bool = False,
) -> CloseRun:
    """يحسب نهاية حدث عند الانتقال ``Running → Idle`` (SRS §9.6).

    نعلم يقينًا أن المحبس كان يعمل عند ``last_running_at`` وأنه توقف قبل
    ``observed_at``. حين يكون المتبقي المرصود معقولًا نأخذ الأقرب من
    الاثنين:

    ``ended_at = min(observed_at, last_running_at + last_remaining)``

    وإلا فالنهاية هي لحظة الرصد مع خفض الثقة، لأننا لا نملك ما يثبت
    اللحظة داخل الفجوة.
    """
    confidence = state.confidence
    remaining = state.last_remaining_seconds

    if remaining is not None and 0 <= remaining <= MAX_REASONABLE_RUNTIME_SECONDS:
        candidate = state.last_running_at + timedelta(seconds=remaining)
        ended_at = min(observed_at, candidate)
    else:
        ended_at = observed_at
        confidence = confidence.downgrade()

    if gap_since_last_seen:
        # مرّ انقطاع بين آخر رصد والآن: لا نعرف متى توقف فعلًا.
        confidence = confidence.downgrade()

    if ended_at < state.started_at:
        ended_at = state.started_at

    runtime = int((ended_at - state.started_at).total_seconds())
    if runtime > MAX_REASONABLE_RUNTIME_SECONDS:
        # مدة غير معقولة تعني حدثًا عالقًا لا ريًّا طويلًا؛ نقصّه ونخفض الثقة.
        runtime = MAX_REASONABLE_RUNTIME_SECONDS
        ended_at = state.started_at + timedelta(seconds=runtime)
        confidence = Confidence.LOW

    return CloseRun(ended_at=ended_at, runtime_seconds=max(0, runtime), confidence=confidence)


def plan_stale_close(state: OpenRunState, *, now: datetime) -> CloseRun:
    """إغلاق حدث عالق لم يعد يُرى — بلا اختراع زمن (SRS §17، §20).

    الفرق عن :func:`plan_close` أن هذا الإغلاق لا يحدث عند رصد توقّف، بل
    عند غياب الرصد أصلًا: الـWorker توقف أو انقطعت الشبكة. لذلك لا يُحتسب
    إلا ما رُصد فعلًا (وامتداده المعلوم بالمتبقي)، وتُخفض الثقة دائمًا.
    """
    remaining = state.last_remaining_seconds or 0
    if 0 <= remaining <= MAX_REASONABLE_RUNTIME_SECONDS:
        ended_at = min(now, state.last_running_at + timedelta(seconds=remaining))
    else:
        ended_at = state.last_running_at
    if ended_at < state.started_at:
        ended_at = state.started_at
    runtime = min(
        MAX_REASONABLE_RUNTIME_SECONDS,
        max(0, int((ended_at - state.started_at).total_seconds())),
    )
    return CloseRun(
        ended_at=state.started_at + timedelta(seconds=runtime),
        runtime_seconds=runtime,
        confidence=state.confidence.downgrade(),
    )


def gap_threshold_seconds(last_nextpoll_seconds: int | None) -> float:
    """متى نعتبر الصمت انقطاعًا: ``max(3 × nextpoll, 180)`` (SRS §9.7)."""
    return max(3 * (last_nextpoll_seconds or 60), 180)

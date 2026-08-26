"""إعدادات التطبيق — تُقرأ من البيئة فقط، ولا يُخزَّن أي سر في المستودع.

كل قيمة هنا لها اسم مطابق لما ورد في `.env.example` (SRS §16). الأسرار
(`HYDRAWISE_API_KEY`, `OPENWA_API_KEY`, `APP_SECRET_KEY`, ...) تُغلَّف بنوع
:class:`pydantic.SecretStr` حتى لا تظهر في التتبّع أو السجلات بالخطأ.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """كل ما يحتاجه التطبيق من البيئة."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- التطبيق -----------------------------------------------------
    app_env: str = "production"
    app_name: str = "Hydrawise Irrigation Reporting"
    app_secret_key: SecretStr = SecretStr("insecure-development-key-change-me")
    report_timezone: str = "Asia/Muscat"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # -- قاعدة البيانات ----------------------------------------------
    database_url: str = "postgresql+psycopg://hydrawise:hydrawise@localhost:5432/hydrawise"

    # -- تكامل Hydrawise (قراءة فقط) ---------------------------------
    hydrawise_api_base: str = "https://api.hydrawise.com/api/v1/"
    hydrawise_api_key: SecretStr = SecretStr("")
    hydrawise_controller_id: int | None = None
    hydrawise_http_timeout_seconds: float = 20.0
    hydrawise_raw_sample_retention_days: int = 90
    #: الحد الأدنى المسموح به بين طلبين حتى لو أعاد الخادم `nextpoll` أصغر.
    hydrawise_min_poll_seconds: int = 30
    #: يُستخدم عند غياب `nextpoll` أو خروجه عن النطاق المعقول (SRS §24.5).
    hydrawise_default_poll_seconds: int = 60
    hydrawise_max_poll_seconds: int = 900

    # -- افتراضات الحساب ---------------------------------------------
    default_flow_min_lpm: float = 80.0
    default_flow_lpm: float = 140.0
    default_flow_max_lpm: float = 200.0
    pump_rated_kw: float = 4.0
    pump_estimated_input_kw: float = 4.0
    well_depth_m: float = 90.0

    # -- الجدولة -----------------------------------------------------
    monthly_report_cron: str = "15 0 1 * *"
    daily_report_cron: str = "10 0 * * *"

    # -- OpenWA ------------------------------------------------------
    openwa_enabled: bool = False
    openwa_base_url: str = ""
    openwa_api_key: SecretStr = SecretStr("")
    openwa_session_id: SecretStr = SecretStr("")
    openwa_recipient: str = ""
    openwa_auth_header: str = "x-api-key"
    openwa_auth_scheme: str = ""
    openwa_send_path: str = "/api/sessions/{session_id}/messages/send-text"
    openwa_recipient_field: str = "chatId"
    openwa_text_field: str = "text"
    openwa_recipient_suffix: str = "@c.us"
    openwa_timeout_seconds: float = 20.0
    openwa_max_attempts: int = 3

    # -- التقارير والنسخ الاحتياطي -----------------------------------
    report_public_link_ttl_days: int = 40
    backup_retention_days: int = 30

    # -- التجهيز الأول ------------------------------------------------
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr = SecretStr("")

    @field_validator("report_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # يرفع الخطأ مبكرًا إن كان الاسم غير صالح
        return value

    @field_validator("hydrawise_api_base")
    @classmethod
    def _normalise_base(cls, value: str) -> str:
        return value.rstrip("/") + "/"

    @field_validator("public_base_url")
    @classmethod
    def _strip_base(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("hydrawise_controller_id", mode="before")
    @classmethod
    def _blank_controller_id(cls, value: object) -> object:
        # المتغير في .env قد يكون فارغًا، وهذا يعني "لا تحدد كنترولرًا".
        if value in ("", None):
            return None
        return value

    # -- مشتقات مساعدة ------------------------------------------------
    @property
    def tzinfo(self) -> ZoneInfo:
        """المنطقة الزمنية التي تُحسب بها حدود اليوم والشهر."""
        return ZoneInfo(self.report_timezone)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def hydrawise_configured(self) -> bool:
        return bool(self.hydrawise_api_key.get_secret_value().strip())

    @property
    def openwa_configured(self) -> bool:
        return bool(
            self.openwa_enabled
            and self.openwa_base_url
            and self.openwa_session_id.get_secret_value()
            and self.openwa_recipient
        )

    def secret_values(self) -> list[str]:
        """كل الأسرار الحية — يستخدمها منقّح السجلات لإخفائها."""
        candidates = [
            self.hydrawise_api_key.get_secret_value(),
            self.openwa_api_key.get_secret_value(),
            self.openwa_session_id.get_secret_value(),
            self.app_secret_key.get_secret_value(),
            self.bootstrap_admin_password.get_secret_value(),
        ]
        return [value for value in candidates if value and len(value) >= 6]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """الإعدادات المحمّلة مرة واحدة لكل عملية."""
    return Settings()


def reset_settings_cache() -> None:
    """تُستخدم في الاختبارات بعد تغيير متغيرات البيئة."""
    get_settings.cache_clear()

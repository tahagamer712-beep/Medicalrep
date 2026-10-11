"""WhatsApp Cloud API settings — environment only, never hardcoded.

Required (production):
    WHATSAPP_VERIFY_TOKEN      token you invent; pasted into the Meta
                               dashboard webhook verification field
    WHATSAPP_APP_SECRET        Meta App secret (Dashboard > App settings)
    WHATSAPP_PHONE_NUMBER_ID   phone_number_id from the WhatsApp dashboard
    WHATSAPP_ACCESS_TOKEN      System User token with whatsapp_business_messaging

Optional:
    WHATSAPP_API_VERSION       default "v21.0"

No real values belong in code, docs, or logs. Use .env (chmod 600, gitignored).
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WhatsAppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WHATSAPP_", env_file=".env",
        env_file_encoding="utf-8", extra="ignore",
    )

    verify_token: str = ""
    app_secret: str = ""
    phone_number_id: str = ""
    access_token: str = ""
    api_version: str = "v21.0"

    def is_configured(self) -> bool:
        """True only when every production credential is present."""
        return all([self.verify_token, self.app_secret,
                    self.phone_number_id, self.access_token])

    def redacted(self) -> dict:
        """Safe summary for logs/health checks — never the secrets."""
        def tail(v: str) -> str:
            return ("…" + v[-4:]) if v else "(unset)"
        return {
            "verify_token": "(set)" if self.verify_token else "(unset)",
            "app_secret": "(set)" if self.app_secret else "(unset)",
            "phone_number_id": tail(self.phone_number_id),
            "access_token": "(set)" if self.access_token else "(unset)",
            "api_version": self.api_version,
        }


whatsapp_settings = WhatsAppSettings()

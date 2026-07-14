from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str
    gemini_api_key: str
    groq_api_key: str
    supabase_url: str
    supabase_service_key: str
    supabase_anon_key: str
    supabase_readonly_password: str
    supabase_db_url: str
    telegram_bot_token: str
    telegram_chat_id_rajat: str
    telegram_chat_id_ayushi: str = ""   # empty until Ayushi's chat id is known
    telegram_alert_bot_token: str
    telegram_alert_chat_id: str
    healthcheck_url: str
    finance_inbox_path: str = "/Users/rajat/finance-inbox"
    finance_backup_path: str = "/Users/rajat/finance-backups"
    finance_log_path: str = "/Users/rajat/finance-logs"
    timezone: str = "Asia/Kolkata"


settings = Settings()

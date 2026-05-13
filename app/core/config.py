from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    ollama_url: str
    telegram_bot_token: str
    telegram_webhook_secret: str = ""
    ollama_model: str = "llama3.1:8b"
    whisper_model: str = "medium"
    basic_auth_user: str = ""
    basic_auth_pass: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()

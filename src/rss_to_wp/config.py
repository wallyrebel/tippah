"""Configuration models and loading."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pendulum
import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeedConfig(BaseModel):
    """Configuration for a single RSS feed."""

    name: str
    url: str
    default_category: Optional[str] = None
    default_tags: list[str] = Field(default_factory=list)
    max_per_run: int = 5
    use_original_title: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Ensure URL starts with http(s)."""
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"Invalid URL: {v}")
        return v


class WeeklyColumnConfig(BaseModel):
    """Configuration for a weekly auto-generated columnist post."""

    name: str
    slug: str
    column_type: str
    default_category: str = "Opinion"
    default_tags: list[str] = Field(default_factory=list)
    day_of_week: str = "monday"
    context_feeds: list[str] = Field(default_factory=list)
    context_hours: int = 168
    max_context_entries: int = 8

    @field_validator("column_type")
    @classmethod
    def validate_column_type(cls, v: str) -> str:
        """Validate supported columnist types."""
        allowed = {"christian", "human_interest", "sports"}
        value = v.strip().lower()
        if value not in allowed:
            raise ValueError(f"Invalid column_type: {v}. Allowed: {sorted(allowed)}")
        return value

    @field_validator("day_of_week")
    @classmethod
    def validate_day_of_week(cls, v: str) -> str:
        """Normalize and validate day of week."""
        allowed = {
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        }
        value = v.strip().lower()
        if value not in allowed:
            raise ValueError(f"Invalid day_of_week: {v}. Allowed: {sorted(allowed)}")
        return value

    @field_validator("context_feeds")
    @classmethod
    def validate_context_feeds(cls, feeds: list[str]) -> list[str]:
        """Ensure context feed URLs are valid."""
        for feed in feeds:
            if not feed.startswith(("http://", "https://")):
                raise ValueError(f"Invalid context feed URL: {feed}")
        return feeds


class FeedsConfig(BaseModel):
    """Container for all feed configurations."""

    feeds: list[FeedConfig] = Field(default_factory=list)
    weekly_columns: list[WeeklyColumnConfig] = Field(default_factory=list)


class AppSettings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = Field(..., description="OpenAI API key")
    openai_model: str = Field(default="gpt-5-mini", description="Primary OpenAI model")
    openai_fallback_model: str = Field(
        default="gpt-4.1-nano",
        description="Fallback OpenAI model",
    )

    # WordPress
    wordpress_base_url: str = Field(..., description="WordPress site URL")
    wordpress_username: str = Field(..., description="WordPress username")
    wordpress_app_password: str = Field(..., description="WordPress application password")
    wordpress_post_status: str = Field(default="publish", description="Post status")

    # Image fallback providers (optional)
    pexels_api_key: Optional[str] = Field(default=None, description="Pexels API key")
    unsplash_access_key: Optional[str] = Field(default=None, description="Unsplash access key")

    # Logging & Timezone
    log_level: str = Field(default="INFO", description="Log level")
    log_file: Optional[str] = Field(default=None, description="Optional log file path")
    timezone: str = Field(default="UTC", description="Timezone for date calculations")

    # Category daily publish caps
    max_daily_mississippi_posts: int = Field(
        default=8,
        description="Daily publish cap for Mississippi News category",
    )
    max_daily_national_posts: int = Field(
        default=8,
        description="Daily publish cap for National News category",
    )

    # Email notifications (optional)
    smtp_email: Optional[str] = Field(default=None, description="SMTP sender email")
    smtp_password: Optional[str] = Field(default=None, description="SMTP password/app password")
    notification_email: Optional[str] = Field(default=None, description="Email to send notifications to")

    @field_validator("wordpress_base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        """Remove trailing slash from URL."""
        return v.rstrip("/")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Validate timezone is valid."""
        try:
            pendulum.timezone(v)
        except Exception:
            raise ValueError(f"Invalid timezone: {v}")
        return v


def load_feeds_config(config_path: str | Path) -> FeedsConfig:
    """Load feeds configuration from YAML file.

    Args:
        config_path: Path to the feeds.yaml configuration file.

    Returns:
        FeedsConfig object with validated feed configurations.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        ValidationError: If config is invalid.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return FeedsConfig.model_validate(data or {"feeds": []})


def get_app_settings() -> AppSettings:
    """Load application settings from environment.

    Returns:
        AppSettings object with validated settings.
    """
    return AppSettings()


def get_data_dir() -> Path:
    """Get the data directory for runtime files.

    Creates the directory if it doesn't exist.

    Returns:
        Path to data directory.
    """
    # Use directory relative to the project root
    data_dir = Path(__file__).parent.parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir

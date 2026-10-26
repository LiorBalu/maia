"""Default per-platform bot settings for a tenant."""

from maia.models.tenants import PlatformBotSettings, WorkingHours

PLATFORMS = ["web", "email", "telephone", "waha"]
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def create_default_working_hours() -> list:
    """Monday-Friday 09:00-17:00 enabled, Saturday/Sunday disabled."""
    return [
        WorkingHours(day=day, start_time="09:00", end_time="17:00", enabled=day not in ("saturday", "sunday"))
        for day in DAYS
    ]


def create_default_platform_settings() -> list:
    """One ``always_bot`` entry per supported platform, with the default working hours."""
    working_hours = create_default_working_hours()
    return [
        PlatformBotSettings(platform=platform, mode="always_bot", working_hours=working_hours, timezone="Asia/Jerusalem")
        for platform in PLATFORMS
    ]

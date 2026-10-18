from datetime import datetime, timedelta, timezone


class ClockService:
    """Single source for local and UTC time.

    Games schedule in UTC while users think in local time, so every
    conversion has to state explicitly which zone a naive value belongs to.
    """

    TIME_FORMAT = "%H:%M:%S"
    DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

    def now_local(
        self,
    ) -> datetime:

        return datetime.now().astimezone()

    def now_utc(
        self,
    ) -> datetime:

        return datetime.now(
            timezone.utc
        )

    def local_to_utc(
        self,
        value: datetime,
    ) -> datetime:
        """Naive input is interpreted as local time."""

        if value.tzinfo is None:
            value = value.astimezone()

        return value.astimezone(
            timezone.utc
        )

    def utc_to_local(
        self,
        value: datetime,
    ) -> datetime:
        """Naive input is interpreted as UTC."""

        if value.tzinfo is None:
            value = value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone()

    def utc_offset(
        self,
    ) -> timedelta:

        return (
            self.now_local().utcoffset()
            or timedelta()
        )

    def utc_offset_label(
        self,
    ) -> str:

        total_minutes = int(
            self.utc_offset().total_seconds() // 60
        )

        sign = "+" if total_minutes >= 0 else "-"

        total_minutes = abs(total_minutes)

        return (
            f"UTC{sign}"
            f"{total_minutes // 60:02d}:"
            f"{total_minutes % 60:02d}"
        )

    def format_local(
        self,
        value: datetime | None = None,
        time_format: str | None = None,
    ) -> str:

        value = (
            self.now_local()
            if value is None
            else self.utc_to_local(value)
        )

        return value.strftime(
            time_format or self.TIME_FORMAT
        )

    def format_utc(
        self,
        value: datetime | None = None,
        time_format: str | None = None,
    ) -> str:

        value = (
            self.now_utc()
            if value is None
            else self.local_to_utc(value)
        )

        return value.strftime(
            time_format or self.TIME_FORMAT
        )

    def format_pair(
        self,
        time_format: str | None = None,
    ) -> str:

        return (
            f"local Time {self.format_local(time_format=time_format)}"
            f" | UTC Time {self.format_utc(time_format=time_format)}"
        )

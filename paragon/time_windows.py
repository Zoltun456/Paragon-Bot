from datetime import datetime, timedelta, timezone
from .config import LOCAL_TZ

def _today_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def _date_key(dt_local: datetime) -> str:
    return dt_local.strftime("%Y-%m-%d")

def _is_active_hours_local(dt_local: datetime) -> bool:
    wd = dt_local.weekday()  # Mon=0..Sun=6
    hour = dt_local.hour
    if wd <= 4:
        return 17 <= hour < 24   # Weekdays: 5pm-midnight
    return 10 <= hour < 24       # Weekends: 10am-midnight

def is_active_hours(utc_dt: datetime) -> bool:
    return _is_active_hours_local(utc_dt.astimezone(LOCAL_TZ))

def count_active_minutes(start_utc: datetime, end_utc: datetime) -> int:
    if end_utc <= start_utc:
        return 0
    t = start_utc.replace(second=0, microsecond=0)
    if t < start_utc:
        t = t + timedelta(minutes=1)
    total = int((end_utc - t).total_seconds() // 60)
    total = max(0, total)
    count = 0
    for i in range(total + 1):
        m = t + timedelta(minutes=i)
        if m >= end_utc: break
        if is_active_hours(m): count += 1
    return count
"""
A historical-event source with no external dependency — events supplied
directly (an admin/manual import, a CSV parsed elsewhere, synthetic test
data). This is both a practical fallback for a fleet without Ymane access
and the source used for automated tests (see tests/test_historical.py) and
for POST /historical/import?source=manual.
"""
from datetime import date
from typing import List

from app.services.historical.models import HistoricalEvent
from app.services.historical.sources.base import HistoricalEventSource


class ManualEventSource(HistoricalEventSource):
    name = "manual"

    def __init__(self, events: List[HistoricalEvent]):
        self._events = events

    async def fetch(self, start: date, end: date) -> List[HistoricalEvent]:
        return [e for e in self._events if start <= e.occurred_at.date() <= end]

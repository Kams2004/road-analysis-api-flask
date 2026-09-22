"""
HistoricalEventSource — the interface every historical-data provider
implements (Ymane, manual/CSV import, future sources). historical_service.py
only ever talks to this interface, never a vendor-specific client directly —
the same discipline Step 5 applied to notifications ("the collision engine
must not depend on the mobile app") applied here to where historical data
comes from. Swapping or adding a source never touches the import/matching/
aggregation pipeline.
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import List

from app.services.historical.models import HistoricalEvent


class HistoricalEventSource(ABC):
    name: str

    @abstractmethod
    async def fetch(self, start: date, end: date) -> List[HistoricalEvent]:
        """Every event in [start, end] this source has, already translated
        into the canonical HistoricalEvent shape."""
        raise NotImplementedError

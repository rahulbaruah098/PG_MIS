from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class MonthlySubmission:
    pg_id: str
    month: int
    year: int
    submitted: bool = False
    submitted_on: Optional[datetime] = None

    def submit(self):
        self.submitted = True
        self.submitted_on = datetime.utcnow()

    def is_locked(self) -> bool:
        return bool(self.submitted)

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict

class LoanEngine:
    @staticmethod
    def calculate_emi(principal: float, annual_interest_rate: float, tenure_months: int) -> float:
        if tenure_months <= 0:
            return 0.0
        r = (annual_interest_rate / 12.0) / 100.0
        if r == 0:
            return round(principal / tenure_months, 2)
        emi = principal * r * (1 + r) ** tenure_months / ((1 + r) ** tenure_months - 1)
        return round(emi, 2)

    @staticmethod
    def amortization_schedule(principal: float, annual_interest_rate: float, tenure_months: int) -> List[Dict]:
        schedule: List[Dict] = []
        if principal <= 0 or tenure_months <= 0:
            return schedule
        emi = LoanEngine.calculate_emi(principal, annual_interest_rate, tenure_months)
        balance = round(float(principal), 2)
        r = (annual_interest_rate / 12.0) / 100.0

        for m in range(1, tenure_months + 1):
            interest = round(balance * r, 2)
            principal_component = round(emi - interest, 2)
            if principal_component > balance:
                principal_component = balance
            balance = round(balance - principal_component, 2)
            schedule.append({
                "month": m,
                "emi": emi,
                "principal": principal_component,
                "interest": interest,
                "outstanding": balance
            })
            if balance <= 0:
                break
        return schedule

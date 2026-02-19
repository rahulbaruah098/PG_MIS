class KPIEngine:
    @staticmethod
    def calculate_turnover(internal_total: float, market_total: float) -> float:
        return round((internal_total or 0) + (market_total or 0), 2)

    @staticmethod
    def calculate_member_percentage(active_members: int, total_members: int) -> float:
        if not total_members:
            return 0.0
        return round((active_members / total_members) * 100.0, 2)

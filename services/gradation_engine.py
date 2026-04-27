from typing import Dict, Tuple

class GradationEngine:
    @staticmethod
    def calculate_grade(scores_dict: Dict[str, float], turnover_percentage: float) -> Tuple[float, str]:
        total_score = float(sum(scores_dict.values())) if scores_dict else 0.0

        # Non‑negotiable per manual: turnover must have >= 75% marks condition
        if turnover_percentage < 75:
            # still return score, but force C
            return round(total_score, 2), "C"

        if total_score >= 75:
            grade = "A"
        elif total_score >= 60:
            grade = "B"
        else:
            grade = "C"
        return round(total_score, 2), grade

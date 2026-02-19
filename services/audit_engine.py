from datetime import datetime
from typing import Any, Dict, Optional

class AuditLogger:
    @staticmethod
    def log(action: str, user_id: str, entity: str, entity_id: Any, meta: Optional[Dict]=None) -> Dict:
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": str(user_id) if user_id is not None else None,
            "action": action,
            "entity": entity,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "meta": meta or {}
        }

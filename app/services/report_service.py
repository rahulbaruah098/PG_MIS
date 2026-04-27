import csv
import io
import zipfile
from typing import Any, Dict, List, Tuple
from flask import Response
from app.utils import safe_objectid
from app.services.filter_engine import FilterEngine

class ReportService:
    """Centralized report generation helpers.

    - Standardizes filters + jurisdiction enforcement via FilterEngine
    - Provides CSV and ZIP report builders without altering routes.
    """

    @staticmethod
    def enforced_filters() -> Dict[str, Any]:
        return FilterEngine.enforced_filters()

    @staticmethod
    def csv_response(filename: str, rows: List[Dict[str, Any]], fieldnames: List[str]) -> Response:
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})
        data = out.getvalue().encode("utf-8-sig")
        return Response(
            data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @staticmethod
    def zip_csv_response(filename: str, files: List[Tuple[str, List[Dict[str, Any]], List[str]]]) -> Response:
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            for csv_name, rows, fields in files:
                out = io.StringIO()
                w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
                z.writestr(csv_name, out.getvalue().encode("utf-8-sig"))
        mem.seek(0)
        return Response(
            mem.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

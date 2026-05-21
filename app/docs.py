from flask import jsonify, render_template_string
import re

REDOC_HTML = """
<!DOCTYPE html>
<html>
  <head>
    <title>PG MIS API Docs</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
    <style>
      body { margin: 0; padding: 0; }
    </style>
  </head>
  <body>
    <redoc
      spec-url='/apispec.json'
      expand-responses="200"
      required-props-first="true"
      hide-download-button="false"
      theme='{
        "colors": {
          "primary": { "main": "#1a7f4b" }
        },
        "typography": {
          "fontSize": "15px",
          "fontFamily": "Roboto, sans-serif",
          "headings": { "fontFamily": "Montserrat, sans-serif" }
        },
        "sidebar": {
          "backgroundColor": "#1a1a2e",
          "textColor": "#ffffff"
        }
      }'>
    </redoc>
    <script src="https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js"></script>
  </body>
</html>
"""

def build_spec(app):
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "PG MIS API",
            "version": "1.0.0",
            "description": "PG Management Information System — REST API Reference"
        },
        "paths": {},
        "tags": []
    }

    tag_set = set()
    SKIP_ENDPOINTS = {"static", "swagger_ui.static", "apispec_json", "redoc_ui"}
    SKIP_PREFIXES = ("/docs", "/static")

    for rule in app.url_map.iter_rules():
        # Skip internal/docs/static routes
        if rule.endpoint in SKIP_ENDPOINTS:
            continue
        if any(rule.rule.startswith(p) for p in SKIP_PREFIXES):
            continue
        if rule.rule in ("/apispec.json",):
            continue

        # Convert Flask <param> → OpenAPI {param}
        path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule.rule)
        methods = [m for m in rule.methods if m not in ("HEAD", "OPTIONS")]

        if not methods:
            continue

        # Tag from first URL segment
        parts = rule.rule.strip("/").split("/")
        tag = parts[0].capitalize() if parts and parts[0] else "General"
        tag_set.add(tag)

        if path not in spec["paths"]:
            spec["paths"][path] = {}

        view_func = app.view_functions.get(rule.endpoint)
        docstring = (view_func.__doc__ or "").strip() if view_func else ""

        # Auto-detect path parameters
        path_params = re.findall(r"\{([^}]+)\}", path)

        for method in methods:
            operation = {
                "tags": [tag],
                "summary": rule.endpoint.split(".")[-1].replace("_", " ").title(),
                "description": docstring,
                "responses": {
                    "200": {"description": "Success"},
                    "400": {"description": "Bad Request"},
                    "401": {"description": "Unauthorized"},
                    "403": {"description": "Forbidden"},
                    "404": {"description": "Not Found"},
                    "500": {"description": "Internal Server Error"},
                }
            }

            if path_params:
                operation["parameters"] = [
                    {
                        "name": p,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": f"{p.replace('_', ' ').title()} ID"
                    }
                    for p in path_params
                ]

            spec["paths"][path][method.lower()] = operation

    spec["tags"] = [
        {"name": t, "description": f"{t} related endpoints"}
        for t in sorted(tag_set)
    ]
    return spec


def register_docs(app):

    @app.route("/apispec.json", endpoint="apispec_json", strict_slashes=False)
    def apispec_json():
        resp = jsonify(build_spec(app))
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/docs", endpoint="redoc_ui", strict_slashes=False)
    def redoc_ui():
        return render_template_string(REDOC_HTML)
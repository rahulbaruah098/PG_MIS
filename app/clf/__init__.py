from flask import Blueprint

clf_bp = Blueprint(
    "clf",
    __name__,
    url_prefix="/clf",
    template_folder="../templates"
)

from . import routes
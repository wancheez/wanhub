from fastapi.templating import Jinja2Templates

from app.core.config import TEMPLATES_DIR

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

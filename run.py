"""Start the app: python run.py   (DEAL_HUNTER_WORKSPACE=demo python run.py for the demo workspace)."""
import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    cfg = get_settings()
    uvicorn.run("app.main:create_app", factory=True, host=cfg.host, port=cfg.port)

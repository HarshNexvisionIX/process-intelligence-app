"""python -m app.api  ->  the web API and (when built) the web UI on http://127.0.0.1:8008"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.api.server:app", host=os.environ.get("PM_API_HOST", "127.0.0.1"),
                port=int(os.environ.get("PM_API_PORT", "8008")), reload=False)

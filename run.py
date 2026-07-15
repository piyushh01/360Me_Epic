import os
from app import create_app
from app.config import EPIC_SNAPSHOT_DIR

app = create_app()

if __name__ == "__main__":
    if not os.path.exists(EPIC_SNAPSHOT_DIR):
        os.makedirs(EPIC_SNAPSHOT_DIR)
    app.run(host="0.0.0.0", port=3000)

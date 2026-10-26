"""Process-wide settings read from the environment: Mongo URI, JWT secret and token lifetime."""

import os
from dotenv import load_dotenv

load_dotenv()


# MONGO DB
MONGO_URI = os.getenv("MONGO_URI")

JWT_SECRET = os.environ["MAIA_JWT_SECRET"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

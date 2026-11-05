"""Process-wide settings read from the environment: Mongo URI, JWT secret and token lifetime."""

import os
from dotenv import load_dotenv

load_dotenv()


# MONGO DB
MONGO_CONNECTION_STRING = os.getenv("MONGO_URI")

JWT_SIGNING_SECRET = os.environ["MAIA_JWT_SECRET"]
JWT_SIGNING_ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME_MINUTES = 60 * 24  # 24 hours

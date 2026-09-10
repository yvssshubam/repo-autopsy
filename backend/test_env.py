import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"

print("Looking for:", env_path)
print("Exists:", env_path.exists())

print("\nRaw .env contents:")

if env_path.exists():
    print(env_path.read_text(encoding="utf-8"))

load_dotenv(env_path, override=True)

print("\nEnvironment variable check:")
print("GITHUB_TOKEN exists:", "GITHUB_TOKEN" in os.environ)
print("Token loaded:", bool(os.getenv("GITHUB_TOKEN")))

if os.getenv("GITHUB_TOKEN"):
    print(
        "Token starts with:",
        os.getenv("GITHUB_TOKEN")[:10] + "..."
    )
import os
from pathlib import Path
from dotenv import load_dotenv
import httpx

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

token = os.getenv("GITHUB_TOKEN")

print("Token loaded:", bool(token))

if not token:
    raise SystemExit("GITHUB_TOKEN was not loaded.")

headers = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {token}"
}

url = "https://api.github.com/repos/facebook/react"

response = httpx.get(
    url,
    headers=headers,
    timeout=10,
    follow_redirects=True
)

print("Status:", response.status_code)
print("Response:", response.text)
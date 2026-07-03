import os
import json
import urllib.request
import urllib.error
from pathlib import Path

# Load dotenv
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

api_key = os.environ.get("GEMINI_API_KEY")
model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

print(f"Using model: {model_name}")
print(f"API key configured: {bool(api_key)}")

if not api_key:
    print("Error: GEMINI_API_KEY not found in env.")
    sys.exit(1)

url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

payload = {
    "contents": [
        {"role": "user", "parts": [{"text": "serenity目前那些股票有看好?"}]}
    ],
    "systemInstruction": {
        "parts": [{"text": "You are a helpful assistant."}]
    },
    "generationConfig": {
        "temperature": 0.3
    }
}

req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode('utf-8'),
    headers={'Content-Type': 'application/json'}
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print("Success! Response:")
        print(resp.read().decode('utf-8')[:300])
except urllib.error.HTTPError as e:
    print(f"HTTPError: {e.code} {e.reason}")
    print("Response body:")
    print(e.read().decode('utf-8'))
except Exception as e:
    print(f"Error: {e}")

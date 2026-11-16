import requests
from bs4 import BeautifulSoup
import json

# Step 1: Fetch a USYD unit page live
url = "https://www.sydney.edu.au/units/INFO1110"
headers = {"User-Agent": "Mozilla/5.0"}

print("Fetching USYD handbook page...")
page = requests.get(url, headers=headers, timeout=10)
soup = BeautifulSoup(page.text, "html.parser")

# Extract readable text, trim to avoid overloading context
raw_text = soup.get_text(separator="\n", strip=True)
handbook_content = raw_text[:8000]

print("Done. Sending to Qwen3-8B...\n")

# Step 2: Pass fetched content to Qwen3-8B via Ollama
prompt = f"""The following is content fetched live from the University of Sydney 
handbook for a unit:

{handbook_content}

Based only on the information above, answer the following:
1. What is the name and code of this unit?
2. What are the prerequisites?
3. How many credit points is it worth?
4. What semester is it available in?
"""

response = requests.post(
    "http://localhost:11434/api/generate",
    json={
        "model": "qwen3:8b",
        "prompt": prompt,
        "stream": False
    }
)

result = response.json()
print("=== Qwen3-8B Response ===")
print(result["response"])
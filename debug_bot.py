import requests
import json
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PANEL_URL  = "https://185.23.18.192:2053/DijIfWXTeUOwJ0nIp6"
PANEL_USER = "soqwertysiewood"
PANEL_PASS = "20008080rusyA"  # ← замени

s = requests.Session()

r = s.post(f"{PANEL_URL}/login",
           json={"username": PANEL_USER, "password": PANEL_PASS},
           verify=False, timeout=10)
print("=== LOGIN ===")
print(r.status_code, r.text[:200])

# Перебираем известные endpoint'ы
endpoints = [
    "/xui/API/inbounds",
    "/panel/api/inbounds",
    "/api/inbounds",
    "/xui/inbound/list",
    "/xui/API/inbounds/list",
]

working = None
print("\n=== ПОИСК ENDPOINT ===")
for ep in endpoints:
    r = s.get(f"{PANEL_URL}{ep}", verify=False, timeout=10)
    preview = r.text[:120].replace("\n", " ")
    print(f"{ep}  status={r.status_code}  len={len(r.text)}  preview={preview!r}")
    if r.status_code == 200 and r.text.strip():
        try:
            d = r.json()
            if d.get("success"):
                working = (ep, d)
                print("  ^^^ РАБОТАЕТ ^^^")
        except Exception:
            pass

if not working:
    print("\n⚠️  Ни один endpoint не сработал. Скинь вывод выше.")
else:
    ep, data = working
    print(f"\n=== КЛИЕНТЫ из {ep} ===")
    print("inbounds:", len(data.get("obj", [])))
    for inbound in data.get("obj", []):
        print(f"\n--- id={inbound.get('id')} remark={inbound.get('remark')} ---")
        settings_raw = inbound.get("settings", "{}")
        try:
            settings = json.loads(settings_raw)
        except Exception:
            print("  settings не распарсился:", settings_raw[:200])
            continue
        clients = settings.get("clients", [])
        print(f"  клиентов: {len(clients)}")
        for c in clients[:5]:
            print("  КЛИЕНТ:", json.dumps(c, ensure_ascii=False))


# Socksy Dashboard
*A zero-friction, one-file Flask app that stress-tests your SOCKS5 proxies and visualises their health in real time.*

![made-with-python](https://img.shields.io/badge/Made%20with-Python%203.11+-blue.svg)

---

## ✨ Key Features
| Feature | Why it matters |
|---------|----------------|
| **One-file deployment** | Clone → `python app.py` → done. No folder sprawl, perfect for quick VPS or Docker spins. |
| **Auto-probing every 60 s** | A background APScheduler job hits every proxy in parallel (20 threads by default) and updates SQLite. |
| **RTT + success ratio** | See not just *if* a proxy works, but *how fast* it answers. |
| **Bootstrap + DataTables UI** | Sort, filter, paginate, and the URL remembers your table state for easy bookmarking. |
| **In-browser `proxies.txt` editor** | Paste new proxies, hit save, watch them populate instantly. |
| **Stat flush button** | Reset counts without touching the file. |
| **100 % clientless** | Uses `requests[socks]`; no local Tor/Privoxy glue required. |

---

## 🚀 Quick Start

```bash
# 1. Grab the code
git clone https://github.com/techroy23/Socksy-Dashboard
cd Socksy-Dashboard

# 2. Create a lightweight venv
## Linux   : python -m venv venv && source venv/bin/activate
## Windows : venv\Scripts\activate

# 3. Install exact, future-proof deps
pip install "flask==3.*" "SQLAlchemy==2.*" "requests[socks]==2.*" "APScheduler==3.*"

# 4. Fire it up
python app.py

# 5. Visit
http://127.0.0.1:3000
```

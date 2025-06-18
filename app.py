#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
# ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ###
# Proxy Dashboard – one-file Flask app
# ------------------------------------
# • Probes SOCKS5 proxies listed in `proxies.txt`
# • Stores pass/fail stats + RTT in SQLite (`proxies.db`)
# • Auto-probes every minute (APScheduler background job)
# • Shows Bootstrap-flavoured DataTables dashboard
# • Lets you edit `proxies.txt` in-browser
# • URL remembers page-length, sort column, sort direction, filter term
# 
# HOW TO RUN
# ----------
# python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
# pip install flask==3.* SQLAlchemy==2.* requests[socks]==2.* APScheduler==3.*
# python app.py
# ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ###
"""

# ────────────────────────────────────────────────────────────
# Standard library
import re, os, time, random, pathlib, concurrent.futures
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

# Third-party
import requests
from flask import (
    Flask, render_template_string, request,
    redirect, url_for, flash
)
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError

# ═════════════════════════════════ CONFIG ═══════════════════
TXT_FILE  = pathlib.Path("proxies.txt")       # list of proxies (one per line)
DB_URI    = "sqlite:///proxies.db"            # SQLite DB file
ENDPOINT  = "https://icanhazip.com"           # test URL
TIMEOUT   = 8                                 # seconds per proxy request
INTERVAL  = 60                                # seconds between probe rounds
POOLSIZE  = 20                                # concurrent worker threads

# Regex that accepts:
#   socks5://username:password@host:port
#   socks5://host:port
VALID_RE = re.compile(
    r"^socks5://"
    r"(?:[A-Za-z0-9_\-\.]+:[^@]+@)?"          # optional user:pass@
    r"[A-Za-z0-9\.\-]+:\d{2,5}$"              # host:port
)

TXT_FILE.touch(exist_ok=True)                 # ensure file exists

# ═══════════════════ SQLAlchemy ORM setup ═══════════════════
engine  = create_engine(DB_URI, future=True)
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base    = declarative_base()

class Proxy(Base):
    """
    ORM row that stores per-proxy statistics.

    Columns
    -------
    address : str   primary key, full socks5://… URI
    passed  : int   # of successful probes
    total   : int   # of total probes
    percent : float success ratio (0-100)
    last_ip : str|None  last IP string returned by endpoint
    rtt_ms  : float|None last round-trip time in milliseconds
    updated : datetime   last update time (UTC)
    """
    __tablename__ = "proxies"

    address = Column(String,  primary_key=True)
    passed  = Column(Integer, default=0, nullable=False)
    total   = Column(Integer, default=0, nullable=False)
    percent = Column(Float,   default=0.0, nullable=False)
    last_ip = Column(String,  nullable=True)
    rtt_ms  = Column(Float,   nullable=True)
    updated = Column(DateTime(timezone=True),
                     default=lambda: datetime.now(timezone.utc),
                     nullable=False)

    # Business logic ---------------------------------------------------------
    def mark(self, ok: bool, ip: str | None, rtt: float | None) -> None:
        """
        Update counters for this proxy after a probe.

        Parameters
        ----------
        ok  : bool          True if probe succeeded
        ip  : str | None    IP string returned by endpoint (None on failure)
        rtt : float | None  Round-trip time in ms (None on failure)
        """
        self.passed = self.passed or 0
        self.total  = self.total  or 0

        self.total += 1
        if ok:
            self.passed += 1
            self.last_ip, self.rtt_ms = ip, rtt
        else:
            self.last_ip = self.rtt_ms = None

        self.percent = (self.passed / self.total) * 100
        self.updated = datetime.now(timezone.utc)

Base.metadata.create_all(engine)

# ═══════════════════ helper functions ═══════════════════════
def read_proxies() -> List[str]:
    """Return raw lines (stripped) from proxies.txt."""
    with TXT_FILE.open() as fh:
        return [ln.strip() for ln in fh if ln.strip()]

def filtered(lines: List[str]) -> List[str]:
    """
    Remove duplicates + invalid lines.
    Returns a *unique* list preserving original order.
    """
    seen, out = set(), []
    for ln in lines:
        ln = ln.strip()
        if VALID_RE.match(ln) and ln not in seen:
            seen.add(ln)
            out.append(ln)
    return out

# ═══════════════════ probing logic ══════════════════════════
def single(addr: str) -> Tuple[str, bool, str | None, float | None]:
    """
    Probe one proxy. Returns (address, ok?, ip, rtt_ms).
    Uses blocking `requests` + SOCKS (provided by requests[socks]).
    """
    t0 = time.perf_counter()
    try:
        r = requests.get(
            ENDPOINT,
            proxies={"http": addr, "https": addr},
            timeout=TIMEOUT
        )
        ok  = (r.status_code == 200)
        rtt = (time.perf_counter() - t0) * 1_000
        ip  = r.text.strip() if ok else None
        return addr, ok, ip, (rtt if ok else None)
    except Exception:
        return addr, False, None, None

def commit(addr: str, ok: bool, ip: str | None, rtt: float | None) -> None:
    """
    INSERT or UPDATE row for `addr`, with tiny retry loop to avoid
    UNIQUE-constraint races (multiple threads can hit same proxy).
    """
    while True:
        with Session() as s:
            row = s.get(Proxy, addr) or Proxy(address=addr)
            row.mark(ok, ip, rtt)
            s.add(row)
            try:
                s.commit()
                return
            except IntegrityError:
                s.rollback()
                time.sleep(0.01 + random.random()*0.02)

def probe() -> None:
    """One full round: read list → filter → thread-probe → DB write."""
    proxies = filtered(read_proxies())
    if not proxies:
        return
    with concurrent.futures.ThreadPoolExecutor(POOLSIZE) as pool:
        for res in pool.map(single, proxies):
            commit(*res)

# ═══════════════════ Flask application ══════════════════════
app = Flask(__name__)
app.secret_key = os.urandom(16)        # needed for flash() messages

# Mapping of column names -> DataTables index
COL = {
    "ProxyAddress":0, "Passed":1, "Total":2, "Percentage":3,
    "LastIp":4, "RTT":5, "Updated":6
}

# ------------------  HTML snippets (Bootstrap + DataTables) --
BOOT_HEAD = """
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.datatables.net/1.13.6/css/dataTables.bootstrap5.min.css" rel="stylesheet">
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/dataTables.bootstrap5.min.js"></script>
"""

# Main dashboard page --------------------------------------------------------
TPL_HOME = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Proxy Dashboard</title>""" + BOOT_HEAD + """
<style>
 body{padding-top:2rem}
 .dataTables_filter label,
 .dataTables_length label{display:flex;align-items:center;gap:.35rem;margin:0;font-weight:600}
 .dataTables_filter input{width:auto}
 .dataTables_length select{width:auto}
</style></head><body>
<div class="container-xl">

{% with msgs=get_flashed_messages() %}
  {% if msgs %}
    <div class="alert alert-info alert-dismissible fade show" role="alert">
      {{ msgs[0] }}
      <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    </div>
  {% endif %}
{% endwith %}

<h2 class="mb-3">SOCKS Proxy Health
  <small class="text-muted">(auto-refresh 60&nbsp;sec)</small>
</h2>

<!-- Row: “Show entries” + “Filter” -->
<div class="d-flex justify-content-between mb-2">
  <div id="lengthHolder"></div>
  <div id="filterHolder"></div>
</div>

<!-- Row: info text + pagination -->
<div class="d-flex justify-content-between mb-2">
  <div id="infoHolder"></div>
  <nav id="paginateHolder"></nav>
</div>

<!-- Buttons -->
<div class="mb-3 d-flex gap-2">
  <form method="post" action="{{ url_for('flush') }}"
        onsubmit="return confirm('Reset all statistics?');">
    <button class="btn btn-danger btn-sm">Flush DB</button>
  </form>
  <a href="{{ url_for('edit') }}" class="btn btn-secondary btn-sm">Edit proxies</a>
</div>

<table id="tbl" class="table table-striped table-bordered table-sm w-100">
<thead class="table-light"><tr>
  <th>ProxyAddress</th><th>Passed</th><th>Total</th><th>Percentage</th>
  <th>LastIp</th><th>RTT</th><th>Updated</th>
</tr></thead><tbody>
{% for p in data %}
  <tr>
    <td>{{ p.address }}</td><td>{{ p.passed }}</td><td>{{ p.total }}</td>
    <td>{{ '%.1f' % p.percent }}</td><td>{{ p.last_ip or '' }}</td>
    <td>{{ '%.1f' % p.rtt_ms if p.rtt_ms is not none else '' }}</td>
    <td>{{ p.updated.strftime('%Y-%m-%d %H:%M:%S') }}</td>
  </tr>
{% endfor %}
</tbody></table>

{% if not has_proxies %}
  <footer class="text-danger mt-3">
    No proxies found. Use “Edit proxies” to add valid
    <code>socks5://…</code> lines.
  </footer>
{% endif %}
</div>

<script>
const initRows={{ rows }}, initCol={{ order_col }},
      initDir="{{ order_dir }}", initFilt="{{ filter_val|e }}",
      names=["ProxyAddress","Passed","Total","Percentage","LastIp","RTT","Updated"];

// DataTable init + Bootstrap skin
const tbl=$('#tbl').DataTable({
  pageLength:initRows,
  order:[[initCol,initDir]],
  search:{search:initFilt},
  language:{search:"Filter:"}
});

// Relocate built-in controls to custom holders
$('#infoHolder').append($('#tbl_info'));
$('#paginateHolder').append($('#tbl_paginate').addClass('pagination'));
$('#lengthHolder').append($('#tbl_length'));
$('#filterHolder').append($('#tbl_filter'));

// • Persist UI state in URL
function keep(o){
  const u=new URL(location);for(const k in o)u.searchParams.set(k,o[k]);
  history.replaceState(null,'',u);
}
tbl.on('length.dt',(_,__,l)=>keep({rows:l}));
tbl.on('order.dt',()=>{
  const[o,d]=tbl.order()[0];keep({sortBy:names[o],dir:d});
});
tbl.on('search.dt',()=>keep({filter:tbl.search()}));

// • Auto refresh every minute
setTimeout(()=>location.reload(),60000);
</script>
</body></html>
"""

# Proxy-editor page ----------------------------------------------------------
TPL_EDIT = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Edit proxies</title>""" + BOOT_HEAD + """
</head><body>
<div class="container-lg" style="max-width:750px;padding-top:2rem">
<h3>Edit <code>proxies.txt</code></h3>
<p class="text-muted">One proxy per line.<br>
Accepted formats:
<code>socks5://username:password@host:port</code> or
<code>socks5://host:port</code>.</p>

<form method="post">
  <div class="mb-3">
    <textarea name="body" rows="15"
              class="form-control monospace">{{ current }}</textarea>
  </div>
  <button class="btn btn-primary btn-sm">Save &amp; return</button>
  <a href="{{ url_for('home') }}" class="btn btn-link btn-sm">Cancel</a>
</form>
</div></body></html>
"""

# ═══════════════════ Flask routes ═══════════════════════════
@app.route("/")
def home():
    """
    Dashboard view.
    Reads query-string for UI state, fetches DB rows, renders TPL_HOME.
    """
    rows  = max(5, min(int(request.args.get("rows", 10) or 10), 500))
    sortBy= request.args.get("sortBy", "Percentage")
    sortBy= sortBy if sortBy in COL else "Percentage"
    col   = COL[sortBy]
    dir_  = "asc" if request.args.get("dir", "desc").lower() == "asc" else "desc"
    filt  = request.args.get("filter", "")

    with Session() as s:
        data = s.query(Proxy).all()

    has_proxies = bool(read_proxies())

    return render_template_string(
        TPL_HOME,
        data=data,
        rows=rows,
        order_col=col,
        order_dir=dir_,
        filter_val=filt,
        has_proxies=has_proxies
    )

@app.route("/flush", methods=["POST"])
def flush():
    """Clear all stats (keeps proxy list)."""
    with Session() as s:
        s.query(Proxy).delete()
        s.commit()
    flash("Statistics cleared.")
    return redirect(url_for('home'))

@app.route("/edit", methods=["GET", "POST"])
def edit():
    """
    GET  → show textarea with current proxies  
    POST → save sanitized proxies, redirect to dashboard
    """
    if request.method == "POST":
        raw      = request.form.get("body", "").splitlines()
        cleaned  = filtered(raw)
        TXT_FILE.write_text("\n".join(cleaned) + "\n")
        flash(f"Saved {len(cleaned)} valid proxies.")
        return redirect(url_for('home'))

    current = "\n".join(read_proxies())
    return render_template_string(TPL_EDIT, current=current)

# ════════════════ APScheduler background job ════════════════
sched = BackgroundScheduler()
sched.add_job(
    probe,
    "interval",
    seconds=INTERVAL,
    next_run_time=datetime.now(timezone.utc) + timedelta(seconds=INTERVAL)
)
sched.start()

# ═══════════════════ main guard ═════════════════════════════
if __name__ == "__main__":
    try:
        probe()  # initial immediate round
        print("Serving →  http://127.0.0.1:3000")
        app.run(host="127.0.0.1", port=3000)
    finally:
        sched.shutdown()

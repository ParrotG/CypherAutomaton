"""Client for the IN-CYPHER platform via the versioned AGENT API (/api/agent/v1).
Auth = a per-team CTFd Access Token (Authorization: Token <token>) -- NOT a session cookie.
Uses urllib with NO cookie jar on purpose: sending only the token header (and never an
anonymous session cookie) keeps CTFd on token auth. The facade returns {ok,data,error},
structured connection info, and the team's PoW key. Drop-in for the old cookie client:
same method names used by main.py / solver.py."""
from __future__ import annotations
import hashlib, hmac, json, os, re, socket, urllib.request, urllib.error

V = "/api/agent/v1"
# Cloudflare's bot filter blocks default library UAs (python-urllib/requests) -> error 1010.
UA = os.environ.get("AGENT_UA", "Mozilla/5.0 (X11; Linux x86_64) autoctf-agent/2.0")


class CTFdClient:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token or ""
        # A plain opener with NO HTTPCookieProcessor: we never carry cookies, only the token.
        self._opener = urllib.request.build_opener()

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + V + path, data=data, method=method)
        req.add_header("Authorization", "Token " + self.token)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        try:
            r = self._opener.open(req, timeout=120)
            txt = r.read().decode()
            code = r.getcode()
        except urllib.error.HTTPError as e:
            txt = e.read().decode(); code = e.code
        except Exception as e:
            return -1, {"ok": False, "error": str(e)[:200], "data": None}
        try:
            return code, json.loads(txt)
        except Exception:
            return code, {"ok": False, "error": txt[:200], "data": None}

    def _get(self, path):
        return self._call("GET", path)

    # ---- challenges ----
    def list_challenges(self) -> list:
        _, j = self._get("/challenges")
        out = []
        for c in (j.get("data") or []):
            c = dict(c); c["value"] = c.get("points")   # solver.py reads .value
            out.append(c)
        return out

    def challenge(self, cid: int) -> dict:
        _, j = self._get("/challenges/%d" % cid)
        c = dict(j.get("data") or {})
        c["value"] = c.get("points")
        c.setdefault("connectionInfo", None)
        return c

    def download(self, url: str, dest: str):
        u = url if url.startswith("http") else self.base + url
        req = urllib.request.Request(u)
        req.add_header("Authorization", "Token " + self.token)
        req.add_header("User-Agent", UA)
        r = self._opener.open(req, timeout=120)
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)

    # ---- dynamic_iac instances ----
    @staticmethod
    def _conn_str(conn: dict):
        if not conn:
            return None
        k = conn.get("kind")
        if k == "web" and conn.get("url"):
            return conn["url"]
        if k == "pwn" and conn.get("host"):
            s = "nc %s %s" % (conn["host"], conn.get("port"))
            pw = conn.get("pow") or {}
            if pw.get("required") and pw.get("team_key"):
                s += "  (PoW-gated: team_key=%s)" % pw["team_key"]
            return s
        return conn.get("raw")

    def boot(self, cid: int) -> dict:
        code, j = self._call("POST", "/challenges/%d/instance" % cid)
        if not j.get("ok"):
            return {"error": j.get("error") or ("deploy failed (%s)" % code)}
        d = j.get("data") or {}
        return {"connectionInfo": self._conn_str(d), "expires_at": d.get("expires_at"), "raw": d}

    def instance_status(self, cid: int) -> dict:
        _, j = self._get("/challenges/%d/instance" % cid)
        d = j.get("data") or {}
        return {"connectionInfo": self._conn_str(d), "raw": d}

    def destroy(self, cid: int) -> bool:
        _, j = self._call("DELETE", "/challenges/%d/instance" % cid)
        return bool((j.get("data") or {}).get("destroyed"))

    def renew(self, cid: int) -> dict:
        _, j = self._call("POST", "/challenges/%d/instance/renew" % cid)
        return j.get("data") or {}

    # ---- flag submission ----
    def submit(self, cid: int, flag: str) -> dict:
        _, j = self._call("POST", "/challenges/%d/submit" % cid, {"flag": flag})
        return j.get("data") or {"status": j.get("error") or "error"}

    def me(self) -> dict:
        _, j = self._get("/me")
        return j.get("data") or {}


# --- PoW gate helper (pwn challenges) ---------------------------------------
# Raw-TCP challenges sit behind a per-team proof-of-work gate. connect_pwn solves it and
# returns a live socket to the real service, so you never have to implement the gate.

def _leading_zero_bits(b: bytes) -> int:
    n = 0
    for x in b:
        if x == 0:
            n += 8
            continue
        for i in range(7, -1, -1):
            if x & (1 << i):
                return n
            n += 1
    return n


def connect_pwn(host: str, port: int, team_key: str, timeout: int = 90):
    """Solve the team-key PoW gate; return a connected socket to the service.

    team_key is included in the connection info of PoW-gated challenges (also from me()).
    """
    s = socket.create_connection((host, int(port)), timeout=timeout)
    f = s.makefile("rwb", buffering=0)
    nonce = bits = None
    while True:
        line = f.readline()
        if not line:
            raise RuntimeError("PoW gate closed the connection")
        t = line.decode(errors="replace")
        m = re.search(r"nonce=(\w+)\s+bits=(\d+)", t)
        if m:
            nonce, bits = m.group(1), int(m.group(2))
        if "send X" in t:
            break
    if nonce is None:
        raise RuntimeError("PoW gate did not present a challenge")
    base = hmac.new(team_key.encode(), nonce.encode(), hashlib.sha256).digest()
    x = 0
    while _leading_zero_bits(hashlib.sha256(base + str(x).encode()).digest()) < bits:
        x += 1
    f.write((str(x) + "\n").encode())
    s.settimeout(None)
    return s

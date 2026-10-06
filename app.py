#!/usr/bin/env python3
"""Document Management System - upload, version control, sharing, search.

Run:  python3 app.py            ->  http://localhost:8000
Needs only the Python 3.8+ standard library (http.server + sqlite3).
Data lives in ./data (SQLite DB + stored files).
"""
import hashlib, json, mimetypes, os, re, secrets, sqlite3, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
FILES = os.path.join(DATA, "files")
MAX_BYTES = 50 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".py", ".js",
            ".css", ".log", ".yaml", ".yml", ".ini", ".sql", ".tsv", ".rtf"}
os.makedirs(FILES, exist_ok=True)

db = sqlite3.connect(os.path.join(DATA, "dms.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, title TEXT, tags TEXT DEFAULT '',
  created REAL, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, doc_id INTEGER, n INTEGER,
  filename TEXT, size INTEGER, mime TEXT, sha TEXT, note TEXT, created REAL, body TEXT,
  UNIQUE(doc_id, n));
CREATE TABLE IF NOT EXISTS shares(token TEXT PRIMARY KEY, doc_id INTEGER, version_n INTEGER,
  expires REAL, created REAL, revoked INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0);
""")


def path_for(doc_id, n):
    return os.path.join(FILES, f"{doc_id}_v{n}")


def extract_text(name, data):
    if os.path.splitext(name)[1].lower() in TEXT_EXT:
        return data[:300_000].decode("utf-8", "ignore")
    return ""


def add_version(doc_id, name, data, note):
    n = (db.execute("SELECT MAX(n) FROM versions WHERE doc_id=?", (doc_id,)).fetchone()[0] or 0) + 1
    with open(path_for(doc_id, n), "wb") as f:
        f.write(data)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    db.execute("INSERT INTO versions(doc_id,n,filename,size,mime,sha,note,created,body) VALUES(?,?,?,?,?,?,?,?,?)",
               (doc_id, n, name, len(data), mime, hashlib.sha256(data).hexdigest(), note, time.time(),
                extract_text(name, data)))
    db.commit()
    return n


def doc_summary(r):
    return {"id": r["id"], "title": r["title"], "tags": r["tags"], "created": r["created"],
            "versions": r["vcount"], "latest": r["latest"], "filename": r["filename"],
            "size": r["size"], "updated": r["updated"]}


BASE_Q = """SELECT d.*, COUNT(v.id) vcount, MAX(v.n) latest, MAX(v.created) updated,
  (SELECT filename FROM versions WHERE doc_id=d.id ORDER BY n DESC LIMIT 1) filename,
  (SELECT size FROM versions WHERE doc_id=d.id ORDER BY n DESC LIMIT 1) size
  FROM docs d JOIN versions v ON v.doc_id=d.id WHERE d.deleted=0 %s GROUP BY d.id ORDER BY updated DESC"""


def search(q):
    terms = [t for t in re.split(r"\s+", q.strip().lower()) if t]
    if not terms:
        return [doc_summary(r) for r in db.execute(BASE_Q % "")]
    out = []
    for r in db.execute(BASE_Q % ""):
        hay_meta = " ".join([r["title"], r["tags"], r["filename"] or ""]).lower()
        hits = db.execute("SELECT n, filename, lower(body) b, lower(note) nt FROM versions WHERE doc_id=?",
                          (r["id"],)).fetchall()
        score, snippet, ok = 0, "", True
        for t in terms:
            m = hay_meta.count(t) * 5
            c = 0
            for h in hits:
                if t in h["b"]:
                    c += h["b"].count(t)
                    if not snippet:
                        i = h["b"].find(t)
                        snippet = ("…" + h["b"][max(0, i - 40): i + 80].replace("\n", " ") + "…")
                if t in h["nt"] or t in h["filename"].lower():
                    m += 2
            if m + c == 0:
                ok = False
                break
            score += m + min(c, 10)
        if ok:
            d = doc_summary(r)
            d["score"], d["snippet"] = score, snippet
            out.append(d)
    return sorted(out, key=lambda d: -d["score"])


def doc_detail(doc_id):
    r = db.execute(BASE_Q % "AND d.id=%d" % doc_id).fetchone()
    if not r:
        return None
    d = doc_summary(r)
    d["history"] = [dict(id=v["n"], n=v["n"], filename=v["filename"], size=v["size"], note=v["note"],
                         created=v["created"], sha=v["sha"][:10], mime=v["mime"])
                    for v in db.execute("SELECT * FROM versions WHERE doc_id=? ORDER BY n DESC", (doc_id,))]
    now = time.time()
    d["shares"] = [dict(token=s["token"], version_n=s["version_n"], expires=s["expires"],
                        created=s["created"], downloads=s["downloads"],
                        active=not s["revoked"] and (not s["expires"] or s["expires"] > now))
                   for s in db.execute("SELECT * FROM shares WHERE doc_id=? ORDER BY created DESC", (doc_id,))]
    return d


class H(BaseHTTPRequestHandler):
    server_version = "DMS/1.0"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, doc_id, n, share=None):
        v = db.execute("SELECT * FROM versions WHERE doc_id=? AND n=?", (doc_id, n)).fetchone()
        if not v or not os.path.exists(path_for(doc_id, n)):
            return self.send(404, {"error": "Version not found"})
        with open(path_for(doc_id, n), "rb") as f:
            data = f.read()
        base, ext = os.path.splitext(v["filename"])
        dl = f'{base}_v{n}{ext}'
        self.send(200, data, v["mime"], {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(dl)})

    def share_row(self, token):
        s = db.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
        if not s or s["revoked"] or (s["expires"] and s["expires"] < time.time()):
            return None
        d = db.execute("SELECT * FROM docs WHERE id=? AND deleted=0", (s["doc_id"],)).fetchone()
        if not d:
            return None
        n = s["version_n"] or db.execute("SELECT MAX(n) FROM versions WHERE doc_id=?", (s["doc_id"],)).fetchone()[0]
        return s, d, n

    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        if p == "/":
            return self.send(200, PAGE, "text/html")
        if p == "/api/docs":
            return self.send(200, search(qs.get("q", [""])[0]))
        m = re.fullmatch(r"/api/docs/(\d+)", p)
        if m:
            d = doc_detail(int(m[1]))
            return self.send(200, d) if d else self.send(404, {"error": "Not found"})
        m = re.fullmatch(r"/files/(\d+)/(\d+)", p)
        if m:
            return self.serve_file(int(m[1]), int(m[2]))
        m = re.fullmatch(r"/s/([\w-]+)(/download)?", p)
        if m:
            r = self.share_row(m[1])
            if not r:
                return self.send(404, SHARE_GONE, "text/html")
            s, d, n = r
            if m[2]:
                db.execute("UPDATE shares SET downloads=downloads+1 WHERE token=?", (m[1],))
                db.commit()
                return self.serve_file(d["id"], n)
            v = db.execute("SELECT * FROM versions WHERE doc_id=? AND n=?", (d["id"], n)).fetchone()
            exp = time.strftime("%d %b %Y", time.localtime(s["expires"])) if s["expires"] else "never"
            esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;")
            return self.send(200, SHARE_PAGE.format(title=esc(d["title"]), fn=esc(v["filename"]), n=n,
                             size=v["size"] // 1024 + 1, exp=exp, tok=m[1]), "text/html")
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BYTES:
            return self.send(413, {"error": "File is larger than 50 MB."})
        body = self.rfile.read(length)
        if p == "/api/upload":
            name = os.path.basename(qs.get("name", ["file"])[0]) or "file"
            note = qs.get("note", [""])[0]
            doc_id = qs.get("doc_id", [""])[0]
            if not body:
                return self.send(400, {"error": "The file is empty."})
            if doc_id:
                doc_id = int(doc_id)
                last = db.execute("SELECT sha FROM versions WHERE doc_id=? ORDER BY n DESC LIMIT 1", (doc_id,)).fetchone()
                if last and last["sha"] == hashlib.sha256(body).hexdigest():
                    return self.send(409, {"error": "This file is identical to the current version, so no new version was saved."})
            else:
                title = qs.get("title", [""])[0].strip() or os.path.splitext(name)[0]
                tags = ", ".join(t.strip() for t in qs.get("tags", [""])[0].split(",") if t.strip())
                cur = db.execute("INSERT INTO docs(title,tags,created) VALUES(?,?,?)", (title, tags, time.time()))
                doc_id = cur.lastrowid
            n = add_version(doc_id, name, body, note or ("First upload" if n_first(doc_id) else ""))
            return self.send(200, {"id": doc_id, "n": n})
        m = re.fullmatch(r"/api/docs/(\d+)/restore/(\d+)", p)
        if m:
            doc_id, n = int(m[1]), int(m[2])
            v = db.execute("SELECT * FROM versions WHERE doc_id=? AND n=?", (doc_id, n)).fetchone()
            if not v:
                return self.send(404, {"error": "Version not found"})
            with open(path_for(doc_id, n), "rb") as f:
                new_n = add_version(doc_id, v["filename"], f.read(), f"Restored from v{n}")
            return self.send(200, {"n": new_n})
        m = re.fullmatch(r"/api/docs/(\d+)/share", p)
        if m:
            opts = json.loads(body or b"{}")
            days = int(opts.get("days") or 0)
            tok = secrets.token_urlsafe(12)
            db.execute("INSERT INTO shares(token,doc_id,version_n,expires,created) VALUES(?,?,?,?,?)",
                       (tok, int(m[1]), opts.get("version_n"), time.time() + days * 86400 if days else None, time.time()))
            db.commit()
            return self.send(200, {"token": tok})
        m = re.fullmatch(r"/api/docs/(\d+)/meta", p)
        if m:
            o = json.loads(body)
            db.execute("UPDATE docs SET title=?, tags=? WHERE id=?",
                       (o.get("title", "").strip() or "Untitled", o.get("tags", ""), int(m[1])))
            db.commit()
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/api/shares/([\w-]+)/revoke", p)
        if m:
            db.execute("UPDATE shares SET revoked=1 WHERE token=?", (m[1],))
            db.commit()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "Not found"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/docs/(\d+)", urlparse(self.path).path)
        if not m:
            return self.send(404, {"error": "Not found"})
        db.execute("UPDATE docs SET deleted=1 WHERE id=?", (int(m[1]),))
        db.execute("UPDATE shares SET revoked=1 WHERE doc_id=?", (int(m[1]),))
        db.commit()
        self.send(200, {"ok": True})


def n_first(doc_id):
    return not db.execute("SELECT 1 FROM versions WHERE doc_id=?", (doc_id,)).fetchone()


SHARE_CSS = ("body{font:16px system-ui,sans-serif;background:#eef1f4;color:#14202b;display:grid;place-items:center;"
             "min-height:100vh;margin:0}main{background:#fff;padding:32px;border-radius:10px;max-width:420px;"
             "box-shadow:0 1px 0 #cfd6dd}h1{margin:0 0 6px;font-size:22px}p{color:#51606e;margin:6px 0}"
             "a.b{display:inline-block;margin-top:16px;background:#0f6b6b;color:#fff;padding:10px 18px;"
             "border-radius:6px;text-decoration:none}")
SHARE_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
              "<title>{title}</title><style>" + SHARE_CSS + "</style><main><h1>{title}</h1>"
              "<p>{fn} · version {n} · about {size} KB</p><p>Link expires: {exp}</p>"
              "<a class=b href='/s/{tok}/download'>Download file</a></main>")
SHARE_GONE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
              "<style>" + SHARE_CSS + "</style><main><h1>This link no longer works</h1>"
              "<p>It was revoked, has expired, or the document was deleted. Ask the owner for a new link.</p></main>")

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Document Vault</title>
<style>
:root{--bg:#eef1f4;--panel:#fff;--ink:#14202b;--mute:#51606e;--line:#d5dce3;--accent:#0f6b6b;--accent-ink:#fff;--stamp:#b97a10;--danger:#a63a2b;--sel:#e3f0ef}
@media (prefers-color-scheme:dark){:root{--bg:#10171d;--panel:#18222b;--ink:#e6edf3;--mute:#9aa8b5;--line:#2b3946;--accent:#3fb3ad;--accent-ink:#062321;--stamp:#e0a23a;--danger:#e58a7b;--sel:#1d3a3a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;border:1px solid var(--line);background:var(--panel);padding:7px 12px;border-radius:6px}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button.danger{color:var(--danger)}
button:focus-visible,input:focus-visible,select:focus-visible,.row:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input,select{border:1px solid var(--line);background:var(--panel);padding:8px 10px;border-radius:6px;width:100%}
.app{display:grid;grid-template-columns:minmax(300px,380px) 1fr;min-height:100vh}
aside{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;max-height:100vh;position:sticky;top:0}
.top{padding:16px;border-bottom:1px solid var(--line);display:grid;gap:10px}
.top h1{font-size:20px;margin:0}
#list{overflow:auto;flex:1}
.row{padding:12px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--bg)} .row.sel{background:var(--sel)}
.row b{display:block;overflow-wrap:anywhere}
.meta{color:var(--mute);font-size:13px}
.snip{color:var(--mute);font-size:13px;font-style:italic;overflow-wrap:anywhere}
.vbadge{display:inline-block;border:1px solid var(--stamp);color:var(--stamp);border-radius:4px;padding:0 6px;font-size:12px;margin-left:6px;font-weight:600}
main{padding:28px;max-width:760px}
.empty{color:var(--mute);padding:40px 0}
.drop{border:2px dashed var(--line);border-radius:8px;padding:14px;text-align:center;color:var(--mute);cursor:pointer}
.drop.over{border-color:var(--accent);background:var(--sel)}
h2{margin:0 0 4px;font-size:24px;overflow-wrap:anywhere}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 28px}
h3{font-size:16px;margin:28px 0 12px}
.tl{list-style:none;margin:0;padding:0 0 0 22px;border-left:2px solid var(--line)}
.tl li{position:relative;padding:0 0 18px 14px}
.tl li::before{content:"";position:absolute;left:-30px;top:5px;width:12px;height:12px;border-radius:50%;background:var(--panel);border:2px solid var(--line)}
.tl li.cur::before{background:var(--stamp);border-color:var(--stamp)}
.tl .v{font-weight:700}
.tl .btns{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.tl .btns button{padding:3px 9px;font-size:13px}
.share{display:grid;gap:4px;padding:10px 0;border-bottom:1px solid var(--line)}
.share code{overflow-wrap:anywhere;font-size:13px}
.dead{opacity:.55}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink);padding:22px;width:min(440px,92vw)}
dialog::backdrop{background:rgba(0,0,0,.45)}
dialog h3{margin-top:0} label{display:grid;gap:4px;margin:12px 0;font-size:14px;color:var(--mute)}
.dlg-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:6px;opacity:0;pointer-events:none;transition:opacity .2s;max-width:90vw}
#toast.on{opacity:1}
@media (max-width:760px){.app{grid-template-columns:1fr}aside{position:static;max-height:none;border-right:0}#list{max-height:40vh}main{padding:18px}}
</style></head>
<body>
<div class="app">
<aside>
  <div class="top">
    <h1>Document Vault</h1>
    <input id="q" type="search" placeholder="Search titles, tags, notes, file text" aria-label="Search documents">
    <button class="primary" id="newBtn">Upload document</button>
  </div>
  <div id="list" role="list"></div>
</aside>
<main id="detail"><p class="empty">Select a document, or upload one to start its version history.</p></main>
</div>

<dialog id="upDlg"><form method="dialog" id="upForm">
  <h3 id="upTitle">Upload document</h3>
  <div class="drop" id="drop" tabindex="0">Drop a file here or click to choose<br><span id="fname" class="meta"></span></div>
  <input type="file" id="file" hidden>
  <div id="newFields">
    <label>Title<input id="title" placeholder="Defaults to the file name"></label>
    <label>Tags (comma separated)<input id="tags" placeholder="contract, 2026, legal"></label>
  </div>
  <label>Version note<input id="note" placeholder="What changed?"></label>
  <div class="dlg-actions"><button type="button" id="upCancel">Cancel</button><button class="primary" id="upGo" type="button">Upload</button></div>
</form></dialog>

<dialog id="shDlg"><form method="dialog">
  <h3>Create share link</h3>
  <label>Which version<select id="shVer"></select></label>
  <label>Link expires<select id="shDays"><option value="0">Never</option><option value="1">After 1 day</option><option value="7" selected>After 7 days</option><option value="30">After 30 days</option></select></label>
  <div class="dlg-actions"><button type="button" id="shCancel">Cancel</button><button class="primary" type="button" id="shGo">Create link</button></div>
</form></dialog>
<div id="toast" role="status"></div>

<script>
const $=s=>document.querySelector(s);
const esc=t=>String(t??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtSize=b=>b<1024?b+" B":b<1048576?(b/1024).toFixed(1)+" KB":(b/1048576).toFixed(1)+" MB";
const fmtDate=t=>new Date(t*1000).toLocaleString([], {dateStyle:"medium",timeStyle:"short"});
let cur=null, uploadFor=null, timer;
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");clearTimeout(timer);timer=setTimeout(()=>t.classList.remove("on"),3200)}
async function api(url,opt){const r=await fetch(url,opt);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||"Request failed");return j}

async function loadList(){
  const docs=await api("/api/docs?q="+encodeURIComponent($("#q").value));
  $("#list").innerHTML=docs.length?docs.map(d=>`<div class="row ${d.id===cur?"sel":""}" role="listitem" tabindex="0" data-id="${d.id}">
    <b>${esc(d.title)}<span class="vbadge">v${d.latest}</span></b>
    <span class="meta">${esc(d.filename)} · ${fmtSize(d.size)} · ${fmtDate(d.updated)}</span>
    ${d.tags?`<div class="meta">Tags: ${esc(d.tags)}</div>`:""}
    ${d.snippet?`<div class="snip">${esc(d.snippet)}</div>`:""}</div>`).join("")
    :`<p class="empty" style="padding:16px">${$("#q").value?"No documents match that search.":"No documents yet. Upload your first file."}</p>`;
}
$("#list").addEventListener("click",e=>{const r=e.target.closest(".row");if(r)open(+r.dataset.id)});
$("#list").addEventListener("keydown",e=>{if(e.key==="Enter"){const r=e.target.closest(".row");if(r)open(+r.dataset.id)}});
$("#q").addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(loadList,200)});

async function open(id){
  cur=id;const d=await api("/api/docs/"+id);
  $("#detail").innerHTML=`<h2>${esc(d.title)}</h2>
  <div class="meta">${d.versions} version${d.versions>1?"s":""} · Tags: ${esc(d.tags)||"none"}</div>
  <div class="actions">
    <button class="primary" id="bNew">Upload new version</button>
    <button id="bShare">Share</button><button id="bEdit">Edit title and tags</button>
    <button class="danger" id="bDel">Delete</button></div>
  <h3>Version history</h3>
  <ol class="tl">${d.history.map((v,i)=>`<li class="${i===0?"cur":""}">
    <span class="v">v${v.n}</span>${i===0?' <span class="vbadge">current</span>':""} <span class="meta">${fmtDate(v.created)}</span>
    <div>${esc(v.filename)} <span class="meta">· ${fmtSize(v.size)} · ${v.sha}</span></div>
    ${v.note?`<div class="meta">${esc(v.note)}</div>`:""}
    <div class="btns"><a href="/files/${d.id}/${v.n}"><button>Download</button></a>
    ${i>0?`<button data-restore="${v.n}">Restore as new version</button>`:""}</div></li>`).join("")}</ol>
  <h3>Share links</h3>
  ${d.shares.length?d.shares.map(s=>{const url=location.origin+"/s/"+s.token;return `<div class="share ${s.active?"":"dead"}">
    <code>${esc(url)}</code>
    <span class="meta">${s.version_n?"Version "+s.version_n:"Always latest"} · ${s.expires?(s.active?"expires ":"expired ")+fmtDate(s.expires):"never expires"} · ${s.downloads} download${s.downloads===1?"":"s"}${s.active?"":" · inactive"}</span>
    ${s.active?`<div class="btns"><button data-copy="${esc(url)}">Copy link</button> <button class="danger" data-revoke="${s.token}">Revoke</button></div>`:""}</div>`}).join(""):'<p class="meta">Not shared yet. Create a link to give someone download access.</p>'}`;
  $("#bNew").onclick=()=>openUpload(d);
  $("#bShare").onclick=()=>openShare(d);
  $("#bEdit").onclick=async()=>{const t=prompt("Title",d.title);if(t===null)return;const g=prompt("Tags (comma separated)",d.tags);if(g===null)return;
    await api(`/api/docs/${d.id}/meta`,{method:"POST",body:JSON.stringify({title:t,tags:g})});toast("Saved");open(d.id);loadList()};
  $("#bDel").onclick=async()=>{if(!confirm("Delete this document and revoke its share links?"))return;
    await api("/api/docs/"+d.id,{method:"DELETE"});cur=null;$("#detail").innerHTML='<p class="empty">Document deleted.</p>';loadList()};
  $("#detail").querySelectorAll("[data-restore]").forEach(b=>b.onclick=async()=>{
    const r=await api(`/api/docs/${d.id}/restore/${b.dataset.restore}`,{method:"POST"});toast(`Restored as v${r.n}`);open(d.id);loadList()});
  $("#detail").querySelectorAll("[data-revoke]").forEach(b=>b.onclick=async()=>{
    await api(`/api/shares/${b.dataset.revoke}/revoke`,{method:"POST"});toast("Link revoked");open(d.id)});
  $("#detail").querySelectorAll("[data-copy]").forEach(b=>b.onclick=()=>{navigator.clipboard.writeText(b.dataset.copy).then(()=>toast("Link copied"),()=>toast("Select and copy the link manually"))});
  loadList();
}

let picked=null;
function setFile(f){picked=f;$("#fname").textContent=f?f.name+" ("+fmtSize(f.size)+")":""}
function openUpload(doc){
  uploadFor=doc||null;setFile(null);$("#note").value="";$("#title").value="";$("#tags").value="";
  $("#newFields").style.display=doc?"none":"";
  $("#upTitle").textContent=doc?`New version of “${doc.title}”`:"Upload document";
  $("#upDlg").showModal();
}
$("#newBtn").onclick=()=>openUpload(null);
$("#drop").onclick=()=>$("#file").click();
$("#drop").onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();$("#file").click()}};
$("#file").onchange=e=>setFile(e.target.files[0]);
["dragover","dragenter"].forEach(n=>$("#drop").addEventListener(n,e=>{e.preventDefault();$("#drop").classList.add("over")}));
["dragleave","drop"].forEach(n=>$("#drop").addEventListener(n,e=>{e.preventDefault();$("#drop").classList.remove("over")}));
$("#drop").addEventListener("drop",e=>setFile(e.dataTransfer.files[0]));
$("#upCancel").onclick=()=>$("#upDlg").close();
$("#upGo").onclick=async()=>{
  if(!picked)return toast("Choose a file first");
  const p=new URLSearchParams({name:picked.name,note:$("#note").value});
  if(uploadFor)p.set("doc_id",uploadFor.id);else{p.set("title",$("#title").value);p.set("tags",$("#tags").value)}
  try{const r=await api("/api/upload?"+p,{method:"POST",body:picked});
    $("#upDlg").close();toast(`Saved as v${r.n}`);await loadList();open(r.id)}catch(e){toast(e.message)}
};

function openShare(d){
  $("#shVer").innerHTML='<option value="">Always the latest version</option>'+d.history.map(v=>`<option value="${v.n}">Only v${v.n} (${esc(v.filename)})</option>`).join("");
  $("#shDlg").showModal();
  $("#shGo").onclick=async()=>{
    const v=$("#shVer").value;
    await api(`/api/docs/${d.id}/share`,{method:"POST",body:JSON.stringify({days:+$("#shDays").value,version_n:v?+v:null})});
    $("#shDlg").close();toast("Share link created");open(d.id)};
}
$("#shCancel").onclick=()=>$("#shDlg").close();
loadList();
</script></body></html>
"""

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Document Vault running at http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()

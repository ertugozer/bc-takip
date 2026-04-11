#!/usr/bin/env python3
"""
Basecamp–Excel Karşılaştırma Raporu
- Her gün 18:00 Türkiye saatinde otomatik çalışır (APScheduler)
- Basecamp webhook geldiğinde de tetiklenir (/webhook)
- Manuel tetikleme için /run endpoint'i
- Sadece okur, hiçbir yerde değişiklik yapmaz

Durum tespiti: yorum okuma yok — sadece todo'nun bulunduğu listenin adına bakılır.
  "Marka Onayında" listesi → YEŞİLE BOYA
  Tamamlanmış (completed) → Excel'de kalmışsa SİL
  Diğerleri (Tasarım Ekibinde, SM&PM vs.) → aktif, renksiz
  Prodüksiyon işleri → sadece completedsa SİL, aktifse rapora dahil edilmez
"""

import os
import io
import json
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

import requests as req_lib

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── Ortam Değişkenleri ────────────────────────────────────────────────────
BASECAMP_CLIENT_ID     = os.environ["BASECAMP_CLIENT_ID"]
BASECAMP_CLIENT_SECRET = os.environ["BASECAMP_CLIENT_SECRET"]
BASECAMP_REFRESH_TOKEN = os.environ["BASECAMP_REFRESH_TOKEN"]
BASECAMP_ACCOUNT_IDS   = ["4181631", "6168221"]

EXCEL_URL       = os.environ["EXCEL_URL"]
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "ertugozerr@gmail.com")

# ─── Hedef Proje İsimleri (tam eşleşme, küçük harf) ───────────────────────
TARGET_PROJECTS = {
    "metro - dijital": "Metro",
    "hopi - sosyal medya": "Hopi",
}

# Prodüksiyon listesi — aktifse rapora dahil etme, completedsa SİL
PRODUKSIYON_LIST_KEYWORDS = ["prodüksiyon", "produksiyon", "production"]

# Webhook lock
_report_lock = threading.Lock()

app = Flask(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  BASECAMP API
# ══════════════════════════════════════════════════════════════════════════

def get_access_token() -> str:
    data = urllib.parse.urlencode({
        "type":          "refresh",
        "client_id":     BASECAMP_CLIENT_ID,
        "client_secret": BASECAMP_CLIENT_SECRET,
        "refresh_token": BASECAMP_REFRESH_TOKEN,
    }).encode()
    req = urllib.request.Request(
        "https://launchpad.37signals.com/authorization/token",
        data=data,
        method="POST",
        headers={"User-Agent": "IsOzetRaporu (ertugozerr@gmail.com)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def bc_get(token: str, account_id: str, path: str) -> list:
    """Basecamp API GET — sayfalı sonuçları tamamen getirir."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent":    "IsOzetRaporu (ertugozerr@gmail.com)",
    }
    url = f"https://3.basecampapi.com/{account_id}/{path}"
    results = []
    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            results.extend(data if isinstance(data, list) else [data])
            link_header = r.headers.get("Link", "")
            url = None
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
    return results


def check_todo_completed(token: str, account_id: str, bucket_id, todo_id) -> bool | None:
    """
    Belirtilen todo'nun tamamlanıp tamamlanmadığını doğrudan API'den kontrol eder.
    Döner: True = tamamlandı, False = aktif, None = bulunamadı / hata
    """
    try:
        items = bc_get(token, account_id, f"buckets/{bucket_id}/todos/{todo_id}.json")
        if items:
            todo = items[0]
            return bool(todo.get("completed", False))
        return None
    except Exception as e:
        err_str = str(e)
        if "404" in err_str:
            return None   # silinmiş / bulunamadı
        print(f"⚠️  check_todo_completed({account_id}/{bucket_id}/{todo_id}): {e}")
        return None


def get_todo_title(todo: dict) -> str:
    return (todo.get("title") or todo.get("content") or todo.get("summary") or "").strip()


def get_todolist_name(todo: dict) -> str:
    return (todo.get("parent") or {}).get("title", "").lower().strip()


def is_produksiyon(todo: dict) -> bool:
    name = get_todolist_name(todo)
    return any(k in name for k in PRODUKSIYON_LIST_KEYWORDS)


def is_marka_onayinda(todo: dict) -> bool:
    return "marka onay" in get_todolist_name(todo)


# ══════════════════════════════════════════════════════════════════════════
#  EXCEL OKUMA
# ══════════════════════════════════════════════════════════════════════════

def _parse_excel_bytes(content: bytes) -> list[dict]:
    """
    Yapı: Satır 7 = başlık, Satır 8+ = veriler
    Kolon C = marka, D = iş adı, E = Basecamp URL
    Sadece Hopi ve Metro satırlarını döndürür.
    URL formatı: https://3.basecamp.com/{account_id}/buckets/{bucket_id}/todos/{todo_id}
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active

    tasks, seen = [], set()
    for row in ws.iter_rows(min_row=8, values_only=True):
        task_name = row[3] if len(row) > 3 else None
        brand_raw = row[2] if len(row) > 2 else None
        bc_url    = str(row[4]) if len(row) > 4 and row[4] else ""

        if not task_name:
            continue
        brand = str(brand_raw).strip() if brand_raw else ""
        if brand.lower().strip() not in ("hopi", "metro"):
            continue

        todo_id = None
        bucket_id = None
        url_account_id = None

        if "/todos/" in bc_url:
            raw_id = bc_url.split("/todos/")[-1].split("#")[0].strip()
            if raw_id.isdigit():
                todo_id = int(raw_id)
            # https://3.basecamp.com/{account_id}/buckets/{bucket_id}/todos/{todo_id}
            try:
                parts = bc_url.split("/")
                # parts: ['https:', '', '3.basecamp.com', '{acct}', 'buckets', '{bid}', 'todos', '{tid}']
                if "buckets" in parts:
                    bi = parts.index("buckets")
                    bucket_id = int(parts[bi + 1]) if parts[bi + 1].isdigit() else None
                    url_account_id = parts[3] if parts[3].isdigit() else None
            except Exception:
                pass

        key = todo_id if todo_id else str(task_name).strip().lower()
        if key in seen:
            continue
        seen.add(key)

        tasks.append({
            "name": str(task_name).strip(),
            "brand": brand,
            "todo_id": todo_id,
            "bucket_id": bucket_id,
            "url_account_id": url_account_id,
        })
    return tasks


def read_excel_tasks() -> list[dict]:
    sep = "&" if "?" in EXCEL_URL else "?"
    download_url = EXCEL_URL + sep + "download=1"

    session = req_lib.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    session.get(EXCEL_URL, timeout=30, allow_redirects=True)
    r = session.get(download_url, timeout=30, allow_redirects=True)
    content = r.content

    if content[:2] != b"PK":
        preview = content[:200].decode("utf-8", errors="replace")
        raise ValueError(f"xlsx değil, HTML/text geldi: {preview[:120]}")

    return _parse_excel_bytes(content)


# ══════════════════════════════════════════════════════════════════════════
#  RAPOR OLUŞTURMA
# ══════════════════════════════════════════════════════════════════════════

def build_report(
    yesile_boya: list,
    aktif: list,
    sil_listesi: list,
    ekle_listesi: list,
    today: str,
    excel_error: str = "",
) -> str:
    def fmt(items):
        if not items:
            return ["  (Yok)"]
        return [f"  - {t['name']} — {t.get('brand','')}" for t in items]

    lines = [f"📋 EXCEL GÜNCELLEME TALİMATLARI — {today}", ""]

    if excel_error:
        lines += [f"⚠️  Excel okunamadı: {excel_error}", ""]

    lines.append("🗑️  SİL (Basecamp'te tamamlandı / artık listede yok):")
    lines += fmt(sil_listesi)
    lines.append("")

    lines.append("🟢 YEŞİLE BOYA (Marka Onayında listesinde):")
    lines += fmt(yesile_boya)
    lines.append("")

    lines.append("📋 AKTİF — renksiz bırak (Tasarım / SM&PM ekibinde):")
    lines += fmt(aktif)
    lines.append("")

    lines.append("➕ EXCEL'E EKLE (Basecamp'te var, Excel'de yok):")
    lines += fmt(ekle_listesi)

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#  EMAİL
# ══════════════════════════════════════════════════════════════════════════

def send_email(subject: str, body: str) -> None:
    """Resend HTTP API ile mail gönderir (Railway SMTP'yi bloklar, HTTP çalışır)."""
    if not RESEND_API_KEY:
        raise ValueError("RESEND_API_KEY env değişkeni ayarlanmamış")

    payload = json.dumps({
        "from":    "Excel Rapor <onboarding@resend.dev>",
        "to":      [RECIPIENT_EMAIL],
        "subject": subject,
        "text":    body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            print(f"✉️  Resend: {resp}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Resend {e.code}: {body}")


# ══════════════════════════════════════════════════════════════════════════
#  ANA RAPOR AKIŞI
# ══════════════════════════════════════════════════════════════════════════

def run_report(trigger: str = "cron") -> str:
    if not _report_lock.acquire(blocking=False):
        return "SKIPPED: rapor zaten çalışıyor"

    try:
        today = datetime.now().strftime("%d.%m.%Y %H:%M")
        print(f"\n▶  Rapor başlatıldı [{trigger}]: {today}\n{'─'*50}")

        token = get_access_token()
        print("✅ Token alındı")

        # Aktif todo'ları çek
        all_todos = []
        for acct_id in BASECAMP_ACCOUNT_IDS:
            try:
                raw = bc_get(token, acct_id, "my/assignments.json")
                fetched = []
                for item in raw:
                    if isinstance(item, dict) and "priorities" in item:
                        fetched.extend(item.get("priorities", []))
                        fetched.extend(item.get("non_priorities", []))
                    elif isinstance(item, dict) and item.get("title"):
                        fetched.append(item)
                print(f"📋 Hesap {acct_id}: {len(fetched)} görev")
                for t in fetched:
                    t["_account_id"] = acct_id
                all_todos.extend(fetched)
            except Exception as e:
                print(f"⚠️  Hesap {acct_id}: {e}")

        # Sadece hedef projeler, sadece aktif (completed=False)
        todos = []
        for t in all_todos:
            project_name = (t.get("bucket") or {}).get("name", "").lower().strip()
            if project_name in TARGET_PROJECTS and not t.get("completed", False):
                todos.append(t)

        print(f"🎯 Hedef projelerde aktif görev: {len(todos)}")

        # Durum kategorileri
        yesile_boya, aktif = [], []
        for todo in todos:
            # Prodüksiyon işleri → rapora dahil etme
            if is_produksiyon(todo):
                continue

            project_raw = (todo.get("bucket") or {}).get("name", "")
            brand = TARGET_PROJECTS.get(project_raw.lower().strip(), project_raw)
            name  = get_todo_title(todo)
            item  = {"name": name, "brand": brand, "id": todo.get("id")}

            if is_marka_onayinda(todo):
                yesile_boya.append(item)
                print(f"  🟢 [MARKA ONAYINDA] {name}")
            else:
                aktif.append(item)
                print(f"  📋 [AKTİF] {name} ({get_todolist_name(todo)})")

        # Excel oku
        excel_error = ""
        excel_tasks = []
        try:
            excel_tasks = read_excel_tasks()
            print(f"\n📊 Excel: {len(excel_tasks)} iş")
        except Exception as e:
            excel_error = str(e)
            print(f"⚠️  Excel: {e}")

        # Aktif Basecamp ID ve isimlerini topla
        active_bc_ids   = {t["id"] for t in todos if t.get("id")}
        active_bc_names = {get_todo_title(t).lower() for t in todos}

        # Excel'de var ama Basecamp'te aktif değil → SİL
        # Ama önce: my/assignments.json sadece Ertuğ'a atanmış işleri döndürür.
        # Dilara/Derin'e atanan aktif işler orada görünmez → yanlış SİL'e düşer.
        # Çözüm: eşleşmeyen Excel item'ları için doğrudan Basecamp API'sine soruyoruz.
        sil_listesi = []
        for t in excel_tasks:
            tid = t.get("todo_id")
            matched = (tid and tid in active_bc_ids) or \
                      (t["name"].lower().strip() in active_bc_names)
            if matched:
                continue  # Ertuğ'un listesinde var, aktif → SİL değil

            # Ertuğ'un listesinde yok — doğrudan API'ye sor
            bucket_id = t.get("bucket_id")
            url_acct  = t.get("url_account_id")
            if tid and bucket_id and url_acct:
                completed = check_todo_completed(token, url_acct, bucket_id, tid)
                if completed is True:
                    # Basecamp'te gerçekten tamamlanmış → SİL
                    sil_listesi.append(t)
                    print(f"  🗑️  [SİL - tamamlandı] {t['name']}")
                elif completed is False:
                    # Aktif ama başka birine atanmış → SİL değil
                    print(f"  ⏭️  [AKTİF - başka kişi] {t['name']}")
                else:
                    # 404 veya bulunamadı → silinmiş/arşivlenmiş → SİL
                    sil_listesi.append(t)
                    print(f"  🗑️  [SİL - bulunamadı] {t['name']}")
            else:
                # URL bilgisi yok → isim eşleşmesi yok → SİL
                sil_listesi.append(t)
                print(f"  🗑️  [SİL - URL yok] {t['name']}")

        # Basecamp'te var ama Excel'de yok → EKLE
        excel_ids   = {t["todo_id"] for t in excel_tasks if t.get("todo_id")}
        excel_names = {t["name"].lower().strip() for t in excel_tasks}
        ekle_listesi = []
        for todo in todos:
            if is_produksiyon(todo):
                continue
            tid  = todo.get("id")
            name = get_todo_title(todo)
            if not ((tid and tid in excel_ids) or (name.lower() in excel_names)):
                project_raw = (todo.get("bucket") or {}).get("name", "")
                brand = TARGET_PROJECTS.get(project_raw.lower().strip(), project_raw)
                ekle_listesi.append({"name": name, "brand": brand})

        report = build_report(yesile_boya, aktif, sil_listesi, ekle_listesi, today, excel_error)
        print(f"\n{'═'*50}\n{report}\n{'═'*50}")

        if RESEND_API_KEY:
            try:
                send_email(f"📋 Excel Güncelleme Talimatları — {today}", report)
                print("✉️  Mail gönderildi!")
            except Exception as e:
                print(f"⚠️  Mail: {e}")
                report += f"\n\n⚠️ Mail gönderilemedi: {e}"

        return report

    finally:
        _report_lock.release()


# ══════════════════════════════════════════════════════════════════════════
#  FLASK ENDPOINT'LERİ
# ══════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/run")
def manual_run():
    report = run_report(trigger="manual")
    return f"<pre>{report}</pre>", 200


@app.route("/webhook", methods=["POST"])
def basecamp_webhook():
    payload = request.get_json(silent=True) or {}
    kind    = payload.get("kind", "unknown")
    print(f"🔔 Webhook: {kind}")
    t = threading.Thread(target=run_report, kwargs={"trigger": f"webhook:{kind}"})
    t.daemon = True
    t.start()
    return jsonify({"status": "accepted"}), 202


@app.route("/debug")
def debug():
    try:
        token = get_access_token()
    except Exception as e:
        return f"<pre>Token hatası: {e}</pre>", 500

    lines = []
    for acct_id in BASECAMP_ACCOUNT_IDS:
        try:
            raw = bc_get(token, acct_id, "my/assignments.json")
            todos = []
            for item in raw:
                if isinstance(item, dict) and "priorities" in item:
                    todos.extend(item.get("priorities", []))
                    todos.extend(item.get("non_priorities", []))
            lines.append(f"\n=== Hesap {acct_id} ({len(todos)} görev) ===")
            for t in todos:
                bucket = (t.get("bucket") or {}).get("name", "YOK")
                lst    = get_todolist_name(t)
                match  = "✅" if bucket.lower().strip() in TARGET_PROJECTS else "❌"
                lines.append(f"{match} [{bucket}] [{lst}] {get_todo_title(t)}")
        except Exception as e:
            lines.append(f"Hata: {e}")

    return f"<pre>{chr(10).join(lines)}</pre>", 200


@app.route("/debug-excel")
def debug_excel():
    """Excel'den okunan item'ları ve URL ayrıştırmasını gösterir."""
    try:
        tasks = read_excel_tasks()
    except Exception as e:
        return f"<pre>Excel hatası: {e}</pre>", 500

    lines = [f"Excel'den {len(tasks)} iş okundu:\n"]
    for t in tasks:
        lines.append(
            f"[{t['brand']}] {t['name']}\n"
            f"  todo_id={t.get('todo_id')} bucket_id={t.get('bucket_id')} acct={t.get('url_account_id')}\n"
        )
    return f"<pre>{''.join(lines)}</pre>", 200


@app.route("/setup-webhooks")
def setup_webhooks():
    railway_url = f"https://{request.host}/webhook"
    results = []
    try:
        token = get_access_token()
    except Exception as e:
        return f"<pre>Token hatası: {e}</pre>", 500

    for acct_id in BASECAMP_ACCOUNT_IDS:
        try:
            projects = bc_get(token, acct_id, "projects.json")
        except Exception as e:
            results.append(f"❌ Hesap {acct_id}: {e}")
            continue

        for proj in projects:
            name = proj.get("name", "").lower().strip()
            if name not in TARGET_PROJECTS:
                continue
            proj_id   = proj["id"]
            proj_name = proj["name"]
            try:
                existing = bc_get(token, acct_id, f"buckets/{proj_id}/webhooks.json")
                if any(w.get("payload_url") == railway_url for w in existing):
                    results.append(f"✅ {proj_name} — zaten kayıtlı")
                    continue
            except Exception:
                pass

            payload = json.dumps({"payload_url": railway_url}).encode()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
                "User-Agent":    "IsOzetRaporu (ertugozerr@gmail.com)",
            }
            req = urllib.request.Request(
                f"https://3.basecampapi.com/{acct_id}/buckets/{proj_id}/webhooks.json",
                data=payload, headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read())
                    results.append(f"✅ {proj_name} — kaydedildi (ID: {resp.get('id')})")
            except Exception as e:
                results.append(f"❌ {proj_name}: {e}")

    return f"<pre>{chr(10).join(results)}\n\nWebhook URL: {railway_url}</pre>", 200


# ══════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════════════

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Europe/Istanbul")
    scheduler.add_job(
        func=run_report,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Europe/Istanbul"),
        kwargs={"trigger": "cron"},
        id="daily_report",
        replace_existing=True,
    )
    scheduler.start()
    print("📅 Scheduler: Haftaiçi 18:00 TK")


if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

#!/usr/bin/env python3
"""
Basecamp–Excel Karşılaştırma Raporu
- Her gün 18:00 Türkiye saatinde otomatik çalışır (APScheduler)
- Basecamp webhook geldiğinde de tetiklenir (/webhook)
- Manuel tetikleme için /run endpoint'i
- Sadece okur, hiçbir yerde değişiklik yapmaz
"""

import os
import io
import json
import threading
import urllib.request
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── Ortam Değişkenleri ────────────────────────────────────────────────────
BASECAMP_CLIENT_ID     = os.environ["BASECAMP_CLIENT_ID"]
BASECAMP_CLIENT_SECRET = os.environ["BASECAMP_CLIENT_SECRET"]
BASECAMP_REFRESH_TOKEN = os.environ["BASECAMP_REFRESH_TOKEN"]
BASECAMP_ACCOUNT_IDS   = ["4181631", "6168221"]

EXCEL_URL       = os.environ["EXCEL_URL"]
GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "ertugozerr@gmail.com")

# ─── Hedef Proje İsimleri (tam eşleşme) ───────────────────────────────────
TARGET_PROJECTS = {
    "metro - dijital": "Metro",
    "hopi - sosyal medya": "Hopi",
}

# ─── Kişi Listeleri ────────────────────────────────────────────────────────
TASARIM_KISILER = ["sümeyye", "sumeyye", "dilara", "özge", "ozge"]
KREATIF_KISILER = ["oya", "derin", "önder", "onder", "ömür", "omur"]

# ─── Webhook lock (aynı anda birden fazla çalışmasın) ─────────────────────
_report_lock = threading.Lock()

app = Flask(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  BASECAMP API
# ══════════════════════════════════════════════════════════════════════════

def get_access_token() -> str:
    """Refresh token ile yeni access token al."""
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


def get_last_comment(token: str, account_id: str, bucket_id: int, recording_id: int) -> dict | None:
    """Görevin son yorumunu döndür, yoksa None."""
    try:
        comments = bc_get(
            token, account_id,
            f"buckets/{bucket_id}/recordings/{recording_id}/comments.json"
        )
        return comments[-1] if comments else None
    except Exception:
        return None


def determine_status(todo: dict, last_comment: dict | None) -> tuple[str, str]:
    """(durum, son_yorum_kisi) döndür."""
    todolist_name = (todo.get("parent") or {}).get("title", "").lower()
    if "marka onay" in todolist_name:
        return "MARKA_ONAYINDA", ""

    if last_comment:
        creator_name  = (last_comment.get("creator") or {}).get("name", "")
        creator_lower = creator_name.lower()
        for name in TASARIM_KISILER:
            if name in creator_lower:
                return "TASARIMDA", creator_name
        for name in KREATIF_KISILER:
            if name in creator_lower:
                return "ONAYDA", creator_name

    return "BELIRSIZ", ""


# ══════════════════════════════════════════════════════════════════════════
#  EXCEL OKUMA
# ══════════════════════════════════════════════════════════════════════════

def read_excel_tasks() -> list[dict]:
    """SharePoint paylaşım linki üzerinden Excel dosyasını oku."""
    import openpyxl

    sep = "&" if "?" in EXCEL_URL else "?"
    download_url = EXCEL_URL + sep + "download=1"

    req = urllib.request.Request(
        download_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; IsOzetRaporu)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        content = r.read()

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active

    tasks = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0]:
            name  = str(row[0]).strip()
            brand = str(row[1]).strip() if len(row) > 1 and row[1] else ""
            tasks.append({"name": name, "brand": brand})
    return tasks


# ══════════════════════════════════════════════════════════════════════════
#  RAPOR OLUŞTURMA
# ══════════════════════════════════════════════════════════════════════════

def format_list(items: list, with_commenter: bool = False) -> list[str]:
    if not items:
        return ["  (Yok)"]
    lines = []
    for t in items:
        line = f"  - {t['name']} — {t.get('brand', '')}"
        if with_commenter and t.get("commenter"):
            line += f" (Son yorum: {t['commenter']})"
        lines.append(line)
    return lines


def build_report(
    categorized: dict,
    sil_listesi: list,
    ekle_listesi: list,
    today: str,
    excel_error: str = "",
) -> str:
    lines = [f"📋 EXCEL GÜNCELLEME TALİMATLARI — {today}", ""]

    if excel_error:
        lines += [f"⚠️  Excel okunamadı: {excel_error}", ""]

    lines.append("🗑️ SİL (Basecamp'te tamamlandı / artık atanmamış):")
    lines += format_list(sil_listesi)
    lines.append("")

    lines.append("🟢 YEŞİLE BOYA (Marka Onayında listesinde):")
    lines += format_list(categorized["MARKA_ONAYINDA"])
    lines.append("")

    lines.append("🎨 TASARIMDA olarak işaretle:")
    lines += format_list(categorized["TASARIMDA"], with_commenter=True)
    lines.append("")

    lines.append("✅ ONAYDA olarak işaretle:")
    lines += format_list(categorized["ONAYDA"], with_commenter=True)
    lines.append("")

    lines.append("➕ EXCEL'E EKLE (Basecamp'te aktif, Excel'de yok):")
    lines += format_list(ekle_listesi)
    lines.append("")

    lines.append("❓ BELİRSİZ (durumu sen kontrol et):")
    lines += format_list(categorized["BELIRSIZ"])

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#  EMAİL
# ══════════════════════════════════════════════════════════════════════════

def send_email(subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg["Subject"] = subject
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())


# ══════════════════════════════════════════════════════════════════════════
#  ANA RAPOR AKIŞI
# ══════════════════════════════════════════════════════════════════════════

def run_report(trigger: str = "cron") -> str:
    """
    Raporu çalıştır ve döndür.
    trigger: 'cron' | 'webhook' | 'manual'
    """
    if not _report_lock.acquire(blocking=False):
        print("⏳ Rapor zaten çalışıyor, bu istek atlandı.")
        return "SKIPPED: rapor zaten çalışıyor"

    try:
        today = datetime.now().strftime("%d.%m.%Y %H:%M")
        print(f"\n▶  Rapor başlatıldı [{trigger}]: {today}\n{'─'*50}")

        # 1. Basecamp access token
        token = get_access_token()
        print("✅ Basecamp token alındı")

        # 2. Her iki hesaptan aktif görevleri çek
        all_todos = []
        for acct_id in BASECAMP_ACCOUNT_IDS:
            try:
                fetched = bc_get(token, acct_id, "my/assignments.json")
                print(f"📋 Hesap {acct_id}: {len(fetched)} görev")
                for t in fetched:
                    t["_account_id"] = acct_id
                all_todos.extend(fetched)
            except Exception as e:
                print(f"⚠️  Hesap {acct_id} okunamadı: {e}")

        # 3. Sadece "Metro - Dijital" ve "Hopi - Sosyal Medya" projeleri
        todos = []
        for t in all_todos:
            project_name = (t.get("bucket") or {}).get("name", "").lower().strip()
            if project_name in TARGET_PROJECTS and not t.get("completed", False):
                todos.append(t)

        print(f"🎯 Hedef projelerdeki aktif görev sayısı: {len(todos)}")

        # 4. Her görev için durum belirle
        categorized: dict[str, list] = {
            "MARKA_ONAYINDA": [],
            "TASARIMDA":      [],
            "ONAYDA":         [],
            "BELIRSIZ":       [],
        }

        for todo in todos:
            bucket_id    = (todo.get("bucket") or {}).get("id")
            recording_id = todo.get("id")
            project_raw  = (todo.get("bucket") or {}).get("name", "")
            brand        = TARGET_PROJECTS.get(project_raw.lower().strip(), project_raw)
            acct_id      = todo.get("_account_id", BASECAMP_ACCOUNT_IDS[0])

            last_comment = get_last_comment(token, acct_id, bucket_id, recording_id)
            status, commenter = determine_status(todo, last_comment)

            categorized[status].append({
                "name":      todo.get("title", ""),
                "brand":     brand,
                "commenter": commenter,
            })
            print(f"   [{status}] {todo.get('title','')} ({brand})")

        # 5. Excel'i oku
        excel_error = ""
        excel_tasks = []
        try:
            excel_tasks = read_excel_tasks()
            print(f"\n📊 Excel'de {len(excel_tasks)} iş bulundu")
        except Exception as e:
            excel_error = str(e)
            print(f"\n⚠️  Excel okunamadı: {e}")

        active_bc_names = {t.get("title", "").lower().strip() for t in todos}

        sil_listesi = [
            t for t in excel_tasks
            if t["name"].lower().strip() not in active_bc_names
        ]

        excel_names = {t["name"].lower().strip() for t in excel_tasks}
        ekle_listesi = []
        for todo in todos:
            name = todo.get("title", "").strip()
            if name.lower() not in excel_names:
                project_raw = (todo.get("bucket") or {}).get("name", "")
                brand       = TARGET_PROJECTS.get(project_raw.lower().strip(), project_raw)
                ekle_listesi.append({"name": name, "brand": brand})

        # 6. Raporu oluştur
        report = build_report(categorized, sil_listesi, ekle_listesi, today, excel_error)
        print(f"\n{'═'*50}\n{report}\n{'═'*50}")

        # 7. Mail gönder
        if GMAIL_USER and GMAIL_PASSWORD:
            send_email(f"📋 Excel Güncelleme Talimatları — {today}", report)
            print("✉️  Mail gönderildi!")
        else:
            print("ℹ️  Mail bilgileri eksik, atlandı.")

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
    """Manuel tetikleme."""
    report = run_report(trigger="manual")
    return f"<pre>{report}</pre>", 200


@app.route("/webhook", methods=["POST"])
def basecamp_webhook():
    """
    Basecamp bu endpoint'e POST gönderir.
    İçerik doğrulanmaz — gelen her POST raporu tetikler.
    """
    payload = request.get_json(silent=True) or {}
    kind    = payload.get("kind", "unknown")
    print(f"🔔 Webhook alındı: {kind}")

    # Arka planda çalıştır — Basecamp 10sn timeout'u var
    t = threading.Thread(target=run_report, kwargs={"trigger": f"webhook:{kind}"})
    t.daemon = True
    t.start()

    return jsonify({"status": "accepted", "kind": kind}), 202


# ══════════════════════════════════════════════════════════════════════════
#  SCHEDULER (18:00 Türkiye = 15:00 UTC yaz / 16:00 UTC kış)
# ══════════════════════════════════════════════════════════════════════════

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Europe/Istanbul")
    # Haftaiçi (Mon-Fri) her gün 18:00 Türkiye saatinde
    scheduler.add_job(
        func=run_report,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=0,
            timezone="Europe/Istanbul",
        ),
        kwargs={"trigger": "cron"},
        id="daily_report",
        replace_existing=True,
    )
    scheduler.start()
    print("📅 Scheduler başlatıldı — Haftaiçi 18:00 (Türkiye)")


# ══════════════════════════════════════════════════════════════════════════
#  BAŞLANGIÇ
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Sunucu başlatıldı — port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

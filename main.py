#!/usr/bin/env python3
"""
Basecamp–Excel Karşılaştırma Raporu
Her gün 18:00 Türkiye saatinde Railway'de çalışır.
Sadece okur, hiçbir yerde değişiklik yapmaz.
"""

import os
import io
import json
import urllib.request
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

# ─── Ortam Değişkenleri ────────────────────────────────────────────────────
BASECAMP_CLIENT_ID     = os.environ["BASECAMP_CLIENT_ID"]
BASECAMP_CLIENT_SECRET = os.environ["BASECAMP_CLIENT_SECRET"]
BASECAMP_REFRESH_TOKEN = os.environ["BASECAMP_REFRESH_TOKEN"]
BASECAMP_ACCOUNT_ID    = "4181631"

EXCEL_URL       = os.environ["EXCEL_URL"]   # SharePoint paylaşım linki
GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "ertugozerr@gmail.com")

# ─── Kişi Listeleri ────────────────────────────────────────────────────────
# Tasarım/brief tarafı → iş tasarımdadır
TASARIM_KISILER = ["sümeyye", "sumeyye", "dilara", "özge", "ozge"]
# Kreatif/onay tarafı → iş onaydadır
KREATIF_KISILER = ["oya", "derin", "önder", "onder", "ömür", "omur"]


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


def bc_get(token: str, path: str) -> list:
    """Basecamp API GET — sayfalı sonuçları tamamen getirir."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent":    "IsOzetRaporu (ertugozerr@gmail.com)",
    }
    url = f"https://3.basecamp.com/{BASECAMP_ACCOUNT_ID}/api/v1/{path}"
    results = []

    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            results.extend(data if isinstance(data, list) else [data])
            # Sonraki sayfa linkini bul
            link_header = r.headers.get("Link", "")
            url = None
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
    return results


def get_last_comment(token: str, bucket_id: int, recording_id: int) -> dict | None:
    """Görevin son yorumunu döndür, yoksa None."""
    try:
        comments = bc_get(
            token,
            f"buckets/{bucket_id}/recordings/{recording_id}/comments.json"
        )
        return comments[-1] if comments else None
    except Exception:
        return None


def determine_status(todo: dict, last_comment: dict | None) -> tuple[str, str]:
    """(durum, son_yorum_kisi) döndür."""
    # Todo list adına bak
    todolist_name = (todo.get("parent") or {}).get("title", "").lower()
    if "marka onay" in todolist_name:
        return "MARKA_ONAYINDA", ""

    # Son yorumun sahibine bak
    if last_comment:
        creator_name = (last_comment.get("creator") or {}).get("name", "")
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

    # SharePoint'te &download=1 ile direkt indirme
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
#  ANA AKIŞ
# ══════════════════════════════════════════════════════════════════════════

def main():
    today = datetime.now().strftime("%d.%m.%Y")
    print(f"\n▶  Rapor başlatıldı: {today}\n{'─'*50}")

    # 1. Basecamp access token
    token = get_access_token()
    print("✅ Basecamp token alındı")

    # 2. Aktif görevleri çek
    all_todos = bc_get(token, "my/assignments.json")
    print(f"📋 Toplam atanan görev: {len(all_todos)}")

    # 3. Sadece Hopi ve Metro projeleri
    todos = [
        t for t in all_todos
        if any(
            k in (t.get("bucket") or {}).get("name", "").lower()
            for k in ["hopi", "metro"]
        )
        and not t.get("completed", False)   # tamamlanmamış olanlar
    ]
    print(f"🎯 Hopi + Metro aktif görev: {len(todos)}")

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
        brand        = "Hopi" if "hopi" in project_raw.lower() else "Metro"

        last_comment = get_last_comment(token, bucket_id, recording_id)
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

    # Aktif Basecamp iş adları (küçük harf)
    active_bc_names = {t.get("title", "").lower().strip() for t in todos}

    # SİL: Excel'de olup Basecamp aktif listesinde olmayan Hopi/Metro işleri
    sil_listesi = [
        t for t in excel_tasks
        if t["name"].lower().strip() not in active_bc_names
    ]

    # EKLE: Basecamp'te aktif olup Excel'de olmayan işler
    excel_names = {t["name"].lower().strip() for t in excel_tasks}
    ekle_listesi = []
    for todo in todos:
        name = todo.get("title", "").strip()
        if name.lower() not in excel_names:
            project_raw = (todo.get("bucket") or {}).get("name", "")
            brand       = "Hopi" if "hopi" in project_raw.lower() else "Metro"
            ekle_listesi.append({"name": name, "brand": brand})

    # 6. Raporu oluştur ve yazdır
    report = build_report(categorized, sil_listesi, ekle_listesi, today, excel_error)
    print(f"\n{'═'*50}\n{report}\n{'═'*50}")

    # 7. Mail gönder
    if GMAIL_USER and GMAIL_PASSWORD:
        send_email(f"📋 Excel Güncelleme Talimatları — {today}", report)
        print("✉️  Mail gönderildi!")
    else:
        print("ℹ️  Mail bilgileri eksik, atlandı.")

    return report


if __name__ == "__main__":
    main()

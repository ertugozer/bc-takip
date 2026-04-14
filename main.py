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
BREVO_API_KEY   = os.environ.get("BREVO_API_KEY", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "ertugozerr@gmail.com")

# ─── Hedef Proje İsimleri (tam eşleşme, küçük harf) ───────────────────────
TARGET_PROJECTS = {
    "metro - dijital": "Metro",
    "hopi - sosyal medya": "Hopi",
}

# Prodüksiyon listesi — aktifse rapora dahil etme, completedsa SİL
PRODUKSIYON_LIST_KEYWORDS = ["prodüksiyon", "produksiyon", "production"]

# SM & PM listesi — aktifse tamamen yoksay (ne yeşile boya ne renksiz yap)
SM_PM_LIST_KEYWORDS = ["sm & pm", "sm&pm", "sm ve pm"]

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


def get_todo_info(token: str, account_id: str, bucket_id, todo_id) -> dict | None:
    """
    Belirtilen todo hakkında bilgi getirir.
    Döner: {"completed": bool, "list_name": str, "produksiyon": bool}
           veya None (bulunamadı / hata)
    """
    try:
        items = bc_get(token, account_id, f"buckets/{bucket_id}/todos/{todo_id}.json")
        if items:
            todo = items[0]
            ln = get_todolist_name(todo)
            return {
                "completed":   bool(todo.get("completed", False)),
                "list_name":   ln,
                "produksiyon": any(k in ln for k in PRODUKSIYON_LIST_KEYWORDS),
            }
        return None
    except Exception as e:
        if "404" in str(e):
            return None
        print(f"⚠️  get_todo_info({account_id}/{bucket_id}/{todo_id}): {e}")
        return None


def get_todo_title(todo: dict) -> str:
    return (todo.get("title") or todo.get("content") or todo.get("summary") or "").strip()


def get_todolist_name(todo: dict) -> str:
    return (todo.get("parent") or {}).get("title", "").lower().strip()


def is_produksiyon(todo: dict) -> bool:
    name = get_todolist_name(todo)
    return any(k in name for k in PRODUKSIYON_LIST_KEYWORDS)


def is_sm_pm(todo: dict) -> bool:
    name = get_todolist_name(todo)
    return any(k in name for k in SM_PM_LIST_KEYWORDS)


def is_marka_onayinda(todo: dict) -> bool:
    return "marka onay" in get_todolist_name(todo)


# ══════════════════════════════════════════════════════════════════════════
#  EXCEL OKUMA
# ══════════════════════════════════════════════════════════════════════════

def _is_green_cell(cell) -> bool:
    """Hücre yeşil mi? Diğer tüm renkler (sarı, mor, kırmızı, mavi) renksiz sayılır."""
    try:
        fill = cell.fill
        if not fill or fill.fill_type != "solid":
            return False
        fg = fill.fgColor
        if fg.type != "rgb":
            return False
        rgb = fg.rgb
        if len(rgb) != 8 or rgb in ("00000000", "FF000000"):
            return False
        r = int(rgb[2:4], 16)
        g = int(rgb[4:6], 16)
        b = int(rgb[6:8], 16)
        return g > 150 and g > r * 1.2 and g > b
    except Exception:
        return False


def _parse_excel_bytes(content: bytes) -> list[dict]:
    """
    Yapı: Satır 7 = başlık, Satır 8+ = veriler
    Kolon C = marka, D = iş adı, E = Basecamp URL
    Sadece Hopi ve Metro satırlarını döndürür.
    URL formatı: https://3.basecamp.com/{account_id}/buckets/{bucket_id}/todos/{todo_id}
    already_green: D sütunu hücre rengi yeşilse True (Excel'de zaten yeşile boyanmış)
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active

    tasks, seen = [], set()
    for row in ws.iter_rows(min_row=8):          # values_only=False → renk okumak için
        if len(row) < 4:
            continue
        cell_brand = row[2]
        cell_name  = row[3]
        cell_url   = row[4] if len(row) > 4 else None

        task_name = cell_name.value
        brand_raw = cell_brand.value
        bc_url    = str(cell_url.value) if cell_url and cell_url.value else ""

        if not task_name:
            continue
        brand = str(brand_raw).strip() if brand_raw else ""
        if brand.lower().strip() not in ("hopi", "metro"):
            continue

        cell_color = "green" if (_is_green_cell(cell_name) or _is_green_cell(cell_brand)) else "none"

        todo_id = None
        bucket_id = None
        url_account_id = None

        if "/todos/" in bc_url:
            raw_id = bc_url.split("/todos/")[-1].split("#")[0].strip()
            if raw_id.isdigit():
                todo_id = int(raw_id)
            try:
                parts = bc_url.split("/")
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
            "name":           str(task_name).strip(),
            "brand":          brand,
            "todo_id":        todo_id,
            "bucket_id":      bucket_id,
            "url_account_id": url_account_id,
            "cell_color":     cell_color,
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

# ══════════════════════════════════════════════════════════════════════════
#  GEÇMİŞ TAKİBİ
# ══════════════════════════════════════════════════════════════════════════

STATE_FILE = "/tmp/bc_state.json"


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(sil: list, yesile: list, renksiz: list, ekle: list, timestamp: str):
    state = {
        "timestamp": timestamp,
        "sil":     [t["name"] for t in sil],
        "yesile":  [t["name"] for t in yesile],
        "renksiz": [t["name"] for t in renksiz],
        "ekle":    [t["name"] for t in ekle],
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️  State kayıt hatası: {e}")


def compute_changes(prev: dict, sil: list, yesile: list, renksiz: list, ekle: list) -> list[str]:
    """Önceki rapor ile karşılaştırarak değişiklikleri döndürür."""
    if not prev:
        return []
    changes = []

    curr_sil    = {t["name"] for t in sil}
    curr_yesile = {t["name"] for t in yesile}
    curr_renksiz = {t["name"] for t in renksiz}
    curr_ekle   = {t["name"] for t in ekle}

    prev_sil    = set(prev.get("sil", []))
    prev_yesile = set(prev.get("yesile", []))
    prev_renksiz = set(prev.get("renksiz", []))
    prev_ekle   = set(prev.get("ekle", []))

    # Yeni tamamlananlar (yeni SİL'e girenler)
    yeni_sil = curr_sil - prev_sil
    if yeni_sil:
        changes.append("Yeni tamamlandı: " + ", ".join(sorted(yeni_sil)))

    # Excel'den silindi (önceki SİL'de artık yok)
    silindi = prev_sil - curr_sil
    if silindi:
        changes.append("Excel'den silindi: " + ", ".join(sorted(silindi)))

    # Marka onayına yeni gelenler
    yeni_onay = curr_yesile - prev_yesile
    if yeni_onay:
        changes.append("Marka onayına geldi: " + ", ".join(sorted(yeni_onay)))

    # Onaydan çıkıp renksiz yapılması gerekenler
    onaydan_cikti = curr_renksiz - prev_renksiz
    if onaydan_cikti:
        changes.append("Onaydan çıktı (renksiz yap): " + ", ".join(sorted(onaydan_cikti)))

    # Yeni eklenen Basecamp işleri
    yeni_bc = curr_ekle - prev_ekle
    if yeni_bc:
        changes.append("Basecamp'te yeni iş: " + ", ".join(sorted(yeni_bc)))

    return changes


def build_report(
    yesile_boya: list,
    renksiz_yap: list,
    sil_listesi: list,
    ekle_listesi: list,
    today: str,
    excel_error: str = "",
    changes: list = None,
) -> str:
    def fmt(items):
        if not items:
            return ["  (Yok)"]
        lines = []
        for t in items:
            note = " 🟢 (yeşile boya)" if t.get("yesile_boya") else ""
            lines.append(f"  - {t['name']} — {t.get('brand','')}{note}")
        return lines

    lines = [f"📋 EXCEL GÜNCELLEME TALİMATLARI — {today}", ""]

    if excel_error:
        lines += [f"⚠️  Excel okunamadı: {excel_error}", ""]

    if changes:
        lines.append("🔄 SON RAPORDAN DEĞİŞİKLİKLER:")
        for c in changes:
            lines.append(f"  • {c}")
        lines.append("")

    lines.append("🗑️  SİL (Basecamp'te tamamlandı):")
    lines += fmt(sil_listesi)
    lines.append("")

    lines.append("🟢 YEŞİLE BOYA (Marka Onayında — Excel'de henüz yeşil değil):")
    lines += fmt(yesile_boya)
    lines.append("")

    lines.append("⬜ RENKSİZ YAP (Artık Marka Onayında değil — Excel'de hâlâ yeşil):")
    lines += fmt(renksiz_yap)
    lines.append("")

    lines.append("➕ EXCEL'E EKLE (Basecamp'te var, Excel'de yok):")
    lines += fmt(ekle_listesi)

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#  HTML MAİL
# ══════════════════════════════════════════════════════════════════════════

def build_html_report(
    yesile_boya: list,
    renksiz_yap: list,
    sil_listesi: list,
    ekle_listesi: list,
    today: str,
    excel_error: str = "",
    changes: list = None,
) -> str:
    def card(title: str, color: str, icon: str, items: list, note_key: str = "yesile_boya") -> str:
        border  = f"border-left: 5px solid {color};"
        header_bg = color
        if not items:
            rows = "<li style='color:#888;font-style:italic;'>Yok</li>"
        else:
            rows = ""
            for t in items:
                note = " &nbsp;<span style='background:#00b050;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;'>yeşile boya</span>" if t.get(note_key) else ""
                rows += f"<li style='padding:4px 0;border-bottom:1px solid #f0f0f0;'><b>{t['name']}</b> <span style='color:#888;font-size:12px;'>— {t.get('brand','')}</span>{note}</li>"
        return f"""
        <div style='margin:12px 0;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);{border}background:#fff;'>
          <div style='background:{header_bg};color:#fff;padding:10px 16px;font-weight:bold;font-size:14px;letter-spacing:.3px;'>
            {icon} &nbsp;{title}
          </div>
          <ul style='margin:0;padding:12px 16px 12px 32px;list-style:disc;'>
            {rows}
          </ul>
        </div>"""

    changes_block = ""
    if changes:
        items_html = "".join(f"<li>{c}</li>" for c in changes)
        changes_block = f"""
        <div style='margin:12px 0;border-radius:8px;background:#fff8e1;border:1px solid #ffe082;padding:12px 16px;'>
          <div style='font-weight:bold;color:#f57c00;margin-bottom:6px;'>🔄 Son Rapordan Değişiklikler</div>
          <ul style='margin:0;padding-left:20px;color:#555;'>{items_html}</ul>
        </div>"""

    error_block = ""
    if excel_error:
        error_block = f"<div style='background:#fff3f3;border:1px solid #ffcdd2;border-radius:8px;padding:10px 16px;margin:12px 0;color:#c62828;'>⚠️ Excel okunamadı: {excel_error}</div>"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style='font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;'>
  <div style='max-width:600px;margin:0 auto;'>

    <div style='background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;'>
      <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;opacity:.7;margin-bottom:4px;'>PunchBBDO — Excel Takip</div>
      <div style='font-size:22px;font-weight:bold;'>📋 Güncelleme Talimatları</div>
      <div style='font-size:13px;opacity:.8;margin-top:4px;'>{today}</div>
    </div>

    {error_block}
    {changes_block}
    {card("SİL — Basecamp'te tamamlandı", "#e53935", "🗑️", sil_listesi)}
    {card("YEŞİLE BOYA — Marka Onayında, Excel'de henüz yeşil değil", "#00b050", "🟢", yesile_boya)}
    {card("RENKSİZ YAP — Artık Marka Onayında değil, Excel'de hâlâ yeşil", "#757575", "⬜", renksiz_yap)}
    {card("EXCEL'E EKLE — Basecamp'te var, Excel'de yok", "#1976d2", "➕", ekle_listesi)}

    <div style='text-align:center;font-size:11px;color:#aaa;margin-top:20px;'>
      Otomatik rapor · bc-takip-production.up.railway.app
    </div>
  </div>
</body></html>"""
    return html


def send_email(subject: str, body_text: str, body_html: str) -> None:
    """Brevo (Sendinblue) HTTP API ile HTML mail gönderir."""
    if not BREVO_API_KEY:
        raise ValueError("BREVO_API_KEY env değişkeni ayarlanmamış")

    payload = json.dumps({
        "sender":      {"name": "Excel Rapor", "email": RECIPIENT_EMAIL},
        "to":          [{"email": RECIPIENT_EMAIL}],
        "subject":     subject,
        "textContent": body_text,
        "htmlContent": body_html,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        method="POST",
        headers={
            "api-key":      BREVO_API_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            print(f"✉️  Brevo: {resp}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Brevo {e.code}: {err_body}")


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
        # Excel oku
        excel_error = ""
        excel_tasks = []
        try:
            excel_tasks = read_excel_tasks()
            print(f"\n📊 Excel: {len(excel_tasks)} iş")
        except Exception as e:
            excel_error = str(e)
            print(f"⚠️  Excel: {e}")

        # Excel lookup tabloları
        excel_by_id   = {t["todo_id"]: t for t in excel_tasks if t.get("todo_id")}
        excel_by_name = {t["name"].lower(): t for t in excel_tasks}
        processed_keys = set()   # işlenmiş Excel item'ları

        yesile_boya  = []   # Marka Onayında ama Excel'de yeşil değil
        renksiz_yap  = []   # Excel'de yeşil ama artık Marka Onayında değil
        sil_listesi  = []   # Basecamp'te tamamlandı
        ekle_listesi = []   # Basecamp'te var, Excel'de yok

        # ── PASS 1: Ertuğ'a atanmış Basecamp todoları ──────────────────────
        for todo in todos:
            if is_produksiyon(todo) or is_sm_pm(todo):
                continue

            tid   = todo.get("id")
            name  = get_todo_title(todo)
            project_raw = (todo.get("bucket") or {}).get("name", "")
            brand = TARGET_PROJECTS.get(project_raw.lower().strip(), project_raw)
            bc_onay = is_marka_onayinda(todo)

            excel_item = excel_by_id.get(tid) or excel_by_name.get(name.lower())

            if excel_item:
                key = excel_item.get("todo_id") or excel_item["name"].lower()
                processed_keys.add(key)
                is_green = excel_item.get("cell_color") == "green"

                if bc_onay and not is_green:
                    yesile_boya.append({"name": name, "brand": brand, "id": tid})
                    print(f"  🟢 [YEŞİLE BOYA] {name}")
                elif not bc_onay and is_green:
                    renksiz_yap.append({"name": name, "brand": brand, "id": tid})
                    print(f"  ⬜ [RENKSİZ YAP] {name}")
                else:
                    print(f"  ✅ [DOĞRU RENK] {name}")
            else:
                # Excel'de yok → EKLE
                ekle_listesi.append({
                    "name": name, "brand": brand,
                    "yesile_boya": bc_onay,
                })
                print(f"  ➕ [EKLE] {name}")

        # ── PASS 2: Excel'de olup Ertuğ'a atanmamış todoları ───────────────
        for t in excel_tasks:
            key = t.get("todo_id") or t["name"].lower()
            if key in processed_keys:
                continue

            tid       = t.get("todo_id")
            bucket_id = t.get("bucket_id")
            url_acct  = t.get("url_account_id")
            is_green  = t.get("cell_color") == "green"

            if tid and bucket_id and url_acct:
                info = get_todo_info(token, url_acct, bucket_id, tid)
                if info is None:
                    sil_listesi.append(t)
                    print(f"  🗑️  [SİL - bulunamadı] {t['name']}")
                elif info["completed"]:
                    sil_listesi.append(t)
                    print(f"  🗑️  [SİL - tamamlandı] {t['name']}")
                elif info["produksiyon"]:
                    print(f"  ⏭️  [SKIP - prodüksiyon] {t['name']}")
                elif any(k in info["list_name"] for k in SM_PM_LIST_KEYWORDS):
                    print(f"  ⏭️  [SKIP - sm&pm] {t['name']}")
                elif "marka onay" in info["list_name"]:
                    if not is_green:
                        yesile_boya.append({**t, "id": tid})
                        print(f"  🟢 [YEŞİLE BOYA - başka kişi] {t['name']}")
                    else:
                        print(f"  ✅ [DOĞRU RENK - başka kişi] {t['name']}")
                else:
                    if is_green:
                        renksiz_yap.append({**t, "id": tid})
                        print(f"  ⬜ [RENKSİZ YAP - başka kişi] {t['name']}")
                    else:
                        print(f"  ✅ [DOĞRU RENK - başka kişi] {t['name']}")
            else:
                # URL yok → sadece SİL listesine ekle (teyit edilemiyor)
                sil_listesi.append(t)
                print(f"  🗑️  [SİL - URL yok] {t['name']}")

        # Geçmiş durum yükle → değişiklikleri hesapla
        prev_state = load_state()
        changes = compute_changes(prev_state, sil_listesi, yesile_boya, renksiz_yap, ekle_listesi)
        save_state(sil_listesi, yesile_boya, renksiz_yap, ekle_listesi, today)

        report      = build_report(yesile_boya, renksiz_yap, sil_listesi, ekle_listesi, today, excel_error, changes)
        report_html = build_html_report(yesile_boya, renksiz_yap, sil_listesi, ekle_listesi, today, excel_error, changes)
        print(f"\n{'═'*50}\n{report}\n{'═'*50}")

        if BREVO_API_KEY:
            try:
                send_email(f"📋 Excel Güncelleme Talimatları — {today}", report, report_html)
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


WEBHOOK_TRIGGER_KINDS = {
    "todo_completed",          # tamamlandı
    "todo_uncompleted",        # tamamlanmadı olarak geri alındı
    "todo_created",            # yeni iş oluşturuldu (liste değişimi de bunu tetikler)
    "todo_assignment_changed", # atanan kişi değişti
    "todo_trashed",            # silindi / arşivlendi
}

@app.route("/webhook", methods=["POST"])
def basecamp_webhook():
    payload = request.get_json(silent=True) or {}
    kind    = payload.get("kind", "unknown")

    if kind not in WEBHOOK_TRIGGER_KINDS:
        print(f"⏭️  Webhook atlandı [{kind}]")
        return jsonify({"status": "ignored", "kind": kind}), 200

    print(f"🔔 Webhook tetiklendi [{kind}]")
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

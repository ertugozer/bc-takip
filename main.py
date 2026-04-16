#!/usr/bin/env python3
"""
Basecamp–Excel Karşılaştırma Raporu v2
─────────────────────────────────────
YENİ:
  • Süre takibi    — her item kaç gündür ilgili kategoride (renk kodlu rozet)
  • Debounce       — webhook gelince 15 dk bekle, art arda değişiklikler tek rapora düşsün
  • Haftalık özet  — Cuma 18:05'te ayrı HTML mail
  • Dashboard      — /dashboard → tarayıcıdan anlık durum + 14 günlük geçmiş
  • URL Eksik      — Basecamp URL'si olmayan Excel satırları ayrı kategoride

AYNI KALANLAR:
  Pass 1 / Pass 2 mimarisi, yeşil renk tespiti, SM&PM/Prodüksiyon hariç tutma,
  Brevo mail, APScheduler, /run /debug /debug-excel /setup-webhooks
"""

import os
import io
import json
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

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

# ─── Hedef Proje İsimleri ──────────────────────────────────────────────────
TARGET_PROJECTS = {
    "metro - dijital": "Metro",
    "hopi - sosyal medya": "Hopi",
}

PRODUKSIYON_LIST_KEYWORDS = ["prodüksiyon", "produksiyon", "production"]
SM_PM_LIST_KEYWORDS       = ["sm & pm", "sm&pm", "sm ve pm"]

# ─── Debounce ─────────────────────────────────────────────────────────────
DEBOUNCE_SECONDS = 900          # 15 dakika
_debounce_timer  = None
_debounce_lock   = threading.Lock()

# ─── Rapor kilidi ──────────────────────────────────────────────────────────
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
        data=data, method="POST",
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
    return any(k in get_todolist_name(todo) for k in PRODUKSIYON_LIST_KEYWORDS)


def is_sm_pm(todo: dict) -> bool:
    return any(k in get_todolist_name(todo) for k in SM_PM_LIST_KEYWORDS)


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
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active

    tasks, seen = [], set()
    for row in ws.iter_rows(min_row=8):
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

        todo_id = bucket_id = url_account_id = None

        if "/todos/" in bc_url:
            raw_id = bc_url.split("/todos/")[-1].split("#")[0].strip()
            if raw_id.isdigit():
                todo_id = int(raw_id)
            try:
                parts = bc_url.split("/")
                if "buckets" in parts:
                    bi = parts.index("buckets")
                    bucket_id      = int(parts[bi + 1]) if parts[bi + 1].isdigit() else None
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
#  DURUM DOSYASI — GEÇMİŞ + SÜRE TAKİBİ
# ══════════════════════════════════════════════════════════════════════════

STATE_FILE        = "/tmp/bc_state.json"
HISTORY_KEEP_DAYS = 14


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(
    sil: list, yesile: list, renksiz: list, ekle: list,
    url_eksik: list, timestamp: str,
) -> dict:
    """
    State'i kaydeder.
    first_seen: yalnızca şu an aktif olan item'lar korunur (eski temizlenir).
    history:    her çalışmada bir snapshot eklenir, 14 günden eskisi atılır.
    Döner: kaydedilen first_seen dict'i (build fonksiyonlarına geçmek için).
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    existing  = load_state()
    old_fs    = existing.get("first_seen", {})
    history   = list(existing.get("history", []))

    # first_seen: aktif kategorilerdeki item'ları koru/ekle
    new_fs = {}
    for cat, items in [("sil", sil), ("yesile", yesile), ("renksiz", renksiz), ("ekle", ekle)]:
        for item in items:
            key = f"{cat}:{item['name']}"
            new_fs[key] = old_fs.get(key, today_str)   # eski tarihi koru

    # Tarihsel snapshot
    prev_yesile = set(existing.get("yesile", []))
    prev_sil    = set(existing.get("sil", []))
    curr_yesile = {t["name"] for t in yesile}
    curr_sil    = {t["name"] for t in sil}

    history.append({
        "date":          today_str,
        "time":          timestamp,
        "sil_count":     len(sil),
        "yesile_count":  len(yesile),
        "renksiz_count": len(renksiz),
        "ekle_count":    len(ekle),
        "completed":     list(curr_sil - prev_sil),
        "new_onay":      list(curr_yesile - prev_yesile),
    })

    cutoff  = (datetime.now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date", "") >= cutoff]

    state = {
        "timestamp":  timestamp,
        "sil":        [t["name"] for t in sil],
        "yesile":     [t["name"] for t in yesile],
        "renksiz":    [t["name"] for t in renksiz],
        "ekle":       [t["name"] for t in ekle],
        "url_eksik":  [t["name"] for t in url_eksik],
        "first_seen": new_fs,
        "history":    history,
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️  State kayıt hatası: {e}")

    return new_fs


def get_days_in_category(first_seen: dict, category: str, name: str) -> int | None:
    """Bir item'ın bir kategoride kaç gündür olduğunu döndürür."""
    date_str = first_seen.get(f"{category}:{name}")
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (datetime.now().date() - d).days
    except Exception:
        return None


def compute_changes(prev: dict, sil: list, yesile: list, renksiz: list, ekle: list) -> list[str]:
    """Önceki rapor ile karşılaştırarak değişiklikleri döndürür."""
    if not prev:
        return []
    changes = []

    curr_sil     = {t["name"] for t in sil}
    curr_yesile  = {t["name"] for t in yesile}
    curr_renksiz = {t["name"] for t in renksiz}
    curr_ekle    = {t["name"] for t in ekle}

    prev_sil     = set(prev.get("sil", []))
    prev_yesile  = set(prev.get("yesile", []))
    prev_renksiz = set(prev.get("renksiz", []))
    prev_ekle    = set(prev.get("ekle", []))

    yeni_sil = curr_sil - prev_sil
    if yeni_sil:
        changes.append("Yeni tamamlandı: " + ", ".join(sorted(yeni_sil)))

    silindi = prev_sil - curr_sil
    if silindi:
        changes.append("Excel'den silindi: " + ", ".join(sorted(silindi)))

    yeni_onay = curr_yesile - prev_yesile
    if yeni_onay:
        changes.append("Marka onayına geldi: " + ", ".join(sorted(yeni_onay)))

    onaydan_cikti = curr_renksiz - prev_renksiz
    if onaydan_cikti:
        changes.append("Onaydan çıktı (renksiz yap): " + ", ".join(sorted(onaydan_cikti)))

    yeni_bc = curr_ekle - prev_ekle
    if yeni_bc:
        changes.append("Basecamp'te yeni iş: " + ", ".join(sorted(yeni_bc)))

    return changes


# ══════════════════════════════════════════════════════════════════════════
#  RAPOR METNİ
# ══════════════════════════════════════════════════════════════════════════

def _days_label(days: int | None) -> str:
    if days is None or days == 0:
        return ""
    if days == 1:
        return " (1 gün)"
    return f" ({days} gündür)"


def build_report(
    yesile_boya: list,
    renksiz_yap: list,
    sil_listesi: list,
    ekle_listesi: list,
    url_eksik: list,
    today: str,
    excel_error: str = "",
    changes: list = None,
    first_seen: dict = None,
) -> str:
    fs = first_seen or {}

    def fmt(items, cat):
        if not items:
            return ["  (Yok)"]
        lines = []
        for t in items:
            dur  = _days_label(get_days_in_category(fs, cat, t["name"]))
            note = " 🟢 (yeşile boya)" if t.get("yesile_boya") else ""
            lines.append(f"  - {t['name']} — {t.get('brand','')}{dur}{note}")
        return lines

    lines = [f"📋 EXCEL GÜNCELLEME TALİMATLARI — {today}", ""]

    if excel_error:
        lines += [f"⚠️  Excel okunamadı: {excel_error}", ""]

    if changes:
        lines += ["🔄 SON RAPORDAN DEĞİŞİKLİKLER:"] + [f"  • {c}" for c in changes] + [""]

    lines.append("🗑️  SİL (Basecamp'te tamamlandı):")
    lines += fmt(sil_listesi, "sil")
    lines.append("")

    lines.append("🟢 YEŞİLE BOYA (Marka Onayında — Excel'de henüz yeşil değil):")
    lines += fmt(yesile_boya, "yesile")
    lines.append("")

    lines.append("⬜ RENKSİZ YAP (Artık Marka Onayında değil — Excel'de hâlâ yeşil):")
    lines += fmt(renksiz_yap, "renksiz")
    lines.append("")

    lines.append("➕ EXCEL'E EKLE (Basecamp'te var, Excel'de yok):")
    lines += fmt(ekle_listesi, "ekle")

    if url_eksik:
        lines.append("")
        lines.append("🔗 BASECAMP URL EKSİK (Excel'deki bu işlerin linki yok — kontrol et):")
        lines += fmt(url_eksik, "url_eksik")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#  HTML MAİL — GÜNLÜK
# ══════════════════════════════════════════════════════════════════════════

def _dur_badge_html(days: int | None) -> str:
    """Gün sayısına göre renkli rozet döndürür."""
    if days is None or days == 0:
        return ""
    if days <= 2:
        color = "#9e9e9e"
    elif days <= 6:
        color = "#f57c00"
    else:
        color = "#c62828"
    return (
        f"&nbsp;<span style='background:{color};color:#fff;"
        f"padding:1px 6px;border-radius:3px;font-size:11px;"
        f"font-weight:bold;'>{days}&nbsp;gün</span>"
    )


def _html_card(title: str, color: str, icon: str, items: list, category: str, first_seen: dict) -> str:
    fs = first_seen or {}
    if not items:
        rows = "<li style='color:#888;font-style:italic;'>Yok</li>"
    else:
        rows = ""
        for t in items:
            days  = get_days_in_category(fs, category, t["name"])
            badge = _dur_badge_html(days)
            note  = (
                " &nbsp;<span style='background:#00b050;color:#fff;"
                "padding:1px 6px;border-radius:3px;font-size:11px;'>yeşile boya</span>"
                if t.get("yesile_boya") else ""
            )
            rows += (
                f"<li style='padding:4px 0;border-bottom:1px solid #f0f0f0;'>"
                f"<b>{t['name']}</b> "
                f"<span style='color:#888;font-size:12px;'>— {t.get('brand','')}</span>"
                f"{badge}{note}</li>"
            )
    return f"""
    <div style='margin:12px 0;border-radius:8px;overflow:hidden;
                box-shadow:0 1px 4px rgba(0,0,0,.08);
                border-left:5px solid {color};background:#fff;'>
      <div style='background:{color};color:#fff;padding:10px 16px;
                  font-weight:bold;font-size:14px;letter-spacing:.3px;'>
        {icon}&nbsp;&nbsp;{title}
      </div>
      <ul style='margin:0;padding:12px 16px 12px 32px;list-style:disc;'>
        {rows}
      </ul>
    </div>"""


def build_html_report(
    yesile_boya: list,
    renksiz_yap: list,
    sil_listesi: list,
    ekle_listesi: list,
    url_eksik: list,
    today: str,
    excel_error: str = "",
    changes: list = None,
    first_seen: dict = None,
) -> str:
    fs = first_seen or {}

    changes_block = ""
    if changes:
        items_html = "".join(f"<li>{c}</li>" for c in changes)
        changes_block = f"""
        <div style='margin:12px 0;border-radius:8px;background:#fff8e1;
                    border:1px solid #ffe082;padding:12px 16px;'>
          <div style='font-weight:bold;color:#f57c00;margin-bottom:6px;'>
            🔄 Son Rapordan Değişiklikler
          </div>
          <ul style='margin:0;padding-left:20px;color:#555;'>{items_html}</ul>
        </div>"""

    error_block = ""
    if excel_error:
        error_block = (
            f"<div style='background:#fff3f3;border:1px solid #ffcdd2;"
            f"border-radius:8px;padding:10px 16px;margin:12px 0;color:#c62828;'>"
            f"⚠️ Excel okunamadı: {excel_error}</div>"
        )

    url_block = (
        _html_card("BASECAMP URL EKSİK — Excel'deki bu işlerin linki yok",
                   "#9c27b0", "🔗", url_eksik, "url_eksik", fs)
        if url_eksik else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style='font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
             background:#f5f5f5;margin:0;padding:20px;'>
  <div style='max-width:600px;margin:0 auto;'>

    <div style='background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;
                border-radius:10px;padding:20px 24px;margin-bottom:16px;'>
      <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;
                  opacity:.7;margin-bottom:4px;'>PunchBBDO — Excel Takip</div>
      <div style='font-size:22px;font-weight:bold;'>📋 Güncelleme Talimatları</div>
      <div style='font-size:13px;opacity:.8;margin-top:4px;'>{today}</div>
    </div>

    {error_block}
    {changes_block}
    {_html_card("SİL — Basecamp'te tamamlandı", "#e53935", "🗑️", sil_listesi, "sil", fs)}
    {_html_card("YEŞİLE BOYA — Marka Onayında, Excel'de henüz yeşil değil", "#00b050", "🟢", yesile_boya, "yesile", fs)}
    {_html_card("RENKSİZ YAP — Artık Marka Onayında değil, Excel'de hâlâ yeşil", "#757575", "⬜", renksiz_yap, "renksiz", fs)}
    {_html_card("EXCEL'E EKLE — Basecamp'te var, Excel'de yok", "#1976d2", "➕", ekle_listesi, "ekle", fs)}
    {url_block}

    <div style='text-align:center;font-size:11px;color:#aaa;margin-top:20px;'>
      Otomatik rapor · bc-takip-production.up.railway.app
    </div>
  </div>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════
#  HTML MAİL — HAFTALIK ÖZET
# ══════════════════════════════════════════════════════════════════════════

def build_weekly_html(state: dict) -> str:
    today    = datetime.now().strftime("%d.%m.%Y")
    history  = state.get("history", [])
    first_seen = state.get("first_seen", {})

    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    week   = [h for h in history if h.get("date", "") >= cutoff]

    total_completed = list({n for h in week for n in h.get("completed", [])})
    total_new_onay  = list({n for h in week for n in h.get("new_onay", [])})

    # Uzun süredir Marka Onayında bekleyenler (3+ gün)
    long_waiters = []
    for key, date_str in first_seen.items():
        if not key.startswith("yesile:"):
            continue
        name = key[len("yesile:"):]
        try:
            d    = datetime.strptime(date_str, "%Y-%m-%d").date()
            days = (datetime.now().date() - d).days
            if days >= 3:
                long_waiters.append((name, days))
        except Exception:
            pass
    long_waiters.sort(key=lambda x: -x[1])

    # ─── Günlük tablo ────────────────────────────────────────────────────
    day_rows = ""
    for h in sorted(week, key=lambda x: x.get("date", ""), reverse=True):
        compl = len(h.get("completed", []))
        day_rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{h.get('date','')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;'>{h.get('yesile_count',0)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;'>{h.get('sil_count',0)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;'>{h.get('ekle_count',0)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;font-weight:bold;color:#00b050;'>"
            f"{'+ ' + str(compl) if compl else '—'}</td>"
            f"</tr>"
        )
    if not day_rows:
        day_rows = "<tr><td colspan='5' style='padding:12px;text-align:center;color:#888;'>Henüz veri yok</td></tr>"

    # ─── Uzun bekleyenler tablosu ─────────────────────────────────────────
    waiter_rows = ""
    for name, days in long_waiters:
        color = "#c62828" if days >= 7 else "#f57c00"
        waiter_rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{name}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;"
            f"color:{color};font-weight:bold;'>{days} gün</td>"
            f"</tr>"
        )

    waiter_section = ""
    if long_waiters:
        waiter_section = f"""
        <div style='background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);
                    margin-bottom:16px;overflow:hidden;border-left:5px solid #f57c00;'>
          <div style='background:#f57c00;color:#fff;padding:10px 16px;font-weight:bold;'>
            ⏰ Uzun Süredir Marka Onayında ({len(long_waiters)} iş)
          </div>
          <table style='width:100%;border-collapse:collapse;'>
            <tr style='background:#fafafa;'>
              <th style='padding:6px 10px;text-align:left;font-size:12px;color:#888;'>İş Adı</th>
              <th style='padding:6px 10px;text-align:center;font-size:12px;color:#888;'>Süre</th>
            </tr>
            {waiter_rows}
          </table>
        </div>"""

    completed_html = (
        "".join(f"<li>{n}</li>" for n in sorted(total_completed))
        or "<li style='color:#888;font-style:italic;'>Yok</li>"
    )
    new_onay_html = (
        "".join(f"<li>{n}</li>" for n in sorted(total_new_onay))
        or "<li style='color:#888;font-style:italic;'>Yok</li>"
    )

    lw_color = "#c62828" if long_waiters else "#4caf50"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style='font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
             background:#f5f5f5;margin:0;padding:20px;'>
  <div style='max-width:620px;margin:0 auto;'>

    <div style='background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;
                border-radius:10px;padding:20px 24px;margin-bottom:16px;'>
      <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;
                  opacity:.7;margin-bottom:4px;'>PunchBBDO — Excel Takip</div>
      <div style='font-size:22px;font-weight:bold;'>📅 Haftalık Özet</div>
      <div style='font-size:13px;opacity:.8;margin-top:4px;'>{today}</div>
    </div>

    <div style='display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;'>
      <div style='flex:1;min-width:120px;background:#fff;border-radius:8px;padding:14px;
                  box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center;'>
        <div style='font-size:32px;font-weight:bold;color:#00b050;'>{len(total_completed)}</div>
        <div style='font-size:12px;color:#666;margin-top:4px;'>Bu Hafta Tamamlanan</div>
      </div>
      <div style='flex:1;min-width:120px;background:#fff;border-radius:8px;padding:14px;
                  box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center;'>
        <div style='font-size:32px;font-weight:bold;color:#1976d2;'>{len(total_new_onay)}</div>
        <div style='font-size:12px;color:#666;margin-top:4px;'>Marka Onayına Geldi</div>
      </div>
      <div style='flex:1;min-width:120px;background:#fff;border-radius:8px;padding:14px;
                  box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center;'>
        <div style='font-size:32px;font-weight:bold;color:{lw_color};'>{len(long_waiters)}</div>
        <div style='font-size:12px;color:#666;margin-top:4px;'>3+ Gündür Bekleyen</div>
      </div>
    </div>

    {waiter_section}

    <div style='background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);
                margin-bottom:16px;overflow:hidden;'>
      <div style='background:#1a1a2e;color:#fff;padding:10px 16px;font-weight:bold;'>
        📊 Günlük İstatistikler
      </div>
      <table style='width:100%;border-collapse:collapse;'>
        <tr style='background:#fafafa;'>
          <th style='padding:6px 10px;text-align:left;font-size:12px;color:#888;'>Tarih</th>
          <th style='padding:6px 10px;text-align:center;font-size:12px;color:#888;'>Onayda</th>
          <th style='padding:6px 10px;text-align:center;font-size:12px;color:#888;'>Silinecek</th>
          <th style='padding:6px 10px;text-align:center;font-size:12px;color:#888;'>Eklenecek</th>
          <th style='padding:6px 10px;text-align:center;font-size:12px;color:#888;'>Tamamlandı</th>
        </tr>
        {day_rows}
      </table>
    </div>

    <div style='display:flex;gap:10px;flex-wrap:wrap;'>
      <div style='flex:1;min-width:200px;background:#fff;border-radius:8px;
                  box-shadow:0 1px 4px rgba(0,0,0,.08);padding:14px;
                  border-left:5px solid #00b050;'>
        <div style='font-weight:bold;color:#00b050;margin-bottom:8px;'>✅ Bu Hafta Tamamlanan</div>
        <ul style='margin:0;padding-left:18px;font-size:13px;'>{completed_html}</ul>
      </div>
      <div style='flex:1;min-width:200px;background:#fff;border-radius:8px;
                  box-shadow:0 1px 4px rgba(0,0,0,.08);padding:14px;
                  border-left:5px solid #1976d2;'>
        <div style='font-weight:bold;color:#1976d2;margin-bottom:8px;'>🔵 Bu Hafta Marka Onayına Gelen</div>
        <ul style='margin:0;padding-left:18px;font-size:13px;'>{new_onay_html}</ul>
      </div>
    </div>

    <div style='text-align:center;font-size:11px;color:#aaa;margin-top:20px;'>
      Haftalık otomatik rapor · bc-takip-production.up.railway.app
    </div>
  </div>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════
#  MAİL GÖNDERİMİ
# ══════════════════════════════════════════════════════════════════════════

def send_email(subject: str, body_text: str, body_html: str) -> None:
    """Brevo HTTP API ile HTML mail gönderir."""
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
        data=payload, method="POST",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
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

        # Sadece hedef projeler, sadece aktif
        todos = [
            t for t in all_todos
            if (t.get("bucket") or {}).get("name", "").lower().strip() in TARGET_PROJECTS
            and not t.get("completed", False)
        ]
        print(f"🎯 Hedef projelerde aktif görev: {len(todos)}")

        # Excel oku
        excel_error = ""
        excel_tasks = []
        try:
            excel_tasks = read_excel_tasks()
            print(f"\n📊 Excel: {len(excel_tasks)} iş")
        except Exception as e:
            excel_error = str(e)
            print(f"⚠️  Excel: {e}")

        excel_by_id    = {t["todo_id"]: t for t in excel_tasks if t.get("todo_id")}
        excel_by_name  = {t["name"].lower(): t for t in excel_tasks}
        processed_keys = set()

        yesile_boya  = []
        renksiz_yap  = []
        sil_listesi  = []
        ekle_listesi = []
        url_eksik    = []

        # ── PASS 1: Ertuğ'a atanmış Basecamp todoları ──────────────────────
        for todo in todos:
            if is_produksiyon(todo) or is_sm_pm(todo):
                continue

            tid         = todo.get("id")
            name        = get_todo_title(todo)
            project_raw = (todo.get("bucket") or {}).get("name", "")
            brand       = TARGET_PROJECTS.get(project_raw.lower().strip(), project_raw)
            bc_onay     = is_marka_onayinda(todo)
            excel_item  = excel_by_id.get(tid) or excel_by_name.get(name.lower())

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
                ekle_listesi.append({"name": name, "brand": brand, "yesile_boya": bc_onay})
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
                # Basecamp URL'si yok — kullanıcı kontrol etmeli
                url_eksik.append(t)
                print(f"  🔗 [URL EKSİK] {t['name']}")

        # Geçmiş yükle → değişiklikler → state kaydet
        prev_state = load_state()
        changes    = compute_changes(prev_state, sil_listesi, yesile_boya, renksiz_yap, ekle_listesi)
        first_seen = save_state(sil_listesi, yesile_boya, renksiz_yap, ekle_listesi, url_eksik, today)

        report      = build_report(yesile_boya, renksiz_yap, sil_listesi, ekle_listesi, url_eksik, today, excel_error, changes, first_seen)
        report_html = build_html_report(yesile_boya, renksiz_yap, sil_listesi, ekle_listesi, url_eksik, today, excel_error, changes, first_seen)
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


def run_weekly_summary():
    """Cuma 18:05'te haftalık özet maili gönderir."""
    print("\n📅 Haftalık özet başlatıldı")
    state = load_state()
    if not state:
        print("⚠️  Haftalık özet: state boş, özet atlandı")
        return
    today = datetime.now().strftime("%d.%m.%Y")
    html  = build_weekly_html(state)
    text  = f"Haftalık özet: {today}"
    if BREVO_API_KEY:
        try:
            send_email(f"📅 Haftalık Özet — {today}", text, html)
            print("✉️  Haftalık özet maili gönderildi")
        except Exception as e:
            print(f"⚠️  Haftalık özet mail hatası: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  DEBOUNCE — Webhook'ları toplu işle
# ══════════════════════════════════════════════════════════════════════════

def schedule_debounced_report(kind: str):
    """
    Webhook gelince raporu hemen değil, DEBOUNCE_SECONDS sonra çalıştırır.
    Bu süre içinde yeni webhook gelirse sayaç sıfırlanır.
    Böylece art arda 5 değişiklik olsa bile sadece 1 mail gönderilir.
    """
    global _debounce_timer
    with _debounce_lock:
        if _debounce_timer is not None and _debounce_timer.is_alive():
            _debounce_timer.cancel()
            print(f"⏱️  Debounce sıfırlandı [{kind}]")

        _debounce_timer = threading.Timer(
            DEBOUNCE_SECONDS,
            run_report,
            kwargs={"trigger": f"webhook:{kind}"},
        )
        _debounce_timer.daemon = True
        _debounce_timer.start()
        print(f"⏱️  Debounce: {DEBOUNCE_SECONDS // 60} dk sonra rapor çalışacak [{kind}]")


# ══════════════════════════════════════════════════════════════════════════
#  FLASK ENDPOINT'LERİ
# ══════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    state = load_state()
    return jsonify({
        "status": "ok",
        "time":   datetime.now().isoformat(),
        "last_report": state.get("timestamp", "—"),
    })


@app.route("/run")
def manual_run():
    report = run_report(trigger="manual")
    return f"<pre>{report}</pre>", 200


WEBHOOK_TRIGGER_KINDS = {
    "todo_completed",           # tamamlandı
    "todo_uncompleted",         # tamamlanmadı olarak geri alındı
    "todo_created",             # yeni iş oluşturuldu
    "todo_assignment_changed",  # atanan kişi değişti
    "todo_trashed",             # silindi / arşivlendi
    "todo_moved",               # liste değişti (Tasarımda → Marka Onayında gibi)
}


@app.route("/webhook", methods=["POST"])
def basecamp_webhook():
    payload = request.get_json(silent=True) or {}
    kind    = payload.get("kind", "unknown")

    if kind not in WEBHOOK_TRIGGER_KINDS:
        print(f"⏭️  Webhook atlandı [{kind}]")
        return jsonify({"status": "ignored", "kind": kind}), 200

    schedule_debounced_report(kind)
    return jsonify({
        "status":         "scheduled",
        "kind":           kind,
        "delay_minutes":  DEBOUNCE_SECONDS // 60,
    }), 202


@app.route("/dashboard")
def dashboard():
    state = load_state()
    if not state:
        return (
            "<html><body style='font-family:sans-serif;padding:40px;'>"
            "<h2>Henüz rapor çalışmadı.</h2>"
            "<a href='/run'>▶ Şimdi çalıştır</a></body></html>"
        ), 200

    ts         = state.get("timestamp", "—")
    sil        = state.get("sil", [])
    yesile     = state.get("yesile", [])
    renksiz    = state.get("renksiz", [])
    ekle       = state.get("ekle", [])
    url_eksik  = state.get("url_eksik", [])
    history    = state.get("history", [])
    first_seen = state.get("first_seen", {})

    # Uzun süredir Marka Onayında bekleyenler (3+ gün)
    long_waiters = []
    for key, date_str in first_seen.items():
        if not key.startswith("yesile:"):
            continue
        name = key[len("yesile:"):]
        try:
            d    = datetime.strptime(date_str, "%Y-%m-%d").date()
            days = (datetime.now().date() - d).days
            if days >= 3:
                long_waiters.append((name, days))
        except Exception:
            pass
    long_waiters.sort(key=lambda x: -x[1])

    def stat_card(label, count, color):
        return (
            f"<div style='background:#fff;border-radius:10px;padding:18px;"
            f"text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.1);"
            f"border-top:4px solid {color};'>"
            f"<div style='font-size:36px;font-weight:bold;color:{color};'>{count}</div>"
            f"<div style='font-size:12px;color:#666;margin-top:4px;'>{label}</div>"
            f"</div>"
        )

    stat_cards = f"""
    <div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));
                gap:12px;margin-bottom:20px;'>
      {stat_card("SİL", len(sil), "#e53935")}
      {stat_card("YEŞİLE BOYA", len(yesile), "#00b050")}
      {stat_card("RENKSİZ YAP", len(renksiz), "#757575")}
      {stat_card("EKLE", len(ekle), "#1976d2")}
      {stat_card("URL EKSİK", len(url_eksik), "#9c27b0")}
    </div>"""

    waiter_rows = ""
    for name, days in long_waiters:
        c = "#c62828" if days >= 7 else "#f57c00"
        waiter_rows += (
            f"<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;'>{name}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;"
            f"text-align:center;color:{c};font-weight:bold;'>{days} gün</td>"
            f"</tr>"
        )

    waiter_section = ""
    if long_waiters:
        waiter_section = f"""
        <div style='background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.1);
                    margin-bottom:20px;overflow:hidden;'>
          <div style='background:#f57c00;color:#fff;padding:12px 16px;font-weight:bold;'>
            ⏰ Uzun Süredir Marka Onayında — {len(long_waiters)} iş
          </div>
          <table style='width:100%;border-collapse:collapse;'>
            <tr style='background:#fafafa;'>
              <th style='padding:8px 12px;text-align:left;font-size:12px;color:#888;'>İş Adı</th>
              <th style='padding:8px 12px;text-align:center;font-size:12px;color:#888;'>Bekleme</th>
            </tr>
            {waiter_rows}
          </table>
        </div>"""

    hist_rows = ""
    for h in sorted(history, key=lambda x: x.get("date", ""), reverse=True)[:14]:
        compl = len(h.get("completed", []))
        onay  = len(h.get("new_onay", []))
        hist_rows += (
            f"<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;font-size:13px;'>{h.get('date','')}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center;'>"
            f"<span style='background:#00b050;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;'>{h.get('yesile_count',0)}</span></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center;'>"
            f"<span style='background:#e53935;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;'>{h.get('sil_count',0)}</span></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center;'>"
            f"<span style='background:#1976d2;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;'>{h.get('ekle_count',0)}</span></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center;"
            f"font-weight:bold;color:#00b050;'>{'+ ' + str(compl) if compl else '—'}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:center;"
            f"font-weight:bold;color:#1976d2;'>{'+ ' + str(onay) if onay else '—'}</td>"
            f"</tr>"
        )
    if not hist_rows:
        hist_rows = "<tr><td colspan='6' style='padding:16px;text-align:center;color:#888;'>Henüz veri yok</td></tr>"

    return f"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>BC Takip · Dashboard</title>
  <style>
    body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
           background:#f0f2f5;margin:0;padding:20px; }}
    .wrap {{ max-width:780px;margin:0 auto; }}
    a {{ color:#1976d2;text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <div class="wrap">

    <div style='background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;
                border-radius:12px;padding:22px 26px;margin-bottom:20px;
                display:flex;justify-content:space-between;align-items:center;
                flex-wrap:wrap;gap:10px;'>
      <div>
        <div style='font-size:11px;text-transform:uppercase;letter-spacing:1px;opacity:.7;'>
          PunchBBDO — Excel Takip
        </div>
        <div style='font-size:24px;font-weight:bold;margin-top:4px;'>📋 Dashboard</div>
        <div style='font-size:13px;opacity:.7;margin-top:4px;'>Son güncelleme: {ts}</div>
      </div>
      <div style='display:flex;gap:8px;flex-wrap:wrap;'>
        <a href='/run'
           style='background:#00b050;color:#fff;padding:8px 16px;
                  border-radius:6px;font-size:13px;font-weight:bold;'>▶ Rapor Çalıştır</a>
        <a href='/debug'
           style='background:rgba(255,255,255,.15);color:#fff;padding:8px 16px;
                  border-radius:6px;font-size:13px;'>🔍 Debug</a>
      </div>
    </div>

    {stat_cards}
    {waiter_section}

    <div style='background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.1);overflow:hidden;'>
      <div style='background:#1a1a2e;color:#fff;padding:12px 16px;font-weight:bold;'>
        📅 Son 14 Gün Geçmiş
      </div>
      <table style='width:100%;border-collapse:collapse;'>
        <tr style='background:#fafafa;'>
          <th style='padding:8px 12px;text-align:left;font-size:12px;color:#888;'>Tarih</th>
          <th style='padding:8px 12px;text-align:center;font-size:12px;color:#888;'>Onayda</th>
          <th style='padding:8px 12px;text-align:center;font-size:12px;color:#888;'>Silinecek</th>
          <th style='padding:8px 12px;text-align:center;font-size:12px;color:#888;'>Eklenecek</th>
          <th style='padding:8px 12px;text-align:center;font-size:12px;color:#888;'>Tamamlandı</th>
          <th style='padding:8px 12px;text-align:center;font-size:12px;color:#888;'>Yeni Onay</th>
        </tr>
        {hist_rows}
      </table>
    </div>

    <div style='text-align:center;font-size:11px;color:#aaa;margin-top:20px;'>
      bc-takip-production.up.railway.app ·
      <a href='/run'>Manuel çalıştır</a> ·
      <a href='/health'>Health</a> ·
      <a href='/debug-excel'>Excel Debug</a>
    </div>
  </div>
</body></html>""", 200


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
    try:
        tasks = read_excel_tasks()
    except Exception as e:
        return f"<pre>Excel hatası: {e}</pre>", 500

    lines = [f"Excel'den {len(tasks)} iş okundu:\n"]
    for t in tasks:
        lines.append(
            f"[{t['brand']}] {t['name']}\n"
            f"  todo_id={t.get('todo_id')} bucket_id={t.get('bucket_id')} "
            f"acct={t.get('url_account_id')} color={t.get('cell_color')}\n"
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

    # Haftaiçi her gün 18:00 — günlük rapor
    scheduler.add_job(
        func=run_report,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Europe/Istanbul"),
        kwargs={"trigger": "cron"},
        id="daily_report",
        replace_existing=True,
    )

    # Cuma 18:05 — haftalık özet (günlük rapor bittikten 5 dk sonra)
    scheduler.add_job(
        func=run_weekly_summary,
        trigger=CronTrigger(day_of_week="fri", hour=18, minute=5, timezone="Europe/Istanbul"),
        id="weekly_summary",
        replace_existing=True,
    )

    scheduler.start()
    print("📅 Scheduler: Haftaiçi 18:00 günlük rapor + Cuma 18:05 haftalık özet")


if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

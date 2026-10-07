# -*- coding: utf-8 -*-
# Exaado Radar Job — runs on GitHub Actions (hourly):
# collect -> Groq analysis -> save state -> push to Hassan (morning brief + strong news)
import json, time, re, os, hashlib, base64, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
BOT_TOKEN = os.environ.get("RADAR_BOT_TOKEN", "")
CHAT_ID = os.environ.get("RADAR_CHAT_ID", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
OR_KEY = os.environ.get("OPENROUTER_API_KEY_2") or os.environ.get("OPENROUTER_API_KEY", "")
GH_TOKEN = os.environ.get("RADAR_GH_TOKEN", "")
GH_STATE_API = "https://api.github.com/repos/mexc1433-crypto/exaado-radar/contents/radar_state.json"
REPO = "mexc1433-crypto/exaado-watch"

FIN_AR = ["فوركس", "أسعار الذهب اليوم", "أسواق المال العالمية", "البيتكوين", "الاحتياطي الفيدرالي", "التضخم الأمريكي", "الدولار اليوم"]
FIN_EN = ["gold price", "federal reserve", "bitcoin", "stock market today", "economic calendar this week", "forex market"]
FIN_WORDS = ["فوركس","ذهب","دولار","بيتكوين","كريبتو","اقتصاد","تضخم","فيدرالي","بنك مركزي","أسواق","اسواق","عملة","بورصة","نفط","أسهم","اسهم","فائدة",
             "fed","dollar","gold","bitcoin","crypto","inflation","tariff","oil","market","stock","bank","cpi","rate","dxy","treasury","yields","eur","usd"]

def http_get(url, timeout=20, headers=None):
    h = {"User-Agent": UA}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout).read()

def http_post_json(url, payload, headers=None, timeout=60, method="POST"):
    h = {"Content-Type": "application/json", "User-Agent": "exaado-radar/1.0"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h, method=method)
    return urllib.request.urlopen(req, timeout=timeout).read()

def _last_wd(year, month, wd):
    import calendar
    ld = calendar.monthrange(year, month)[1]
    d = datetime(year, month, ld)
    while d.weekday() != wd: d -= timedelta(days=1)
    return d.day

def cairo_offset_hours(now=None):
    now = now or datetime.now(timezone.utc)
    y = now.year
    start = datetime(y, 4, _last_wd(y, 4, 4), 0, 0, tzinfo=timezone.utc)
    end = datetime(y, 10, _last_wd(y, 10, 3), 23, 59, tzinfo=timezone.utc)
    return 3 if start <= now < end else 2

def cairo_now():
    return datetime.now(timezone(timedelta(hours=cairo_offset_hours())))

def fmt_cairo(ts):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=cairo_offset_hours()))).strftime("%Y-%m-%d %H:%M")

# ---------- Telegram ----------
def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID: return False
    http_post_json("https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN,
                   {"chat_id": int(CHAT_ID), "text": text[:4000], "parse_mode": "HTML",
                    "disable_web_page_preview": True}, timeout=30)
    return True

# ---------- State on GitHub ----------
def gh_state_load():
    h = {"Authorization": "token %s" % GH_TOKEN, "Accept": "application/vnd.github+json"}
    d = json.loads(http_get(GH_STATE_API + "?t=%d" % int(time.time()), headers=h))
    return json.loads(base64.b64decode(d["content"])), d["sha"]

def gh_state_save(st, sha):
    h = {"Authorization": "token %s" % GH_TOKEN, "Accept": "application/vnd.github+json"}
    payload = {"message": "radar state [skip ci]", "sha": sha,
               "content": base64.b64encode(json.dumps(st, ensure_ascii=False, indent=1).encode()).decode()}
    http_post_json(GH_STATE_API, payload, headers=h, timeout=30, method="PUT")

# ---------- Collection ----------
def _rss_titles(xml, limit=12):
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    out = []
    for it in items[:limit]:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", it)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", it, re.S)
        s = re.search(r"<source[^>]*>(.*?)</source>", it)
        if t:
            out.append({"title": t.group(1).strip(), "date": d.group(1) if d else "",
                        "link": l.group(1).strip() if l else "", "source": s.group(1) if s else ""})
    return out

def collect_trends():
    trends = []
    for geo in ["EG", "US"]:
        try:
            xml = http_get("https://trends.google.com/trending/rss?geo=%s" % geo, timeout=15).decode("utf-8", "ignore")
            for it in _rss_titles(xml, 20):
                if any(w in it["title"].lower() for w in FIN_WORDS):
                    it["geo"] = geo
                    trends.append(it)
        except Exception: pass
    return trends

def collect_news():
    news = []
    for q in FIN_AR:
        try:
            url = "https://news.google.com/rss/search?q=%s&hl=ar&gl=EG&ceid=EG:ar" % urllib.parse.quote(q)
            for it in _rss_titles(http_get(url, timeout=15).decode("utf-8", "ignore"), 6):
                it["lang"] = "ar"; news.append(it)
        except Exception: pass
    for q in FIN_EN:
        try:
            url = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % urllib.parse.quote(q)
            for it in _rss_titles(http_get(url, timeout=15).decode("utf-8", "ignore"), 6):
                it["lang"] = "en"; news.append(it)
        except Exception: pass
    seen, uniq = set(), []
    for n in news:
        k = n["title"][:70]
        if k not in seen: seen.add(k); uniq.append(n)
    return uniq

def collect_prices():
    p = {}
    try:
        d = json.loads(http_get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true", timeout=10))
        p["BTC"] = {"usd": d["bitcoin"]["usd"], "chg": round(d["bitcoin"].get("usd_24h_change") or 0, 2)}
        p["ETH"] = {"usd": d["ethereum"]["usd"], "chg": round(d["ethereum"].get("usd_24h_change") or 0, 2)}
    except Exception: pass
    try:
        d = json.loads(http_get("https://api.gold-api.com/price/XAU", timeout=10))
        p["XAU"] = {"usd": d["price"], "chg": None}
    except Exception: pass
    try:
        d = json.loads(http_get("https://open.er-api.com/v6/latest/USD", timeout=10))
        p["EURUSD"] = {"usd": round(1 / d["rates"]["EUR"], 4), "chg": None}
        p["USDEGP"] = {"usd": round(d["rates"]["EGP"], 3), "chg": None}
    except Exception: pass
    return p

# ---------- LLM ----------
ANALYSIS_SYS = """أنت محرر مالي محترف في منصة إكسادو لتعليم التداول. حلل بيانات ترندات وأخبار الأسواق (فوركس، ذهب، كريبتو، أسهم، اقتصاد) واختر الأهم.
قواعد صارمة:
- الترندات: أعلى 5 مواضيع مرتبطة بالتداول وأسواق المال والاقتصاد فقط (ممنوع أخبار التجارة الإلكترونية أو التسويق أو الترفيه). لو أقل من 5 قدم المتاح.
- الخبر القوي: فقط أخبار بتأثير حقيقي على السوق (فيدرالي، بنوك مركزية، تضخم، بيانات وظائف، عملات رئيسية، ذهب، بيتكوين) وحجم حدث كبير. الخبر الخفيف يترفض.
- اللغة: عامية مصرية ذكية ومحترفة بدون لغة خشبية.
- الأرقام من العناوين المعطاة حرفيًا — ممنوع اختلاق أرقام.
- الأجندة: لو في أحداث اقتصادية قادمة مذكورة في العناوين أو معروفة من التاريخ الحالي، اذكرها بالتاريخ والموعد المتوقع.
أرجع JSON فقط:
{"top_trends":[{"title":"...","why":"سطر","source":"المصدر"}],
"strong_news":[{"title":"...","summary":"سطرين بالأرقام من العنوان","why_strong":"سطر","score":1-10,"sources":["لينك","لينك"]}],
"events":[{"day":"الخميس 8/10","time_cairo":"20:30","event":"بيان كذا","currency":"USD","importance":"عالي"}]}
لو مفيش أخبار قوية أو أحداث، سيبها فاضية [] — ممنوع حشو."""

def _chat(base, auth, model, system, user, timeout=90, json_mode=False):
    body = {"model": model, "temperature": 0.3, "max_tokens": 4000,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode: body["response_format"] = {"type": "json_object"}
    d = json.loads(http_post_json(base + "/chat/completions", body,
                                   headers={"Authorization": "Bearer %s" % auth}, timeout=timeout))
    return d["choices"][0]["message"]["content"]

def _extract_json(raw):
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m: raise ValueError("no json")
    return json.loads(m.group(0))

def llm_json(system, user):
    attempts = []
    if GROQ_KEY:
        attempts.append(("https://api.groq.com/openai/v1", GROQ_KEY, "openai/gpt-oss-120b", False))
        attempts.append(("https://api.groq.com/openai/v1", GROQ_KEY, "qwen/qwen3.8-27b", True))
    if OR_KEY:
        attempts.append(("https://openrouter.ai/api/v1", OR_KEY, "meta-llama/llama-3.3-70b-instruct:free", False))
    last = None
    for base, auth, model, jm in attempts:
        try:
            raw = _chat(base, auth, model, system, user, json_mode=jm)
            return json.loads(raw) if jm else _extract_json(raw)
        except Exception as e:
            last = e
    raise RuntimeError("LLM failed: %s" % last)

# ---------- Formatting ----------
def _num(v):
    try: return format(float(v), ",.1f").rstrip("0").rstrip(".")
    except Exception: return str(v)

def prices_block(a):
    p = a.get("prices", {})
    rows = []
    if "XAU" in p: rows.append("🥇 الذهب: <b>%s$</b>" % _num(p["XAU"]["usd"]))
    if "BTC" in p: rows.append("₿ BTC: <b>%s$</b> (%s%%)" % (_num(p["BTC"]["usd"]), p["BTC"].get("chg", 0)))
    if "ETH" in p: rows.append("Ξ ETH: <b>%s$</b> (%s%%)" % (_num(p["ETH"]["usd"]), p["ETH"].get("chg", 0)))
    if "EURUSD" in p: rows.append("💱 EUR/USD: <b>%s</b>" % p["EURUSD"]["usd"])
    if "USDEGP" in p: rows.append("🇪🇬 دولار/جنيه: <b>%s</b>" % p["USDEGP"]["usd"])
    return "\n".join(rows)

def trends_msg(a):
    lines = ["🛰️ <b>أعلى ترندات التداول والأسواق</b>", "تحديث: %s" % fmt_cairo(a["ts"]), ""]
    for i, t in enumerate(a.get("trends", [])[:5], 1):
        lines.append("%d. 🔥 <b>%s</b>" % (i, t.get("title", "")))
        lines.append("   <i>%s</i>" % t.get("why", ""))
        if t.get("source"): lines.append("   المصدر: %s" % t["source"])
        lines.append("")
    if not a.get("trends"): lines.append("مفيش ترندات جديدة في المجال دلوقتي.")
    return "\n".join(lines)

def _h(s):
    return hashlib.md5((s or "")[:80].encode()).hexdigest()

# ---------- Main ----------
def main():
    trends, news, prices = collect_trends(), collect_news(), collect_prices()
    compact_news = [{"t": n["title"][:110], "s": (n["source"] or "")[:30], "l": n["link"][:95]}
                    for n in news[:30]]
    compact_tr = [{"t": t["title"][:80], "g": t["geo"]} for t in trends[:15]]
    today = cairo_now().strftime("%A %d/%m %H:%M")
    user = "التاريخ الآن (القاهرة): %s\n\nالترندات من جوجل:\n%s\n\nعناوين الأخبار (الأحدث):\n%s\n\nالأسعار الآن: %s\n\nحلل وأرجع JSON." % (
        today, json.dumps(compact_tr, ensure_ascii=False), json.dumps(compact_news, ensure_ascii=False), json.dumps(prices))
    res = llm_json(ANALYSIS_SYS, user)
    a = {"ts": time.time(), "prices": prices,
         "trends": (res.get("top_trends") or [])[:5],
         "news_brief": (res.get("strong_news") or [])[:5],
         "events": (res.get("events") or [])[:15]}
    # load state
    try:
        st, sha = gh_state_load()
    except Exception:
        st, sha = {}, None
    old = st.get("analysis") or {}
    st["analysis"] = a
    pushed = 0
    # 1) morning briefing
    now = cairo_now()
    if 8 <= now.hour < 12 and st.get("morning_date") != now.strftime("%Y-%m-%d"):
        st["morning_date"] = now.strftime("%Y-%m-%d")
        ev_lines = ""
        if a.get("events"):
            ev_lines = "\n\n📅 <b>أحداث النهاردة القادمة:</b>\n" + "\n".join(
                "• %s — %s %s" % (e.get("time_cairo", ""), e.get("event", ""), e.get("currency", ""))
                for e in a["events"][:8])
        tg_send("☀️ <b>صباح الخير يا حسن — رادار إكسادو</b>\n\n%s\n\n%s%s" % (trends_msg(a), prices_block(a), ev_lines))
        pushed += 1
    # 2) strong news alerts (score>=8, dedupe)
    seen = st.setdefault("pushed_news", {})
    for n in a.get("news_brief", []):
        if (n.get("score") or 0) >= 8:
            key = _h(n.get("title"))
            if key not in seen:
                seen[key] = int(time.time())
                lines = ["🔥 <b>خبر قوي من رادار إكسادو</b>", "", "<b>%s</b>" % n.get("title", ""),
                         n.get("summary", "")]
                if n.get("why_strong"): lines.append("💡 <i>%s</i>" % n["why_strong"])
                for s in (n.get("sources") or [])[:2]: lines.append("🔗 %s" % s)
                tg_send("\n".join(lines))
                pushed += 1
    if len(seen) > 200:
        for k in sorted(seen, key=seen.get)[:100]: del seen[k]
    st["last_run"] = time.time()
    gh_state_save(st, sha)
    print("RADAR OK | trends=%d news=%d events=%d pushed=%d" % (
        len(a["trends"]), len(a["news_brief"]), len(a["events"]), pushed))

if __name__ == "__main__":
    main()

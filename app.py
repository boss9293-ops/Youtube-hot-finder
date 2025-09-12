# app.py — YouTube Hot Finder (simple globalized)
# - 입력 탭: 원문 키워드 → 실시간 번역(예: ko→ja), "번역을 검색에 사용" 체크 시 번역본으로 검색
# - 설정 탭: 국가 범위(한국만/해외만/한국+해외)만 선택 — 중복되는 언어/국가 입력 제거
# - 결과 테이블: 헤더 정렬 + hover 미리보기 (JS, rerun 없음)
# - 키워드 엄격 필터(제목/설명/태그), Excel, Transcript(SRT/ZIP), 쿼터 추적
# - Windows 친화, pyarrow 미사용

import os
import time
import json
import math
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

import streamlit as st
import streamlit.components.v1 as components

# === Secrets → session_state (최우선) ===
if "api_keys" not in st.session_state:
    # 배열형(권장)
    keys = list(st.secrets.get("YOUTUBE_API_KEYS", []))
    # 단일 키 호환
    if not keys and "YOUTUBE_API_KEY" in st.secrets:
        keys = [st.secrets["YOUTUBE_API_KEY"]]

    st.session_state["api_keys"] = keys
    st.session_state["api_key_idx"] = 0
    if keys:
        st.session_state["api_key"] = keys[0]  # 기존 코드와의 호환


# -----------------------
# Constants / Config
# -----------------------
API_BASE = "https://www.googleapis.com/youtube/v3"
DEFAULT_DAILY_QUOTA = 10_000
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".youtube_hot_finder.json")

LANG_NAME = {
    "ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese",
    "es": "Spanish", "de": "German", "fr": "French", "pt": "Portuguese"
}

# 해외 기본 프리셋(필요 시 수정)
FOREIGN_PRESET = ["US","JP","TW","HK","SG","GB","DE","FR","ES","BR"]

# -----------------------
# API Key persistence
# -----------------------
def load_api_key_from_disk() -> Optional[str]:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("api_key")
    except Exception:
        pass
    return None

def save_api_key_to_disk(key: str) -> bool:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"api_key": key}, f)
        return True
    except Exception:
        return False

def delete_api_key_on_disk() -> bool:
    try:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
        return True
    except Exception:
        return False

# -----------------------
# Session defaults
# -----------------------
st.session_state.setdefault("q_calls", {"search": 0, "videos": 0, "channels": 0})
st.session_state.setdefault("q_units", 0)
st.session_state.setdefault("q_log", [])
st.session_state.setdefault("api_waiting", False)
st.session_state.setdefault("api_wait_reason", "")
st.session_state.setdefault("results_df", pd.DataFrame())
st.session_state.setdefault("payload_cache", [])
st.session_state.setdefault("lang_pref", "ko")

if "api_key" not in st.session_state:
    st.session_state["api_key"] = load_api_key_from_disk() or ""

def _quota_units_for(endpoint_name: str) -> int:
    if endpoint_name.startswith("search"): return 100
    if endpoint_name.startswith("videos"): return 1
    if endpoint_name.startswith("channels"): return 1
    return 0

def _record_quota(endpoint_name: str, path: str) -> None:
    units = _quota_units_for(endpoint_name)
    if endpoint_name.startswith("search"):
        st.session_state["q_calls"]["search"] += 1
    elif endpoint_name.startswith("videos"):
        st.session_state["q_calls"]["videos"] += 1
    elif endpoint_name.startswith("channels"):
        st.session_state["q_calls"]["channels"] += 1
    st.session_state["q_units"] += units
    st.session_state["q_log"].append((endpoint_name, units, path, time.time()))

# -----------------------
# Translator (cached with fallback)
# -----------------------
@st.cache_data(show_spinner=False)
def translate_keyword_once(src_text: str, src_lang: str, tgt_lang: str) -> str:
    s = (src_text or "").strip()
    if not s or src_lang == tgt_lang:
        return s
    # 1) googletrans
    try:
        from googletrans import Translator
        return Translator().translate(s, src=src_lang, dest=tgt_lang).text
    except Exception:
        pass
    # 2) deep-translator
    try:
        from deep_translator import GoogleTranslator as DTGoogle
        return DTGoogle(source=src_lang, target=tgt_lang).translate(s)
    except Exception:
        return s  # 실패 시 원문

def translate_keywords_list(keywords: List[str], src_lang: str, tgt_lang: str) -> List[str]:
    outs: List[str] = []
    seen = set()
    for k in [x.strip() for x in keywords if x and x.strip()]:
        v = translate_keyword_once(k, src_lang, tgt_lang).strip()
        if v and v.lower() not in seen:
            seen.add(v.lower()); outs.append(v)
    return outs

# -----------------------
# YouTube API helpers
# -----------------------
def yt_get(endpoint: str, params: Dict[str, Any], api_key: str,
           wait_minutes: float = 0.0, max_retries: int = 2) -> Dict[str, Any]:
    params = {**params, "key": api_key}
    tries = 0
    while True:
        r = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
        if r.status_code == 200:
            _record_quota(endpoint, r.url)
            return r.json()

        tries += 1
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        err_reason = (
            (body.get("error", {}).get("errors", [{}])[0].get("reason"))
            or body.get("error", {}).get("message", "")
            or r.text
        )

        if (
            r.status_code in (403, 429)
            and any(k in str(err_reason).lower() for k in ["quota", "daily", "rate", "exceed"])
            and wait_minutes > 0
            and tries <= max_retries
        ):
            st.session_state["api_waiting"] = True
            st.session_state["api_wait_reason"] = f"{endpoint}: {err_reason}"
            wait_secs = int(wait_minutes * 60)
            with st.status("API 쿼터 초과로 대기 중…", expanded=True) as stat:
                for s in range(wait_secs, 0, -1):
                    stat.update(label=f"API 대기 {s}초 남음 (사유: {err_reason})")
                    time.sleep(1)
            st.session_state["api_waiting"] = False
            continue

        raise RuntimeError(f"YouTube API error {r.status_code}: {r.text}")

def iso8601_to_seconds(duration: str) -> int:
    import re
    m = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
    if not m: return 0
    h = int(m.group(1) or 0); m_ = int(m.group(2) or 0); s = int(m.group(3) or 0)
    return h*3600 + m_*60 + s

def batched(iterable: List[Any], n: int):
    batch = []
    for x in iterable:
        batch.append(x)
        if len(batch) == n:
            yield batch; batch = []
    if batch: yield batch

def fetch_videos_by_search(
    api_key: str, query: Optional[str] = None, channel_id: Optional[str] = None,
    region_code: Optional[str] = None, relevance_language: Optional[str] = None,
    published_after: Optional[str] = None, max_results: int = 100,
    order: str = "viewCount", wait_minutes: float = 0.0
) -> List[str]:
    effective_order = "date" if (published_after and order != "date") else order
    ids: List[str] = []
    params = {"part":"snippet","type":"video","maxResults":50,"order":effective_order}
    if query: params["q"] = query
    if channel_id: params["channelId"] = channel_id
    if region_code: params["regionCode"] = region_code
    if relevance_language: params["relevanceLanguage"] = relevance_language
    if published_after: params["publishedAfter"] = published_after

    next_page = None
    while True:
        if next_page: params["pageToken"] = next_page
        data = yt_get("search", params, api_key, wait_minutes=wait_minutes)
        for item in data.get("items", []):
            vid = item["id"]["videoId"]
            ids.append(vid)
            if len(ids) >= max_results: return ids
        next_page = data.get("nextPageToken")
        if not next_page: break
    return ids

def fetch_video_details(api_key: str, video_ids: List[str], wait_minutes: float = 0.0) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    if not video_ids: return details
    for batch in batched(video_ids, 50):
        data = yt_get("videos", {"part":"snippet,contentDetails,statistics","id":",".join(batch)}, api_key, wait_minutes=wait_minutes)
        for item in data.get("items", []):
            details[item["id"]] = item
    return details

def fetch_channel_subs(api_key: str, channel_ids: List[str], wait_minutes: float = 0.0) -> Dict[str, int]:
    subs: Dict[str, int] = {}
    if not channel_ids: return subs
    for batch in batched(channel_ids, 50):
        data = yt_get("channels", {"part":"statistics","id":",".join(batch)}, api_key, wait_minutes=wait_minutes)
        for item in data.get("items", []):
            subs[item["id"]] = int(item["statistics"].get("subscriberCount", 0))
    return subs

def compute_metrics(detail: Dict[str, Any]) -> Dict[str, Any]:
    snip = detail["snippet"]; stats = detail.get("statistics", {}); content = detail.get("contentDetails", {})
    published_dt = datetime.fromisoformat(snip["publishedAt"].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    hours_since = max((now - published_dt).total_seconds() / 3600.0, 1e-6)
    views = int(stats.get("viewCount", 0)); vph = views / hours_since
    dur_sec = iso8601_to_seconds(content.get("duration", "PT0S"))
    return {"publishedAt": published_dt, "views": views, "viewsPerHour": vph, "durationSec": dur_sec}

def human_duration(seconds: int) -> str:
    h = seconds // 3600; m = (seconds % 3600) // 60; s = seconds % 60
    if h: return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
    return f"{int(m):02d}:{int(s):02d}"

def filter_duration_mode(dur_sec: int, mode: str, shorts_sec: int = 60) -> bool:
    if mode == "둘다": return True
    if mode == "쇼츠": return dur_sec < shorts_sec
    if mode == "롱폼": return dur_sec >= shorts_sec
    return True

def parse_list_field(txt: Optional[str]) -> List[str]:
    if not txt: return []
    return [p.strip() for part in txt.split(",") for p in part.split() if p.strip()]

# -----------------------
# Keyword strict filter
# -----------------------
def normalize_text(s: str) -> str:
    return (s or "").lower()

def contains_keywords(text: str, keywords: List[str], mode: str) -> bool:
    if not keywords:
        return True
    t = normalize_text(text)
    ks = [normalize_text(k) for k in keywords if k.strip()]
    if mode == "all":
        return all(k in t for k in ks)
    else:
        return any(k in t for k in ks)

# -----------------------
# HTML/JS component (table + preview)
# -----------------------
def build_component_html(payload: List[Dict[str, Any]]) -> str:
    tpl = r"""
<div id="app-root"></div>
<script type="application/json" id="data">__DATA__</script>
<style>
:root { --bg:#fff; --fg:#0f172a; --muted:#475569; --border:#e5e7eb; --thead-bg:#f3f4f6; --thead-fg:#0f172a; --row-hover:#f8fafc; }
@media (prefers-color-scheme: dark){ :root{ --bg:#0b1020; --fg:#f8fafc; --muted:#cbd5e1; --border:#334155; --thead-bg:#1f2937; --thead-fg:#f8fafc; --row-hover:#111827; } }
html,body{background:transparent;color:var(--fg);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;}
.container{display:grid;grid-template-columns:85% 15%;gap:12px;height:640px;}
.table-wrap{border:1px solid var(--border);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;min-width:0;}
.table-head{background:var(--thead-bg);color:var(--thead-fg);padding:6px 10px;font-weight:700;border-bottom:1px solid var(--border);font-size:13px;}
.table-scroll{overflow:auto;height:100%;}
table{width:100%;border-collapse:collapse;table-layout:fixed;}
th,td{border-bottom:1px solid var(--border);padding:6px 8px;font-size:12px;text-align:left;}
th{position:sticky;top:0;background:var(--thead-bg);color:var(--thead-fg);z-index:2;cursor:pointer;user-select:none;}
tr:hover td{background:var(--row-hover);}
th .caret{opacity:.6;margin-left:4px;}
colgroup col.title{width:24%;} colgroup col.channel{width:14%;} colgroup col.uploaded{width:13%;}
colgroup col.views{width:10%;} colgroup col.vph{width:10%;} colgroup col.subs{width:10%;}
colgroup col.vs{width:9%;} colgroup col.dur{width:10%;}
td.title,th.title{white-space:normal;word-break:break-word;line-height:1.25;font-size:11.5px;}
td:not(.title),th:not(.title){white-space:nowrap;}
.preview{border:1px solid var(--border);border-radius:10px;padding:8px;}
.preview .img-wrap{display:flex;justify-content:center;}
.preview img{width:100%;max-width:200px;height:auto;border-radius:6px;border:1px solid var(--border);display:block;}
.meta{font-size:11px;color:var(--muted);}
.title-pv{font-weight:700;margin:6px 0 4px 0;font-size:12px;}
.link a{color:inherit;text-decoration:underline;font-size:12px;}
.badge{display:inline-block;padding:1px 5px;border:1px solid var(--border);border-radius:6px;font-size:11px;margin-right:4px;}
</style>
<script>
(function(){
  const root = document.getElementById('app-root');
  const data = JSON.parse(document.getElementById('data').textContent || "[]");
  const columns = [
    {key:'title',label:'Video Title',type:'str',className:'title'},
    {key:'channel',label:'Channel',type:'str',className:'channel'},
    {key:'uploaded',label:'Uploaded',type:'time',sortKey:'uploaded_ts',className:'uploaded'},
    {key:'views',label:'Views',type:'num',className:'views'},
    {key:'vph',label:'Views/hr',type:'num',className:'vph'},
    {key:'subs',label:'Subscribers',type:'num',className:'subs'},
    {key:'vs',label:'Views/Subscribers',type:'num',className:'vs'},
    {key:'duration',label:'Duration',type:'dur',sortKey:'duration_sec',className:'dur'},
  ];
  const fmtInt = (n)=> (n==null||isNaN(n))? '' : Number(n).toLocaleString();
  const fmtNum = (n)=> (n==null||isNaN(n))? '' : (Math.round(n*100)/100).toLocaleString();

  let sortKey='vph', sortDir=-1, rows=data.slice();
  function sortRows(){
    rows.sort((a,b)=>{
      const col = columns.find(c=>c.key===sortKey)||{};
      const key = col.sortKey||col.key||sortKey;
      let av=a[key]; let bv=b[key];
      if(av==null) av=-Infinity; if(bv==null) bv=-Infinity;
      if(typeof av==='string' && typeof bv==='string'){ return sortDir * av.localeCompare(bv); }
      return sortDir * ((+av)-(+bv));
    });
  }
  sortRows();

  const container=document.createElement('div'); container.className='container';
  const tableWrap=document.createElement('div'); tableWrap.className='table-wrap';
  const head=document.createElement('div'); head.className='table-head'; head.textContent='Hot Videos';
  const scroll=document.createElement('div'); scroll.className='table-scroll';
  const table=document.createElement('table');

  const colg=document.createElement('colgroup');
  ['title','channel','uploaded','views','vph','subs','vs','dur'].forEach(c=>{const col=document.createElement('col'); col.className=c; colg.appendChild(col);});
  table.appendChild(colg);

  const thead=document.createElement('thead'); const trh=document.createElement('tr');
  columns.forEach(col=>{
    const th=document.createElement('th'); th.className=col.className||''; th.textContent=col.label;
    const caret=document.createElement('span'); caret.className='caret'; caret.textContent=(sortKey===col.key?(sortDir===-1?'▼':'▲'):''); th.appendChild(caret);
    th.addEventListener('click',()=>{ if(sortKey===col.key){sortDir*=-1;} else {sortKey=col.key; sortDir=-1;}
      [...thead.querySelectorAll('th .caret')].forEach(c=>c.textContent=''); caret.textContent=(sortDir===-1?'▼':'▲'); sortRows(); renderBody();});
    trh.appendChild(th);
  });
  thead.appendChild(trh); table.appendChild(thead);
  const tbody=document.createElement('tbody'); table.appendChild(tbody); scroll.appendChild(table);
  tableWrap.appendChild(head); tableWrap.appendChild(scroll);

  const preview=document.createElement('div'); preview.className='preview';
  const imgWrap=document.createElement('div'); imgWrap.className='img-wrap';
  const pvImg=document.createElement('img'); imgWrap.appendChild(pvImg);
  const pvTitle=document.createElement('div'); pvTitle.className='title-pv';
  const pvMeta=document.createElement('div'); pvMeta.className='meta';
  const pvBadges=document.createElement('div');
  const pvLink=document.createElement('div'); pvLink.className='link';
  preview.appendChild(imgWrap); preview.appendChild(pvTitle); preview.appendChild(pvMeta); preview.appendChild(pvBadges); preview.appendChild(pvLink);

  container.appendChild(tableWrap); container.appendChild(preview); root.appendChild(container);

  function escapeHtml(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
  function rowHTML(r){
    return '<tr data-vid="'+r.vid+'">'
      + '<td class="title" title="'+escapeHtml(r.title)+'">'+escapeHtml(r.title)+'</td>'
      + '<td class="channel" title="'+escapeHtml(r.channel)+'">'+escapeHtml(r.channel)+'</td>'
      + '<td class="uploaded" data-sort="'+r.uploaded_ts+'">'+escapeHtml(r.uploaded)+'</td>'
      + '<td class="views" data-sort="'+r.views+'">'+fmtInt(r.views)+'</td>'
      + '<td class="vph" data-sort="'+r.vph+'">'+fmtNum(r.vph)+'</td>'
      + '<td class="subs" data-sort="'+r.subs+'">'+fmtInt(r.subs)+'</td>'
      + '<td class="vs" data-sort="'+(r.vs==null?'':r.vs)+'">'+(r.vs==null?'':r.vs)+'</td>'
      + '<td class="dur" data-sort="'+r.duration_sec+'">'+escapeHtml(r.duration)+'</td>'
      + '</tr>';
  }
  function renderBody(){
    tbody.innerHTML = rows.map(rowHTML).join('');
    Array.prototype.forEach.call(tbody.querySelectorAll('tr'), function(tr){
      tr.addEventListener('mouseenter', function(){
        const vid=tr.getAttribute('data-vid'); const r=rows.find(x=>x.vid===vid); if(!r) return;
        pvImg.src=r.thumb; pvTitle.textContent=r.title; pvMeta.textContent=r.channel+' · '+r.uploaded;
        pvBadges.innerHTML = '<span class="badge">Views: '+fmtInt(r.views)+'</span>'
          + '<span class="badge">VPH: '+fmtNum(r.vph)+'</span>'
          + '<span class="badge">Subs: '+fmtInt(r.subs)+'</span>'
          + (r.vs!=null?'<span class="badge">V/Sub: '+r.vs+'</span>':'')
          + '<span class="badge">Dur: '+r.duration+'</span>';
        pvLink.innerHTML = '<a href="'+r.url+'" target="_blank" rel="noreferrer">▶ Open on YouTube</a>';
      }, {passive:true});
    });
  }
  renderBody();
  if(rows.length){
    const r=rows[0]; pvImg.src=r.thumb; pvTitle.textContent=r.title; pvMeta.textContent=r.channel+' · '+r.uploaded;
    pvBadges.innerHTML = '<span class="badge">Views: '+fmtInt(r.views)+'</span>'
      + '<span class="badge">VPH: '+fmtNum(r.vph)+'</span>'
      + '<span class="badge">Subs: '+fmtInt(r.subs)+'</span>'
      + (r.vs!=null?'<span class="badge">V/Sub: '+r.vs+'</span>':'')
      + '<span class="badge">Dur: '+r.duration+'</span>';
    pvLink.innerHTML = '<a href="'+r.url+'" target="_blank" rel="noreferrer">▶ Open on YouTube</a>';
  }
})();
</script>
"""
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__DATA__", data_json)

# -----------------------
# Transcript helpers
# -----------------------
def _format_srt_time(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

@st.cache_data(show_spinner=False)
def fetch_transcript_srt(video_id: str, lang_pref: str = "ko") -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return None
    langs = []
    if lang_pref:
        langs.append(lang_pref)
        if "-" in lang_pref:
            langs.append(lang_pref.split("-")[0])
    for extra in ["en", "ko"]:
        if extra not in langs:
            langs.append(extra)
    try:
        list_obj = YouTubeTranscriptApi.list_transcripts(video_id)
        segs = None
        for lp in langs:
            try:
                tr = list_obj.find_transcript([lp])
                segs = tr.fetch(); break
            except Exception:
                pass
        if segs is None:
            try:
                tr = list_obj.find_transcript(list_obj._generated_transcripts_language_codes)
                tr = tr.translate(langs[0]); segs = tr.fetch()
            except Exception:
                return None
    except Exception:
        return None

    lines = []
    for idx, seg in enumerate(segs, start=1):
        start = float(seg.get("start", 0.0)); dur = float(seg.get("duration", 0.0)); end = start + dur
        text = (seg.get("text") or "").replace("\n", " ").strip()
        lines.append(str(idx)); lines.append(f"{_format_srt_time(start)} --> {_format_srt_time(end)}")
        lines.append(text if text else ""); lines.append("")
    return "\n".join(lines) if lines else None

def _safe_filename(s: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join(c for c in s if c not in bad)
    return out[:120].strip() or "video"

@st.cache_data(show_spinner=False)
def build_transcripts_zip_cached(vids: Tuple[str, ...], labels: Tuple[str, ...], lang_pref: str) -> bytes:
    from io import BytesIO; import zipfile
    buf = BytesIO(); missing = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for label, vid in zip(labels, vids):
            srt = fetch_transcript_srt(vid, lang_pref=lang_pref)
            if srt:
                fn = _safe_filename(label)[:100] + ".srt"; zf.writestr(fn, srt)
            else:
                missing.append(label)
        if missing:
            zf.writestr("README.txt", "No transcript for:\n\n" + "\n".join(f"- {m}" for m in missing))
    return buf.getvalue()

# -----------------------
# Streamlit Page
# -----------------------
st.set_page_config(page_title="YouTube Hot Finder", layout="wide")
st.title("🔥 YouTube Hot Finder")

# Live quota header
quota_box = st.container()
def render_quota_header():
    used = st.session_state["q_units"]
    left = max(DEFAULT_DAILY_QUOTA - used, 0)
    p = min(used / DEFAULT_DAILY_QUOTA, 1.0)
    with quota_box:
        st.progress(p, text=f"일일 쿼터 사용량: {used:,} / {DEFAULT_DAILY_QUOTA:,}  (남음 {left:,})")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("search.list calls", f"{st.session_state['q_calls']['search']}")
        c2.metric("videos.list calls", f"{st.session_state['q_calls']['videos']}")
        c3.metric("channels.list calls", f"{st.session_state['q_calls']['channels']}")
        c4.metric("총 유닛", f"{used:,}")
render_quota_header()

# Tabs
tab_input, tab_settings, tab_results = st.tabs(["키워드·채널 입력", "설정", "결과"])

# -----------------------
# Input Tab (live translation)
# -----------------------
with tab_input:
    st.subheader("키워드 / 채널핸들 입력")

    col_sel1, col_sel2, col_chk = st.columns([0.25, 0.25, 0.5])
    with col_sel1:
        live_src = st.selectbox("입력 언어", ["ko","en","ja","zh","es","de","fr","pt"], index=0, key="live_src")
    with col_sel2:
        live_dst = st.selectbox("변환 언어", ["ja","ko","en","zh","es","de","fr","pt"], index=0, key="live_dst")  # 기본 ja
    with col_chk:
        live_replace = st.checkbox("번역본을 검색에 사용(원문 대신)", value=True, key="live_replace")

    c1, c2 = st.columns(2)
    with c1:
        st.text_area("키워드(원문: 쉼표/스페이스/줄바꿈 구분)", key="kw_src", height=80, placeholder="예: 시니어, 황혼")
    # 즉시 변환
    src_list = parse_list_field(st.session_state.get("kw_src",""))
    dst_list = translate_keywords_list(src_list, st.session_state["live_src"], st.session_state["live_dst"])
    with c2:
        st.text_area(f"키워드(변환: {st.session_state['live_dst']})", value=", ".join(dst_list), height=80, disabled=True)

    # 검색에 사용할 최종 키워드/언어
    st.session_state["effective_keywords"] = dst_list if st.session_state["live_replace"] else src_list
    st.session_state["effective_lang"] = st.session_state["live_dst"] if st.session_state["live_replace"] else st.session_state["live_src"]

    channels_input = st.text_area("채널 핸들 또는 채널 ID (쉼표/스페이스/줄바꿈 구분)", key="channels_input", height=80, placeholder="@channel 또는 UCxxxxx")

# -----------------------
# Settings Tab (simple scope)
# -----------------------
with tab_settings:
    st.subheader("설정")
    with st.container():
        # API 키
        st.text_input("YouTube Data API v3 키", type="password", key="api_key")
        api_key = st.session_state.get("api_key", "")

        b1, b2, b3 = st.columns([0.25, 0.25, 0.5])
        if b1.button("API 키 저장"):
            if api_key:
                ok = save_api_key_to_disk(api_key)
                st.success("API 키를 로컬에 저장했습니다." if ok else "저장 실패.")
            else:
                st.warning("API 키가 비어 있습니다.")
        if b2.button("API 키 삭제"):
            ok = delete_api_key_on_disk()
            if ok:
                st.session_state["api_key"] = ""; st.success("로컬 저장된 API 키를 삭제했습니다.")
            else:
                st.warning("삭제 실패 또는 저장된 키 없음.")
        b3.caption(f"로컬 경로: `{CONFIG_PATH}` (개인PC 외 저장 비권장)")

        # 검색/필터 옵션
        run_mode = st.selectbox("실행모드", ["채널", "키워드", "둘다"], index=2, key="run_mode")
        form_factor = st.selectbox("쇼츠/롱폼", ["쇼츠", "롱폼", "둘다"], key="form_factor")
        shorts_sec = st.number_input("쇼츠 기준(초)", min_value=10, max_value=300, value=60, step=5, key="shorts_sec")
        days_back = st.number_input("최근 몇일간의 영상을 분석할까요", min_value=1, max_value=3650, value=180, key="days_back")
        per_channel_max = st.number_input("채널당 최대 검색 수", min_value=10, max_value=1000, value=200, step=10, key="per_channel_max")
        per_keyword_max = st.number_input("검색어당 최대 검색수", min_value=10, max_value=1000, value=200, step=10, key="per_keyword_max")
        min_vph = st.number_input("최소 시간당 조회수", min_value=0.0, value=0.0, step=10.0, key="min_vph")
        wait_minutes = st.number_input("API키 쿼터 소진 시 대기시간(분)", min_value=0.0, value=0.0, step=0.5, key="wait_minutes")
        ignore_filters = st.checkbox("테스트용: 길이/시간당 조회수 필터 무시", value=False, key="ignore_filters")

        # 국가 범위만 선택(심플)
        st.markdown("### 🌐 국가 범위")
        scope = st.radio("검색 범위", ["한국만", "해외만", "한국+해외"], index=2, horizontal=True, key="region_scope")
        overseas_regions = []
        if scope in ("해외만", "한국+해외"):
            overseas_regions = st.multiselect("해외 국가 선택", options=FOREIGN_PRESET, default=FOREIGN_PRESET, key="overseas_regions")
        target_regions = (["KR"] if scope in ("한국만","한국+해외") else []) + (overseas_regions if scope in ("해외만","한국+해외") else [])
        st.session_state["target_regions"] = target_regions

        # 키워드 엄격 필터 옵션
        st.markdown("**키워드 정확도 옵션**")
        strict_on = st.checkbox("키워드 엄격 필터링 (제목/설명/태그 검사)", value=True, key="kw_strict_on")
        strict_mode = st.radio("매칭 방식", options=["하나 이상 포함(권장)", "모두 포함(엄격)"], index=0, horizontal=True, key="kw_strict_mode")

        # Quota Estimator (대략)
        st.subheader("🔢 쿼터 예상 소모량")
        def parse_for_estimator(txt: Optional[str]) -> List[str]:
            return [p.strip() for part in (txt or "").split(",") for p in part.split() if p.strip()]
        ch_list = parse_for_estimator(st.session_state.get("channels_input","")) if st.session_state["run_mode"] in ("채널","둘다") else []
        kw_list = st.session_state.get("effective_keywords", []) if st.session_state["run_mode"] in ("키워드","둘다") else []
        est_videos = len(ch_list) * st.session_state["per_channel_max"] + len(kw_list) * st.session_state["per_keyword_max"]
        search_calls = len(ch_list) * math.ceil(st.session_state["per_channel_max"]/50) + len(kw_list) * math.ceil(st.session_state["per_keyword_max"]/50)
        search_units = search_calls * 100
        videos_calls = math.ceil(est_videos/50) if est_videos else 0
        videos_units = videos_calls * 1
        chan_calls_min = math.ceil((len(ch_list) or 0)/50) if est_videos else 0
        chan_calls_max = math.ceil(est_videos/50) if est_videos else 0
        chan_units_min = chan_calls_min * 1
        chan_units_max = chan_calls_max * 1
        total_units_min = search_units + videos_units + chan_units_min
        total_units_max = search_units + videos_units + chan_units_max
        quota = DEFAULT_DAILY_QUOTA
        warn = total_units_max > quota

        cA, cB, cC, cD = st.columns(4)
        cA.metric("search.list(100/u)", f"{search_units:,}", f"{search_calls} calls")
        cB.metric("videos.list(1/u)", f"{videos_units:,}", f"{videos_calls} calls")
        cC.metric("channels.list(1/u)", f"{chan_units_min:,} ~ {chan_units_max:,}", f"{chan_calls_min}~{chan_calls_max} calls")
        cD.metric("총 예상(최소~최대)", f"{total_units_min:,} ~ {total_units_max:,}", f"일일 한도 {quota:,}")

        if not warn: st.success("대부분 한도 내에서 동작합니다.")
        else: st.error("최대 추정 사용량이 일일 한도를 초과할 수 있습니다. 검색 개수/키워드/채널 수를 조정하세요.")

        col_run, col_clear = st.columns([0.25, 0.25])
        run = col_run.button("시작하기", type="primary", key="run_btn")
        clear = col_clear.button("결과 지우기", key="clear_btn")
        if clear:
            st.session_state["results_df"] = pd.DataFrame()
            st.session_state["payload_cache"] = []
            st.experimental_rerun()

# -----------------------
# Main run
# -----------------------
if 'run' in locals() and run:
    api_key = st.session_state.get("api_key", "")
    if not api_key:
        st.error("설정 탭에서 API 키를 입력하세요.")
        st.stop()

    run_mode = st.session_state["run_mode"]
    form_factor = st.session_state["form_factor"]
    shorts_sec = int(st.session_state["shorts_sec"])
    days_back = int(st.session_state["days_back"])
    per_channel_max = int(st.session_state["per_channel_max"])
    per_keyword_max = int(st.session_state["per_keyword_max"])
    min_vph = float(st.session_state["min_vph"])
    wait_minutes = float(st.session_state["wait_minutes"])
    ignore_filters = bool(st.session_state["ignore_filters"])
    target_regions = st.session_state.get("target_regions", ["KR"])

    strict_on = bool(st.session_state["kw_strict_on"])
    strict_mode_val = st.session_state["kw_strict_mode"]
    strict_mode_key = "all" if strict_mode_val == "모두 포함(엄격)" else "any"

    # 입력 탭에서 결정된 최종 키워드/언어
    base_keywords = st.session_state.get("effective_keywords", [])
    effective_lang = st.session_state.get("effective_lang", "ko")

    def parse_list_field_inner(txt: Optional[str]) -> List[str]:
        if not txt: return []
        return [p.strip() for part in txt.split(",") for p in part.split() if p.strip()]

    input_channels = parse_list_field_inner(st.session_state.get("channels_input","")) if run_mode in ("채널","둘다") else []

    if len(input_channels) == 0 and len(base_keywords) == 0:
        st.error("실행모드에 맞게 채널 또는 키워드를 최소 1개 이상 입력하세요.")
        st.stop()

    with st.spinner("검색 중…"):
        published_after = (datetime.utcnow() - timedelta(days=days_back)).isoformat("T") + "Z"

        def resolve_channel_ids(lst: List[str]) -> List[str]:
            out: List[str] = []
            for token in lst:
                if token.startswith("@"):
                    data = yt_get("search", {"part":"snippet", "type":"channel", "q": token, "maxResults": 1}, api_key, wait_minutes=wait_minutes)
                    items = data.get("items", [])
                    ch_id = items[0]["snippet"].get("channelId") if items else None
                    if not ch_id and items: ch_id = items[0]["id"].get("channelId")
                    if ch_id: out.append(ch_id)
                else:
                    out.append(token)
            return out

        channels = resolve_channel_ids(input_channels) if run_mode in ("채널","둘다") else []

        collected_ids = set()

        # 채널 모드
        if run_mode in ("채널","둘다"):
            for region in target_regions:
                for ch in channels:
                    ids = fetch_videos_by_search(
                        api_key, channel_id=ch,
                        region_code=region, relevance_language=effective_lang,
                        published_after=published_after, max_results=per_channel_max,
                        order="date", wait_minutes=wait_minutes
                    )
                    collected_ids.update(ids); time.sleep(0.02)

        # 키워드 모드
        if run_mode in ("키워드","둘다"):
            for region in target_regions:
                for kw in base_keywords:
                    if not kw: continue
                    ids = fetch_videos_by_search(
                        api_key, query=kw,
                        region_code=region, relevance_language=effective_lang,
                        published_after=published_after, max_results=per_keyword_max,
                        order="viewCount", wait_minutes=wait_minutes
                    )
                    collected_ids.update(ids); time.sleep(0.02)

        st.info(f"수집된 비디오 ID 수: {len(collected_ids)}")

        details = fetch_video_details(api_key, list(collected_ids), wait_minutes=wait_minutes)
        st.info(f"상세 조회된 비디오 수: {len(details)}")

        channel_ids = {v["snippet"]["channelId"] for v in details.values() if "snippet" in v}
        subs_map = fetch_channel_subs(api_key, list(channel_ids), wait_minutes=wait_minutes) if channel_ids else {}

        # 엄격 필터용 키워드(현재 사용 중인 언어의 키워드만)
        all_keywords_norm = [normalize_text(k) for k in base_keywords]

        rows: List[Dict[str, Any]] = []
        for vid, detail in details.items():
            snip = detail["snippet"]
            metrics = compute_metrics(detail)
            dur_sec = metrics["durationSec"]

            if not ignore_filters:
                if not filter_duration_mode(dur_sec, form_factor, shorts_sec=int(shorts_sec)):
                    continue
                if metrics["viewsPerHour"] < float(min_vph):
                    continue

            if strict_on and all_keywords_norm:
                title = snip.get("title") or ""
                desc = snip.get("description") or ""
                tags = detail.get("snippet", {}).get("tags", [])
                tag_text = " ".join(tags) if isinstance(tags, list) else ""
                combined = f"{title}\n{desc}\n{tag_text}"
                if not contains_keywords(combined, all_keywords_norm, mode=strict_mode_key):
                    continue

            ch_id = snip["channelId"]
            subs = int(subs_map.get(ch_id, 0))
            vs = (metrics["views"]/subs) if subs > 0 else None
            thumb = (snip.get("thumbnails", {}).get("medium")
                     or snip.get("thumbnails", {}).get("high")
                     or snip.get("thumbnails", {}).get("default")
                     or {}).get("url", "")
            rows.append({
                "Channel": snip["channelTitle"],
                "Video Title": snip["title"],
                "Uploaded": metrics["publishedAt"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "_Uploaded_ts": metrics["publishedAt"].timestamp(),
                "Views": metrics["views"],
                "Views/hr": round(metrics["viewsPerHour"], 2),
                "Subscribers": subs,
                "Views/Subscribers": round(vs, 3) if vs is not None else None,
                "Duration": human_duration(dur_sec),
                "_Duration_sec": dur_sec,
                "URL": f"https://www.youtube.com/watch?v={vid}",
                "_vid": vid,
                "_thumb": thumb or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            })

        results_df = pd.DataFrame(rows, columns=[
            "Channel","Video Title","Uploaded","_Uploaded_ts","Views","Views/hr",
            "Subscribers","Views/Subscribers","Duration","_Duration_sec","URL","_vid","_thumb"
        ])
        st.session_state["results_df"] = results_df
        st.session_state["payload_cache"] = []  # 새 검색 시 캐시 무효화

# -----------------------
# Results tab
# -----------------------
with tab_results:
    st.subheader("결과")
    df = st.session_state.get("results_df", pd.DataFrame())
    if df.empty:
        st.info("아직 결과가 없습니다. 설정 탭에서 ‘시작하기’를 눌러 검색해 주세요.")
    else:
        st.success(f"{len(df)}개 결과")
        df_sorted = df.sort_values(by=["Views/hr","Views"], ascending=[False, False], kind="mergesort")

        if st.session_state["payload_cache"]:
            payload = st.session_state["payload_cache"]
        else:
            payload: List[Dict[str, Any]] = []
            for _, r in df_sorted.iterrows():
                payload.append({
                    "channel": r["Channel"], "title": r["Video Title"],
                    "uploaded": r["Uploaded"], "uploaded_ts": float(r["_Uploaded_ts"]),
                    "views": int(r["Views"]), "vph": float(r["Views/hr"]),
                    "subs": int(r["Subscribers"]),
                    "vs": (float(r["Views/Subscribers"]) if pd.notna(r["Views/Subscribers"]) else None),
                    "duration": r["Duration"], "duration_sec": float(r["_Duration_sec"]),
                    "url": r["URL"], "vid": r["_vid"], "thumb": r["_thumb"],
                })
            st.session_state["payload_cache"] = payload

        html = build_component_html(st.session_state["payload_cache"])
        components.html(html, height=680, scrolling=False)

        @st.cache_data
        def to_excel(dfi: pd.DataFrame) -> bytes:
            from io import BytesIO
            try:
                import openpyxl  # noqa: F401
            except Exception:
                bio = BytesIO(); bio.write(b"Install openpyxl: pip install openpyxl"); return bio.getvalue()
            out = BytesIO()
            export_df = dfi.drop(columns=["_Uploaded_ts","_Duration_sec","_vid","_thumb"], errors="ignore")
            with pd.ExcelWriter(out, engine="openpyxl") as writer:
                export_df.to_excel(writer, index=False, sheet_name="HotVideos")
            return out.getvalue()

        xlsx = to_excel(df_sorted)
        st.download_button("Download Excel", data=xlsx,
                           file_name="youtube_hot_finder.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        # Transcripts
        st.markdown("### 📝 Transcripts 다운로드 (공개 자막이 있는 영상만)")
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # noqa: F401
            transcripts_available = True
        except Exception:
            transcripts_available = False
            st.info("Transcript 기능을 사용하려면 다음을 설치하세요:\n\n`pip install youtube-transcript-api`")

        if transcripts_available:
            titles_map = {
                f"{row['Video Title']}  —  ({row['Channel']}) [{row['_vid']}]": row["_vid"]
                for _, row in df_sorted.iterrows()
            }
            st.session_state.setdefault("transcript_selection", list(titles_map.keys())[:50])
            select_keys = st.multiselect("대본을 받을 영상 선택", options=list(titles_map.keys()), key="transcript_selection")
            lang_pref = st.text_input("우선 언어(예: ko, en, ko-KR)", value=st.session_state.get("lang_pref","ko"), key="lang_pref")

            col_srt, col_zip = st.columns([0.5, 0.5])
            with col_srt:
                st.write("**개별 SRT 다운로드**")
                if select_keys:
                    for label in select_keys[:30]:
                        vid = titles_map[label]
                        srt = fetch_transcript_srt(vid, lang_pref=lang_pref)
                        if srt:
                            fn = _safe_filename(label)[:100] + ".srt"
                            st.download_button("⬇️ " + fn, data=srt.encode("utf-8"),
                                               file_name=fn, mime="application/x-subrip", key=f"srt_{vid}")
                        else:
                            st.caption(f"• `{label}` : 공개 자막 없음 / 가져오기 실패")
                else:
                    st.caption("선택된 항목이 없습니다.")
            with col_zip:
                st.write("**선택 항목 ZIP 일괄 다운로드**")
                if select_keys:
                    labels_tuple = tuple(select_keys)
                    vids_tuple = tuple(titles_map[k] for k in select_keys)
                    zip_bytes = build_transcripts_zip_cached(vids_tuple, labels_tuple, lang_pref)
                    st.download_button("⬇️ transcripts_selected.zip", data=zip_bytes,
                                       file_name="transcripts_selected.zip", mime="application/zip",
                                       key="zip_selected")
                else:
                    st.caption("선택된 항목이 없습니다.")

st.markdown("---")
st.caption("입력 탭에서 바로 다국어 키워드를 미리보고, 설정 탭에서는 국가 범위만 고르면 됩니다. 제목·설명·태그 기반 엄격 필터도 유지됩니다.")


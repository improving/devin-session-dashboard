#!/usr/bin/env python3
"""Minimal local dashboard for Devin Desktop's cached SQLite state."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import struct
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEFAULT_COST_PER_ACU = 1.25

APP_TITLE = "Devin Session Dashboard"


def candidate_db_paths() -> list[Path]:
    """Return Devin state database locations in platform-specific priority order."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        local_appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Devin/User/globalStorage/state.vscdb")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Devin/User/globalStorage/state.vscdb")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library/Application Support/Devin/User/globalStorage/state.vscdb")
    else:
        candidates.append(Path.home() / ".config/Devin/User/globalStorage/state.vscdb")
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            candidates.append(Path(config_home) / "Devin/User/globalStorage/state.vscdb")
    return list(dict.fromkeys(candidates))


def resolve_db_path(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    for path in candidate_db_paths():
        if path.exists():
            return path
    searched = "\n".join(f"  - {path}" for path in candidate_db_paths())
    raise FileNotFoundError(f"Devin database not found. Searched:\n{searched}")


def candidate_cli_db_paths() -> list[Path]:
    """Return Devin CLI session database locations in platform-specific priority order."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "devin/cli/sessions.db")
        if appdata:
            candidates.append(Path(appdata) / "devin/cli/sessions.db")
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        if data_home:
            candidates.append(Path(data_home) / "devin/cli/sessions.db")
        candidates.append(Path.home() / ".local/share/devin/cli/sessions.db")
    return list(dict.fromkeys(candidates))


def resolve_cli_db_path(override: str | None) -> Path | None:
    if override:
        path = Path(override).expanduser().resolve()
        return path if path.exists() else None
    for path in candidate_cli_db_paths():
        if path.exists():
            return path
    return None


def read_session_stats(db_path: Path | None, window_start: str | None = None, window_end: str | None = None) -> dict[str, dict[str, int | float | None]]:
    """Read deduplicated ACU, token usage, active runtime, and generation time per CLI session."""
    if db_path is None:
        return {}
    uri = f"file:{quote(str(db_path), safe="/:\\")}?mode=ro"
    query = """
        WITH messages AS (
            SELECT session_id,
                   MAX(json_extract(chat_message, '$.role')) AS role,
                   MAX(json_extract(chat_message, '$.metadata.created_at')) AS created_at,
                   MAX(json_extract(chat_message, '$.metadata.committed_acu_cost')) AS acu,
                   MAX(json_extract(chat_message, '$.metadata.metrics.input_tokens')) AS input_tokens,
                   MAX(json_extract(chat_message, '$.metadata.metrics.output_tokens')) AS output_tokens,
                   MAX(json_extract(chat_message, '$.metadata.metrics.total_time_ms')) AS gen_ms
            FROM message_nodes
            GROUP BY session_id, json_extract(chat_message, '$.message_id')
        ), ordered AS (
            SELECT session_id, role, acu, input_tokens, output_tokens, gen_ms, created_at,
                   strftime('%s', created_at) AS timestamp,
                   LAG(strftime('%s', created_at)) OVER (
                       PARTITION BY session_id ORDER BY created_at
                   ) AS previous_timestamp
            FROM messages
        )
        SELECT session_id,
               SUM(acu),
               SUM(input_tokens),
               SUM(output_tokens),
               SUM(gen_ms),
               SUM(CASE
                   WHEN previous_timestamp IS NULL OR timestamp IS NULL OR role = 'user' THEN 0
                   ELSE MIN(MAX(timestamp - previous_timestamp, 0), 3600) * 1000
               END),
               SUM(CASE WHEN created_at >= ? AND created_at < ? THEN acu ELSE 0 END)
        FROM ordered
        GROUP BY session_id
    """
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(query, (window_start or "", window_end or "9999")).fetchall()
            try:
                models = read_session_models(conn)
            except sqlite3.Error:
                models = {}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    stats: dict[str, dict[str, Any]] = {session_id: {"model": model} for session_id, model in models.items()}
    for session_id, acu, input_tokens, output_tokens, gen_ms, runtime_ms, window_acu in rows:
        stats.setdefault(str(session_id), {}).update({
            "acu": float(acu) if isinstance(acu, (int, float)) else None,
            "windowAcu": float(window_acu) if isinstance(window_acu, (int, float)) else None,
            "inputTokens": int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
            "outputTokens": int(output_tokens) if isinstance(output_tokens, (int, float)) else None,
            "runtimeMs": float(runtime_ms) if isinstance(runtime_ms, (int, float)) else None,
            "genMs": float(gen_ms) if isinstance(gen_ms, (int, float)) else None,
        })
    return stats


def read_session_models(conn: sqlite3.Connection) -> dict[str, str]:
    """Return each session's configured model, overridden by the last observed generation model."""
    models: dict[str, str] = {}
    for session_id, model in conn.execute("SELECT id, model FROM sessions WHERE model IS NOT NULL"):
        if isinstance(model, str) and model.strip():
            models[str(session_id)] = model.strip()
    rows = conn.execute("""
        SELECT session_id,
               json_extract(chat_message, '$.metadata.generation_model') AS model,
               MAX(row_id)
        FROM message_nodes
        WHERE json_extract(chat_message, '$.metadata.generation_model') IS NOT NULL
        GROUP BY session_id
    """).fetchall()
    for session_id, model, _ in rows:
        if isinstance(model, str) and model.strip():
            models[str(session_id)] = model.strip()
    return models


def safe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def iso_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def epoch_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def session_id_from_key(key: str) -> str:
    prefix = "windsurf.acp.sessioninfo.session."
    return key[len(prefix):] if key.startswith(prefix) else key


def extract_provider(session_id: str, provider_id: Any) -> str:
    if provider_id:
        return str(provider_id)
    parts = session_id.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "acp" else "unknown"


def fetch_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[tuple[str, str]]:
    return [(str(key), value) for key, value in conn.execute(query, params).fetchall()]


def workspace_name(workspace: Any, cwd: str) -> str:
    if isinstance(workspace, dict):
        label = workspace.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
        folders = workspace.get("folders")
        if isinstance(folders, list) and folders:
            workspace = folders[0]
    if isinstance(workspace, str) and workspace.strip():
        return Path(workspace).name or workspace
    return Path(cwd).name if cwd else ""


def billing_window_bounds(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return the plan billing window as ISO strings comparable to message created_at values."""
    def fmt(value: Any) -> str | None:
        ms = epoch_ms(value)
        if ms is None:
            return None
        try:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    return fmt(raw.get("startTimestamp", raw.get("planStart"))), fmt(raw.get("endTimestamp", raw.get("planEnd")))


def read_state(db_path: Path, cli_db_path: Path | None = None) -> dict[str, Any]:
    uri = f"file:{quote(str(db_path), safe="/:\\")}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        session_rows = fetch_rows(conn, "SELECT key, value FROM ItemTable WHERE key LIKE 'windsurf.acp.sessioninfo.%'")
        start_rows = fetch_rows(conn, "SELECT key, value FROM ItemTable WHERE key LIKE 'windsurf.acp.session/session_start_time/%'")
        count_rows = fetch_rows(conn, "SELECT key, value FROM ItemTable WHERE key LIKE 'windsurf.acp.session/userMessageCount/%'")
        workspace_rows = fetch_rows(conn, "SELECT key, value FROM ItemTable WHERE key LIKE 'windsurfSpace.sessionWorkspace/%'")
        plan_rows = fetch_rows(conn, "SELECT key, value FROM ItemTable WHERE key LIKE 'windsurf.reactSettings.cachedPlanInfoData%' OR key = 'windsurf.settings.cachedPlanInfo'")
        auth_rows = fetch_rows(conn, "SELECT key, value FROM ItemTable WHERE key = 'windsurfAuthStatus'")
    finally:
        conn.close()

    starts = {key.split("windsurf.acp.session/session_start_time/", 1)[-1]: safe_json(value) for key, value in start_rows}
    counts = {key.split("windsurf.acp.session/userMessageCount/", 1)[-1]: safe_json(value) for key, value in count_rows}
    workspaces = {key.split("windsurfSpace.sessionWorkspace/", 1)[-1]: safe_json(value) for key, value in workspace_rows}
    plan_value = next((safe_json(value) for key, value in plan_rows if key.startswith("windsurf.reactSettings.cachedPlanInfoData")), None)
    if not isinstance(plan_value, dict):
        plan_value = next((safe_json(value) for key, value in plan_rows if key == "windsurf.settings.cachedPlanInfo"), {})
    plan = normalize_plan(plan_value)
    auth = next((safe_json(value) for _, value in auth_rows), None)
    if isinstance(auth, dict):
        doubles = user_status_plan_doubles(auth.get("userStatusProtoBinaryBase64"))
        if isinstance(doubles.get(20), (int, float)) and doubles[20] > 0:
            plan["limit"] = doubles[20]
        if isinstance(doubles.get(19), (int, float)) and doubles[19] >= 0:
            plan["consumed"] = doubles[19]
    window_start, window_end = billing_window_bounds(plan_value if isinstance(plan_value, dict) else {})
    stats_by_session = read_session_stats(cli_db_path, window_start, window_end)
    plan["sessionAcu"] = sum(s["windowAcu"] for s in stats_by_session.values() if isinstance(s.get("windowAcu"), (int, float)))
    sessions: list[dict[str, Any]] = []

    for key, raw_value in session_rows:
        value = safe_json(raw_value)
        if not isinstance(value, dict):
            continue
        info = value.get("info") if isinstance(value.get("info"), dict) else {}
        meta = info.get("_meta") if isinstance(info.get("_meta"), dict) else {}
        session_id = str(info.get("sessionId") or session_id_from_key(key))
        workspace = workspaces.get(session_id)
        if not isinstance(workspace, dict):
            workspace = {}
        created = meta.get("cognition.ai/createdAt") or starts.get(session_id)
        user_messages = counts.get(session_id, meta.get("cognition.ai/userMessageCount"))
        slug = session_id.rsplit("/", 1)[-1]
        stats = stats_by_session.get(slug, {})
        sessions.append({
            "id": session_id,
            "slug": slug,
            "provider": extract_provider(session_id, value.get("providerId")),
            "title": str(info.get("title") or "(untitled)"),
            "cwd": str(info.get("cwd") or ""),
            "workspace": workspace_name(workspace, str(info.get("cwd") or "")),
            "model": stats.get("model"),
            "createdAt": iso_value(created),
            "updatedAt": iso_value(info.get("updatedAt")),
            "messages": int(user_messages) if isinstance(user_messages, (int, float)) else 0,
            "acu": stats.get("acu"),
            "runtimeMs": stats.get("runtimeMs"),
            "genMs": stats.get("genMs"),
            "inputTokens": stats.get("inputTokens"),
            "outputTokens": stats.get("outputTokens"),
        })

    total_sessions = len(sessions)
    sessions = [s for s in sessions if s["title"] != "(untitled)" or s["messages"] > 0]
    sessions.sort(key=lambda item: epoch_ms(item["updatedAt"]) or 0, reverse=True)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dbPath": str(db_path),
        "cliDbPath": str(cli_db_path) if cli_db_path else None,
        "hiddenEmpty": total_sessions - len(sessions),
        "plan": plan,
        "sessions": sessions,
    }


def normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    consumed = raw.get("acuConsumed")
    limit = raw.get("acuLimit")
    if consumed is None:
        consumed = usage.get("usedFlexCredits", usage.get("usedMessages", 0))
    if limit is None:
        limit = usage.get("flexCredits", usage.get("messages", -1))
    return {
        "name": raw.get("planName", "Unknown plan"),
        "billingStrategy": raw.get("billingStrategy", ""),
        "consumed": number_or_zero(consumed),
        "limit": number_or_zero(limit, -1),
        "overageBalanceMicros": number_or_zero(raw.get("overageBalanceMicros")),
        "start": iso_value(raw.get("startTimestamp", raw.get("planStart"))),
        "end": iso_value(raw.get("endTimestamp", raw.get("planEnd"))),
        "account": raw.get("accountIdentityText", ""),
    }


def number_or_zero(value: Any, default: float = 0) -> int | float:
    return value if isinstance(value, (int, float)) else default


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while i < len(buf):
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, i


def _fixed64_fields(buf: bytes) -> dict[int, float]:
    """Collect fixed64 fields of a protobuf message as doubles."""
    out: dict[int, float] = {}
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            _, i = _varint(buf, i)
        elif wire == 1:
            out[field] = struct.unpack("<d", buf[i:i + 8])[0]
            i += 8
        elif wire == 5:
            i += 4
        elif wire == 2:
            length, i = _varint(buf, i)
            i += length
        else:
            break
    return out


def user_status_plan_doubles(b64: Any) -> dict[int, float]:
    """Extract fixed64 fields (19: ACU consumed, 20: ACU limit) from the cached userStatus plan proto."""
    if not isinstance(b64, str):
        return {}
    try:
        buf = base64.b64decode(b64)
    except (ValueError, TypeError):
        return {}
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            _, i = _varint(buf, i)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        elif wire == 2:
            length, i = _varint(buf, i)
            if field == 13:
                return _fixed64_fields(buf[i:i + length])
            i += length
        else:
            break
    return {}


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Devin Session Dashboard</title><style>
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202124;background:#f6f7f9;line-height:1.4}*{box-sizing:border-box}body{margin:0}.shell{max-width:1780px;margin:auto;padding:32px 24px 48px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:28px}h1{font-size:25px;letter-spacing:-.02em;margin:0 0 5px}h2{font-size:16px;margin:0 0 16px}.muted,.meta{color:#6b7280;font-size:13px}.actions{display:flex;gap:9px;align-items:center}button,.input{font:inherit;border:1px solid #d5d9e0;border-radius:7px;background:white;padding:8px 11px;color:inherit}button{cursor:pointer}button:hover{background:#f0f2f5}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}.card,.panel{background:white;border:1px solid #e1e4e9;border-radius:10px;box-shadow:0 1px 2px #00000008}.card{padding:17px}.card-label{text-transform:uppercase;letter-spacing:.07em;color:#727985;font-size:11px;font-weight:650}.metric{font-size:23px;font-weight:650;margin:8px 0 3px}.progress{height:7px;background:#e8ebef;border-radius:9px;overflow:hidden;margin:11px 0 6px}.progress>i{display:block;height:100%;background:#5269d9;border-radius:9px}.panel{padding:20px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:16px}.controls{display:flex;gap:9px;flex-wrap:wrap}.search{min-width:260px}.table-wrap{overflow:auto;margin:0 -20px}.session-table{width:100%;border-collapse:collapse;min-width:1150px}.session-table th,.session-table td{text-align:left;padding:11px 20px;border-top:1px solid #edf0f3;font-size:13px;white-space:nowrap}.session-table th{color:#6b7280;font-weight:600;font-size:12px;cursor:pointer}.session-table th:hover{color:#202124}.title{max-width:390px;overflow:hidden;text-overflow:ellipsis}.workspace{max-width:250px;overflow:hidden;text-overflow:ellipsis}.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:#eef1ff;color:#4054b2;font-size:11px}.footer{padding-top:13px}.settings{display:flex;align-items:center;gap:7px;font-size:13px}.price{width:90px;padding:6px 8px}.error{padding:20px;color:#a12727;background:#fff0f0;border:1px solid #f2c8c8;border-radius:10px;white-space:pre-wrap}.nowrap{white-space:nowrap}.share-btn{border:0;background:none;padding:0;width:26px;height:26px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;color:#9aa1ab;cursor:pointer}.share-btn:hover{background:#f0f2f5;color:#202124}.session-table th.no-sort{cursor:default}.share-menu{position:fixed;background:white;border:1px solid #d5d9e0;border-radius:8px;box-shadow:0 6px 20px #0000001f;z-index:1000;min-width:170px;padding:5px;font-size:13px}.share-menu button{display:block;width:100%;text-align:left;border:0;background:none;border-radius:6px;padding:7px 11px;font:inherit;color:inherit;cursor:pointer}.share-menu button:hover{background:#f0f2f5}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}header,.panel-head{align-items:flex-start;flex-direction:column}}@media(max-width:500px){.shell{padding:22px 14px}.grid{grid-template-columns:1fr}.actions{width:100%}.actions button{flex:1}}
</style></head><body><main class="shell"><header><div><h1>Devin Session Dashboard</h1><div class="meta" id="source">Loading local state…</div></div><div class="actions"><span class="meta" id="asof"></span><button id="refresh" title="Refresh" aria-label="Refresh" style="display:inline-flex;align-items:center;justify-content:center"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89"/><polyline points="13.5 1.5 13.5 4.5 10.5 4.5"/></svg></button><label class="settings"><input id="auto" type="checkbox"> Auto-refresh</label></div></header><div id="app"><div class="muted">Loading…</div></div></main><script>
const DEFAULT_COST_PER_ACU=__DEFAULT_COST_PER_ACU__;
const state={data:null,sort:'updatedAt',desc:true};const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=v=>v?new Date(v).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}):'—';const dte=v=>v?new Date(v).toLocaleDateString([], {dateStyle:'medium'}):'—';const dmy=v=>{if(!v)return'—';const d=new Date(v);return`${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')}/${String(d.getFullYear()).slice(-2)}`};const fmt=v=>v==null?'—':Number(v).toLocaleString();const dur=v=>{if(v==null)return'—';const s=Math.round(Number(v)/1000);if(s<60)return`${s}s`;const m=Math.floor(s/60);if(m<60)return`${m}m ${s%60}s`;return`${(s/3600).toFixed(1)}h`};const pct=(v,total)=>total>0?Math.min(100,Math.max(0,v/total*100)):0;
function render(){closeShareMenu();const d=state.data,p=d.plan||{},serverAcu=Number(p.consumed)||0,localAcu=Number(p.sessionAcu)||0,consumed=Math.max(serverAcu,localAcu),gap=Math.max(0,serverAcu-localAcu);const price=DEFAULT_COST_PER_ACU,limit=Number(p.limit),acuPct=pct(consumed,limit),est=(consumed*price).toFixed(2);const now=new Date(),monthSessions=(d.sessions||[]).filter(s=>{const t=s.createdAt?new Date(s.createdAt):null;return t&&t.getMonth()===now.getMonth()&&t.getFullYear()===now.getFullYear()}),tokIn=monthSessions.reduce((a,s)=>a+(Number(s.inputTokens)||0),0),tokOut=monthSessions.reduce((a,s)=>a+(Number(s.outputTokens)||0),0),modelCounts={},modelTok={},modelAcu={};monthSessions.forEach(s=>{if(s.model){modelCounts[s.model]=(modelCounts[s.model]||0)+1;modelTok[s.model]=(modelTok[s.model]||0)+(Number(s.inputTokens)||0)+(Number(s.outputTokens)||0);modelAcu[s.model]=(modelAcu[s.model]||0)+(Number(s.acu)||0)}});const top=o=>Object.entries(o).sort((a,b)=>b[1]-a[1])[0],topTok=top(modelTok),topAcu=top(modelAcu),topCnt=top(modelCounts);
$('source').textContent=`Monthly Metrics: ${dte(p.start)} — ${dte(p.end)}`;$('asof').textContent=`Last update: ${date(d.generatedAt)}`;let rows=d.sessions||[];const prevSearch=$('search'),raw=prevSearch?.value||'',q=raw.toLowerCase(),focused=prevSearch&&document.activeElement===prevSearch,caret=prevSearch?.selectionStart;if(q)rows=rows.filter(s=>[s.title,s.cwd,s.id,s.provider,s.workspace,s.model].join(' ').toLowerCase().includes(q));rows.sort((a,b)=>{const v=s=>state.sort==='cost'?(s.acu==null?'':s.acu*price):s[state.sort];let x=v(a)??'',y=v(b)??'';if(state.sort.includes('At')){x=new Date(x||0).getTime();y=new Date(y||0).getTime()}else if(typeof x==='string'){x=x.toLowerCase();y=String(y).toLowerCase()}return (x<y?-1:x>y?1:0)*(state.desc?-1:1)});
$('app').innerHTML=`<section class="grid"><article class="card"><div class="card-label">Plan</div><div class="metric">${esc(p.name)}</div><div class="card-label" style="margin-top:14px">Estimated cost</div><div class="metric">$${est}</div><div class="muted">$${price.toFixed(2)} / ACU${limit>0?' · $'+(limit*price).toFixed(2)+' per month':''}</div></article><article class="card"><div class="card-label">ACU consumed</div><div class="metric">${consumed.toFixed(2)} <span class="muted">/ ${limit>0?limit.toFixed(2):'∞'}</span></div><div class="progress"><i style="width:${acuPct}%"></i></div><div class="muted">${limit>0?acuPct.toFixed(1)+'% of monthly limit':'Unlimited'}</div><div class="muted">${serverAcu.toFixed(2)} server · ${localAcu.toFixed(2)} local · ${gap.toFixed(2)} other</div></article><article class="card"><div class="card-label">Sessions this month</div><div class="metric">${fmt(monthSessions.length)}</div><div class="muted">Tokens:</div><div class="muted">IN ${fmt(tokIn)} · OUT ${fmt(tokOut)}</div></article><article class="card"><div class="card-label">Model metrics</div><div class="metric">${fmt(Object.keys(modelCounts).length)}</div><div class="muted">Top by tokens: ${topTok?esc(topTok[0])+' · '+fmt(topTok[1]):'—'}</div><div class="muted">Top by ACU: ${topAcu?esc(topAcu[0])+' · '+topAcu[1].toFixed(2):'—'}</div><div class="muted">Top by count: ${topCnt?esc(topCnt[0])+' · '+fmt(topCnt[1]):'—'}</div></article></section><section class="panel"><div class="panel-head"><div><h2>Sessions <span class="muted">(${rows.length!==d.sessions.length?`${rows.length} of `:''}${d.sessions.length} Total)</span></h2></div><div class="controls"><input class="input search" id="search" placeholder="Search title, workspace, provider…" value="${esc(raw)}"></div></div><div class="table-wrap"><table class="session-table"><thead><tr><th class="no-sort"></th>${[['updatedAt','Last updated'],['workspace','Workspace'],['title','Title'],['model','Model'],['acu','ACU'],['cost','Est. cost'],['inputTokens','Tokens in'],['outputTokens','Tokens out'],['genMs','Gen. time'],['runtimeMs','Runtime'],['createdAt','Created'],['messages','Messages']].map(([k,l])=>`<th data-sort="${k}">${l}${state.sort===k?' '+(state.desc?'↓':'↑'):''}</th>`).join('')}</tr></thead><tbody>${rows.map(s=>`<tr><td class="nowrap"><button class="share-btn" data-id="${esc(s.id)}" title="Share" aria-label="Share session">${SHARE_ICON_SVG}</button></td><td class="nowrap">${date(s.updatedAt)}</td><td class="workspace" title="${esc(s.cwd)}">${esc(s.workspace||s.cwd||'—')}</td><td class="title" title="${esc(s.title)}">${esc(s.title)}</td><td class="nowrap">${esc(s.model||'—')}</td><td class="nowrap">${s.acu==null?'—':s.acu.toFixed(3)}</td><td class="nowrap">${s.acu==null?'—':'$'+(s.acu*price).toFixed(2)}</td><td class="nowrap">${fmt(s.inputTokens)}</td><td class="nowrap">${fmt(s.outputTokens)}</td><td class="nowrap">${dur(s.genMs)}</td><td class="nowrap">${dur(s.runtimeMs)}</td><td class="nowrap">${dmy(s.createdAt)}</td><td>${s.messages}</td></tr>`).join('')||'<tr><td colspan="13" class="muted">No sessions match this search.</td></tr>'}</tbody></table></div><div class="footer muted">${d.sessions.length} sessions loaded from local metadata.${d.hiddenEmpty?' '+d.hiddenEmpty+' empty sessions (no title, no messages) hidden.':''}${d.cliDbPath?' Per-session ACU, token usage, runtime, and generation time read from '+d.cliDbPath+'.':' Per-session ACU, token usage, and timing unavailable (Devin CLI session store not found).'}</div></section>`;
$('search').addEventListener('input',render);document.querySelectorAll('[data-sort]').forEach(el=>el.addEventListener('click',()=>{if(state.sort===el.dataset.sort)state.desc=!state.desc;else{state.sort=el.dataset.sort;state.desc=true}render()}));const search=$('search');if(search&&focused){search.focus();search.setSelectionRange(caret??raw.length,caret??raw.length)}}
const SHARE_ICON_SVG='<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.6v7.9"/><polyline points="4.9 4.8 8 1.6 11.1 4.8"/><path d="M4.6 6.6H3.6a1 1 0 0 0-1 1v5.8a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1V7.6a1 1 0 0 0-1-1h-1"/></svg>';
let shareMenu=null;
function closeShareMenu(){if(!shareMenu)return;const m=shareMenu;shareMenu=null;m.el.remove();document.removeEventListener('click',m.onDoc,true);document.removeEventListener('keydown',m.onKey)}
function shareFileDate(s){const v=s.createdAt||s.updatedAt;const d=v?new Date(v):new Date();if(isNaN(d))return new Date().toISOString().slice(0,10);const p=n=>String(n).padStart(2,'0');return`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`}
function shareSanitize(v){return String(v||'').replace(/[\\/:*?"<>|\u0000-\u001f]/g,'-').replace(/\s+/g,' ').replace(/^[.\s]+/,'').replace(/[.\s]+$/,'').slice(0,80).trim()}
function shareFilename(s){let base=shareSanitize(s.title);if(!base||base==='(untitled)')base=shareSanitize(s.slug)||'session';return`${base} ${shareFileDate(s)}.md`}
function estCost(s){return s.acu==null?'—':'$'+(s.acu*DEFAULT_COST_PER_ACU).toFixed(2)}
function sessionMarkdown(s){const pipe=v=>String(v??'').replace(/\|/g,'\\|');const val=v=>pipe(v==null||v===''?'—':v);return['# '+pipe(s.title||'(untitled)'),'','| Field | Value |','| --- | --- |','| Session ID | '+val(s.id)+' |','| Provider | '+val(s.provider)+' |','| Workspace | '+val(s.workspace)+' |','| Directory | '+val(s.cwd)+' |','| Model | '+val(s.model)+' |','| User messages | '+val(s.messages)+' |','| ACU consumed | '+(s.acu==null?'—':s.acu.toFixed(3))+' |','| Est. cost | '+estCost(s)+' |','| Tokens in | '+fmt(s.inputTokens)+' |','| Tokens out | '+fmt(s.outputTokens)+' |','| Gen. time | '+dur(s.genMs)+' |','| Runtime | '+dur(s.runtimeMs)+' |','| Created | '+date(s.createdAt)+' |','| Last updated | '+date(s.updatedAt)+' |','','Estimated at $'+DEFAULT_COST_PER_ACU.toFixed(2)+' / ACU.'].join('\n')}
async function copySessionJson(s){const text=JSON.stringify({...s,costPerAcu:DEFAULT_COST_PER_ACU,estimatedCostUsd:s.acu==null?null:Number((s.acu*DEFAULT_COST_PER_ACU).toFixed(2))},null,2);try{await navigator.clipboard.writeText(text);return}catch(e){}const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{document.execCommand('copy')}catch(e){}ta.remove()}
function downloadSessionMarkdown(s){const blob=new Blob([sessionMarkdown(s)],{type:'text/markdown'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=shareFilename(s);document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}
function openShareMenu(btn,s){closeShareMenu();const el=document.createElement('div');el.className='share-menu';const copy=document.createElement('button');copy.type='button';copy.textContent='Copy JSON';const dl=document.createElement('button');dl.type='button';dl.textContent='Download .md file';el.append(copy,dl);document.body.appendChild(el);const r=btn.getBoundingClientRect(),w=el.offsetWidth,h=el.offsetHeight;let top=r.bottom+6,left=r.right-w;if(top+h>window.innerHeight-8&&r.top-h-6>=8)top=r.top-h-6;left=Math.max(8,Math.min(left,window.innerWidth-w-8));el.style.top=top+'px';el.style.left=left+'px';const onDoc=e=>{if(!el.contains(e.target)&&e.target!==btn)closeShareMenu()};const onKey=e=>{if(e.key==='Escape')closeShareMenu()};document.addEventListener('click',onDoc,true);document.addEventListener('keydown',onKey);shareMenu={el,onDoc,onKey};copy.addEventListener('click',async()=>{await copySessionJson(s);copy.textContent='Copied!';setTimeout(closeShareMenu,1000)});dl.addEventListener('click',()=>{downloadSessionMarkdown(s);closeShareMenu()})}
document.addEventListener('click',e=>{const btn=e.target.closest&&e.target.closest('.share-btn');if(!btn)return;const s=(state.data&&state.data.sessions||[]).find(x=>x.id===btn.dataset.id);if(s)openShareMenu(btn,s)});

async function load(){try{const r=await fetch('/api/data',{cache:'no-store'});if(!r.ok)throw new Error(await r.text());state.data=await r.json();render()}catch(e){$('app').innerHTML=`<div class="error">Unable to load dashboard data.\n\n${esc(e.message)}</div>`}}$('refresh').addEventListener('click',load);$('auto').addEventListener('change',e=>{if(e.target.checked)window.refreshTimer=setInterval(load,60000);else clearInterval(window.refreshTimer)});load();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    db_path: Path
    cli_db_path: Path | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path == "/index.html":
            html = HTML.replace("__DEFAULT_COST_PER_ACU__", str(DEFAULT_COST_PER_ACU))
            self.send_text(html, "text/html; charset=utf-8")
            return
        if self.path == "/api/data":
            try:
                payload = json.dumps(read_state(self.db_path, self.cli_db_path), ensure_ascii=False).encode()
            except Exception as exc:  # surface a safe local diagnostic to the UI
                payload = json.dumps({"error": str(exc)}).encode()
                self.send_bytes(payload, "application/json", 500)
                return
            self.send_bytes(payload, "application/json")
            return
        self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def send_text(self, text: str, content_type: str) -> None:
        self.send_bytes(text.encode("utf-8"), content_type)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local Devin session and quota dashboard.")
    parser.add_argument("--db", help="Path to Devin state.vscdb (auto-detected by default)")
    parser.add_argument("--cli-db", help="Path to Devin CLI sessions.db (auto-detected by default)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Starting port (default: 8787)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically")
    args = parser.parse_args()
    try:
        db_path = resolve_db_path(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    Handler.db_path = db_path
    Handler.cli_db_path = resolve_cli_db_path(args.cli_db)
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"Could not bind a port from {args.port} to {args.port + 19}.", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{server.server_port}/"
    print(f"{APP_TITLE} running at {url}")
    print(f"Reading: {db_path}")
    if not args.no_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

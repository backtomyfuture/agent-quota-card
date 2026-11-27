#!/usr/bin/env python3
"""Unified quota collector for popular AI coding agents & CLIs."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


HOME = Path.home()
HTTP_TIMEOUT = 12


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _curl_escape(val: str) -> str:
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "") + '"'


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> tuple[int, dict[str, Any] | str]:
    payload_str: str | None = None
    req_headers = dict(headers or {})
    if json_body is not None:
        payload_str = json.dumps(json_body)
        req_headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        payload_str = urllib.parse.urlencode(form_body)
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    curl_bin = "/usr/bin/curl" if os.path.exists("/usr/bin/curl") else shutil.which("curl")
    if curl_bin:
        cfg_lines = [
            f"url = {_curl_escape(url)}",
            f"request = {_curl_escape(method)}",
            f"max-time = {int(timeout)}",
            'noproxy = "127.0.0.1,localhost"',
            'write-out = "\\n__HTTP_CODE__:%{http_code}"',
            "silent",
            "show-error",
        ]
        for k, v in req_headers.items():
            cfg_lines.append(f"header = {_curl_escape(f'{k}: {v}')}")
        if payload_str is not None:
            cfg_lines.append(f"data-raw = {_curl_escape(payload_str)}")

        res = subprocess.run(
            [curl_bin, "-K", "-"],
            input="\n".join(cfg_lines),
            capture_output=True,
            text=True,
            timeout=timeout + 3,
        )
        if res.returncode in (35, 56):
            res = subprocess.run(
                [curl_bin, "--http1.1", "-K", "-"],
                input="\n".join(cfg_lines),
                capture_output=True,
                text=True,
                timeout=timeout + 3,
            )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or f"curl exited {res.returncode}").strip())
        out = res.stdout
        marker = "\n__HTTP_CODE__:"
        if marker in out:
            body_text, code_str = out.rsplit(marker, 1)
            code = int(code_str.strip())
        else:
            body_text, code = out, 200
        try:
            return code, json.loads(body_text)
        except json.JSONDecodeError:
            return code, body_text

    data = payload_str.encode("utf-8") if payload_str is not None else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _epoch_to_iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _keychain_has(service: str, account: str | None = None) -> bool:
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    return res.returncode == 0


def _has_bin(name: str) -> bool:
    return bool(
        shutil.which(name)
        or os.path.exists(f"/opt/homebrew/bin/{name}")
        or os.path.exists(f"/usr/local/bin/{name}")
        or (HOME / ".local" / "bin" / name).exists()
    )


def _err_card(pid: str, name: str, cli: str, msg: str) -> dict[str, Any]:
    return {
        "id": pid,
        "name": name,
        "cli": cli,
        "status": "error",
        "subtitle": None,
        "error": msg,
        "items": [],
    }


def _ok_card(pid: str, name: str, cli: str, subtitle: str | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": pid,
        "name": name,
        "cli": cli,
        "status": "ok",
        "subtitle": subtitle,
        "error": None,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 1. Antigravity (agy)
# ---------------------------------------------------------------------------

def _agy_read_keychain() -> dict[str, Any]:
    res = subprocess.run(
        ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if res.returncode != 0:
        raise RuntimeError("未在钥匙串找到 Antigravity 登录凭据")
    raw = res.stdout.strip()
    if raw.startswith("go-keyring-base64:"):
        raw = base64.b64decode(raw.split(":", 1)[1]).decode("utf-8")
    return json.loads(raw)


def _agy_refresh_token(refresh_token: str) -> str | None:
    cid = os.environ.get("AGY_OAUTH_CLIENT_ID")
    secret = os.environ.get("AGY_OAUTH_CLIENT_SECRET")
    if not cid or not secret:
        return None
    status, body = http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        form_body={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if status == 200 and isinstance(body, dict) and body.get("access_token"):
        return body["access_token"]
    return None


def _agy_fetch_from_local_ls() -> list[dict[str, Any]] | None:
    try:
        lsof_bin = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else (shutil.which("lsof") or "lsof")
        res = subprocess.run(
            [lsof_bin, "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-c", "antigravi"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        ports: list[int] = []
        for line in res.stdout.splitlines():
            m = re.search(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", line)
            if m:
                ports.append(int(m.group(1)))
        for port in ports:
            url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            status, body = http_json(url, method="POST", json_body={}, timeout=3)
            if status != 200 or not isinstance(body, dict):
                continue
            groups = (body.get("response") or {}).get("groups") or []
            if not groups:
                continue
            items: list[dict[str, Any]] = []
            for grp in groups:
                gname = grp.get("displayName") or ""
                short_grp = (
                    "Gemini" if "Gemini" in gname
                    else ("Claude/GPT" if "Claude" in gname or "GPT" in gname else gname)
                )
                buckets = grp.get("buckets") or []
                active_buckets = [b for b in buckets if not b.get("disabled")] or buckets
                active_buckets.sort(key=lambda b: (0 if b.get("window") == "5h" else 1))
                for b in active_buckets:
                    if "remainingFraction" not in b:
                        continue
                    win = b.get("window") or b.get("bucketId") or ""
                    win_label = "5h" if win == "5h" else ("Weekly" if win == "weekly" else win)
                    rem_pct = round(float(b["remainingFraction"]) * 100.0, 1)
                    items.append(
                        {
                            "label": f"{short_grp} {win_label}".strip(),
                            "remaining_percent": rem_pct,
                            "used_percent": round(100.0 - rem_pct, 1),
                            "reset_at": b.get("resetTime"),
                            "detail": None,
                        }
                    )
            if items:
                return items
    except Exception:
        return None
    return None


def detect_agy() -> bool:
    return bool(shutil.which("agy") or os.path.exists("/opt/homebrew/bin/agy") or _keychain_has("gemini", "antigravity"))


def fetch_agy() -> dict[str, Any]:
    try:
        creds = _agy_read_keychain()
        email = creds.get("id_token_email") or "OAuth"

        local_items = _agy_fetch_from_local_ls()
        if local_items:
            return _ok_card("agy", "Antigravity", "agy", email, local_items)

        tok_obj = creds.get("token") or {}
        access_token = tok_obj.get("access_token")
        refresh_token = tok_obj.get("refresh_token")
        if not access_token:
            raise RuntimeError("Antigravity token 为空，请运行 agy 登录")

        def call_cloud(ep: str, tok: str) -> tuple[int, Any]:
            return http_json(
                f"https://daily-cloudcode-pa.googleapis.com/{ep}",
                method="POST",
                headers={"Authorization": f"Bearer {tok}", "User-Agent": "antigravity/1.0"},
                json_body={},
            )

        status, body = call_cloud("v1internal:fetchAvailableModels", access_token)
        if status == 401 and refresh_token:
            new_tok = _agy_refresh_token(refresh_token)
            if new_tok:
                access_token = new_tok
                status, body = call_cloud("v1internal:fetchAvailableModels", access_token)

        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        items: list[dict[str, Any]] = []
        s_sum, b_sum = call_cloud("v1internal:retrieveUserQuotaSummary", access_token)
        seen_claude = False
        if s_sum == 200 and isinstance(b_sum, dict):
            for grp in b_sum.get("groups") or []:
                gname = grp.get("displayName") or ""
                short_grp = (
                    "Gemini" if "Gemini" in gname
                    else ("Claude/GPT" if "Claude" in gname or "GPT" in gname else gname)
                )
                if "Claude" in gname or "GPT" in gname:
                    seen_claude = True
                for b in grp.get("buckets") or []:
                    if b.get("disabled") or "remainingFraction" not in b:
                        continue
                    win = b.get("window") or ""
                    win_label = "5h" if win == "5h" else ("Weekly" if win == "weekly" else win)
                    rem_pct = round(float(b["remainingFraction"]) * 100.0, 1)
                    items.append(
                        {
                            "label": f"{short_grp} {win_label}".strip(),
                            "remaining_percent": rem_pct,
                            "used_percent": round(100.0 - rem_pct, 1),
                            "reset_at": b.get("resetTime"),
                            "detail": None,
                        }
                    )

        models = body.get("models") or {}
        gemini_entries: list[tuple[float, str | None]] = []
        claude_entries: list[tuple[float, str | None]] = []
        for mid, info in models.items():
            if mid.startswith("tab_"):
                continue
            qi = info.get("quotaInfo")
            if not isinstance(qi, dict) or "remainingFraction" not in qi:
                continue
            frac = float(qi["remainingFraction"])
            reset_time = qi.get("resetTime")
            low_id = mid.lower()
            if "gemini" in low_id:
                gemini_entries.append((frac, reset_time))
            elif ("claude" in low_id or "gpt-oss" in low_id) and not seen_claude:
                claude_entries.append((frac, reset_time))

        if gemini_entries and not any(it["label"].startswith("Gemini") for it in items):
            min_frac, reset_time = min(gemini_entries, key=lambda x: (x[0], x[1] or ""))
            rem_pct = round(min_frac * 100.0, 1)
            items.insert(
                0,
                {
                    "label": "Gemini",
                    "remaining_percent": rem_pct,
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": reset_time,
                    "detail": None,
                },
            )
        if claude_entries:
            min_frac, reset_time = min(claude_entries, key=lambda x: (x[0], x[1] or ""))
            rem_pct = round(min_frac * 100.0, 1)
            items.append(
                {
                    "label": "Claude / OSS",
                    "remaining_percent": rem_pct,
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": reset_time,
                    "detail": None,
                },
            )
        return _ok_card("agy", "Antigravity", "agy", email, items)
    except Exception as exc:
        return _err_card("agy", "Antigravity", "agy", str(exc))


# ---------------------------------------------------------------------------
# 2. Factory Droid (droid)
# ---------------------------------------------------------------------------

def _find_node() -> str:
    for candidate in [shutil.which("node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"]:
        if candidate and os.path.exists(candidate):
            return candidate
    raise RuntimeError("未找到 node（用于解密 droid 本地凭据）")


def _droid_load_creds() -> dict[str, Any]:
    node_bin = _find_node()
    node_script = r"""
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

function decrypt(text, keyBuf) {
  const [s, c, p] = text.trim().split(':');
  const d = crypto.createDecipheriv('aes-256-gcm', keyBuf, Buffer.from(s, 'base64'));
  d.setAuthTag(Buffer.from(c, 'base64'));
  return Buffer.concat([d.update(Buffer.from(p, 'base64')), d.final()]).toString('utf8');
}

const home = process.env.HOME;
const attempts = [
  () => {
    const k = execFileSync('security', ['find-generic-password', '-s', 'Factory CLI', '-a', 'auth-encryption-key-security-cli', '-w'], {encoding:'utf8'}).trim();
    const raw = fs.readFileSync(path.join(home, '.factory/auth.v2.loginkeychain'), 'utf8');
    return JSON.parse(decrypt(raw, Buffer.from(k, 'base64')));
  },
  () => {
    const k = execFileSync('security', ['find-generic-password', '-s', 'Factory CLI', '-a', 'auth-encryption-key', '-w'], {encoding:'utf8'}).trim();
    const raw = fs.readFileSync(path.join(home, '.factory/auth.v2.loginkeychain'), 'utf8');
    return JSON.parse(decrypt(raw, Buffer.from(k, 'base64')));
  },
  () => {
    const k = fs.readFileSync(path.join(home, '.factory/auth.v2.key'), 'utf8').trim();
    const raw = fs.readFileSync(path.join(home, '.factory/auth.v2.file'), 'utf8');
    return JSON.parse(decrypt(raw, Buffer.from(k, 'base64')));
  },
];

for (const fn of attempts) {
  try {
    const out = fn();
    if (out && (out.access_token || out.accessToken)) {
      process.stdout.write(JSON.stringify(out));
      process.exit(0);
    }
  } catch (_) {}
}
process.exit(1);
"""
    res = subprocess.run([node_bin, "-e", node_script], capture_output=True, text=True, timeout=8)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("无法读取 ~/.factory/auth.v2 凭据，请先运行 droid 登录")
    return json.loads(res.stdout)


def _droid_refresh_token(refresh_token: str) -> str | None:
    for cid in ["client_01HNM7927XNSKCJ4982Z5J3FFZ", "client_01HNM792M5G5G1A2THWPXKFMXB"]:
        status, body = http_json(
            "https://api.workos.com/user_management/authenticate",
            method="POST",
            json_body={"client_id": cid, "grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            return body["access_token"]
    return None


def detect_droid() -> bool:
    return (HOME / ".factory" / "auth.v2.loginkeychain").exists() or (HOME / ".factory" / "auth.v2.file").exists()


def fetch_droid() -> dict[str, Any]:
    try:
        creds = _droid_load_creds()
        tok = creds.get("access_token") or creds.get("accessToken")
        refresh_tok = creds.get("refresh_token") or creds.get("refreshToken")
        whoami = creds.get("whoami") or {}
        subtitle = whoami.get("email") or "Factory"

        def call_limits(bearer: str) -> tuple[int, Any]:
            return http_json(
                "https://api.factory.ai/api/billing/limits",
                method="GET",
                headers={"Authorization": f"Bearer {bearer}"},
            )

        status, body = call_limits(tok)
        if status == 401 and refresh_tok:
            new_tok = _droid_refresh_token(refresh_tok)
            if new_tok:
                status, body = call_limits(new_tok)

        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        limits = body.get("limits") or {}
        std = limits.get("standard") or {}
        core = limits.get("core") or {}
        items: list[dict[str, Any]] = []
        for label, bucket in [
            ("Standard 5h", std.get("fiveHour")),
            ("Standard Weekly", std.get("weekly")),
            ("Standard Monthly", std.get("monthly")),
            ("Droid Core Monthly", core.get("monthly")),
        ]:
            if not isinstance(bucket, dict) or bucket.get("usedPercent") is None:
                continue
            used = float(bucket["usedPercent"])
            rem = max(0.0, round(100.0 - used, 1))
            items.append(
                {
                    "label": label,
                    "remaining_percent": rem,
                    "used_percent": round(used, 1),
                    "reset_at": bucket.get("windowEnd"),
                    "detail": None,
                }
            )

        extra_cents = body.get("extraUsageBalanceCents")
        overage = body.get("overagePreference")
        if extra_cents is not None:
            dollars = float(extra_cents) / 100.0
            items.append(
                {
                    "label": "Extra Balance",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": None,
                    "detail": f"${dollars:.2f}" + (f" · {overage}" if overage else ""),
                }
            )
        return _ok_card("droid", "Factory Droid", "droid", subtitle, items)
    except Exception as exc:
        return _err_card("droid", "Factory Droid", "droid", str(exc))


# ---------------------------------------------------------------------------
# 3. Grok Build (grok)
# ---------------------------------------------------------------------------

def _grok_refresh_token(entry: dict[str, Any]) -> str | None:
    refresh_tok = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    issuer = (entry.get("oidc_issuer") or "https://auth.x.ai").rstrip("/")
    if not refresh_tok or not client_id:
        return None
    status, body = http_json(
        f"{issuer}/oauth2/token",
        method="POST",
        form_body={"grant_type": "refresh_token", "refresh_token": refresh_tok, "client_id": client_id},
    )
    if status == 200 and isinstance(body, dict) and body.get("access_token"):
        return body["access_token"]
    return None


def detect_grok() -> bool:
    return (HOME / ".grok" / "auth.json").exists()


def fetch_grok() -> dict[str, Any]:
    try:
        auth_path = HOME / ".grok" / "auth.json"
        if not auth_path.exists():
            raise RuntimeError("未找到 ~/.grok/auth.json，请先运行 grok login")
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        if not data:
            raise RuntimeError("~/.grok/auth.json 为空")
        entry = next(iter(data.values()))
        tok = entry.get("key")
        email = entry.get("email") or "xAI"
        if not tok:
            raise RuntimeError("~/.grok/auth.json 缺少 key")

        def call_billing(bearer: str) -> tuple[int, Any]:
            return http_json(
                "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                method="GET",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "X-XAI-Token-Auth": "xai-grok-cli",
                    "x-grok-client-version": "1.0.92",
                    "Accept": "application/json",
                },
            )

        status, body = call_billing(tok)
        if status == 401:
            new_tok = _grok_refresh_token(entry)
            if new_tok:
                status, body = call_billing(new_tok)

        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        cfg = body.get("config") or {}
        period = cfg.get("currentPeriod") or {}
        period_type = period.get("type") or ""
        period_label = "Weekly Limit" if "WEEKLY" in period_type else (
            "Monthly Limit" if "MONTHLY" in period_type else "Plan Limit"
        )
        used_pct = float(cfg.get("creditUsagePercent") or 0.0)
        rem_pct = max(0.0, round(100.0 - used_pct, 1))
        reset_at = period.get("end") or cfg.get("billingPeriodEnd")

        items: list[dict[str, Any]] = [
            {
                "label": period_label,
                "remaining_percent": rem_pct,
                "used_percent": round(used_pct, 1),
                "reset_at": reset_at,
                "detail": None,
            }
        ]
        prepaid_val = (cfg.get("prepaidBalance") or {}).get("val")
        ondemand_cap = (cfg.get("onDemandCap") or {}).get("val")
        ondemand_used = (cfg.get("onDemandUsed") or {}).get("val")
        if prepaid_val is not None:
            prepaid_num = float(prepaid_val)
            cap_num = float(ondemand_cap or 0)
            used_num = float(ondemand_used or 0)
            if prepaid_num > 0 or cap_num > 0:
                items.append(
                    {
                        "label": "Prepaid / On-Demand",
                        "remaining_percent": None,
                        "used_percent": None,
                        "reset_at": None,
                        "detail": f"Prepaid ${prepaid_num / 100:.2f} · On-Demand ${used_num / 100:.2f}/${cap_num / 100:.2f}",
                    }
                )
        return _ok_card("grok", "Grok Build", "grok", email, items)
    except Exception as exc:
        return _err_card("grok", "Grok Build", "grok", str(exc))


# ---------------------------------------------------------------------------
# 4. Amp (amp)
# ---------------------------------------------------------------------------

def _find_amp() -> str | None:
    for candidate in [shutil.which("amp"), str(HOME / ".local/bin/amp"), str(HOME / ".amp/bin/amp")]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def detect_amp() -> bool:
    return _find_amp() is not None


def fetch_amp() -> dict[str, Any]:
    try:
        amp_bin = _find_amp()
        if not amp_bin:
            raise RuntimeError("未找到 amp 命令")
        res = subprocess.run([amp_bin, "usage"], capture_output=True, text=True, timeout=HTTP_TIMEOUT)
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        if res.returncode != 0:
            raise RuntimeError(out.strip() or f"amp usage exited {res.returncode}")

        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out).replace("**", "")
        subtitle = None
        m_user = re.search(r"Signed in as\s+([^\s(]+)", clean)
        if m_user:
            subtitle = m_user.group(1)

        items: list[dict[str, Any]] = []
        m_free = re.search(r"Amp Free:\s*([0-9]+(?:\.[0-9]+)?)%\s*remaining\s*([^\-\n]*)", clean, re.IGNORECASE)
        if m_free:
            rem = float(m_free.group(1))
            note = m_free.group(2).strip()
            items.append(
                {
                    "label": "Amp Free",
                    "remaining_percent": round(rem, 1),
                    "used_percent": round(100.0 - rem, 1),
                    "reset_at": None,
                    "detail": note or "resets daily",
                }
            )
        for m_ws in re.finditer(r"Workspace\s+([^:\n]+):\s*(\$[0-9,.]+)\s*remaining", clean, re.IGNORECASE):
            items.append(
                {
                    "label": f"Workspace {m_ws.group(1).strip()}",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": None,
                    "detail": f"{m_ws.group(2).strip()} remaining",
                }
            )
        if not items:
            for line in clean.splitlines():
                m_pct = re.match(r"^([^:]+):\s*([0-9]+(?:\.[0-9]+)?)%\s*remaining", line.strip(), re.I)
                if m_pct:
                    rem = float(m_pct.group(2))
                    items.append(
                        {
                            "label": m_pct.group(1).strip(),
                            "remaining_percent": round(rem, 1),
                            "used_percent": round(100.0 - rem, 1),
                            "reset_at": None,
                            "detail": None,
                        }
                    )
        return _ok_card("amp", "Amp", "amp", subtitle or "Amp CLI", items)
    except Exception as exc:
        return _err_card("amp", "Amp", "amp", str(exc))


# ---------------------------------------------------------------------------
# 5. OpenAI Codex (codex)
# ---------------------------------------------------------------------------

def detect_codex() -> bool:
    p = HOME / ".codex" / "auth.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return bool((d.get("tokens") or {}).get("access_token"))
    except Exception:
        return False


def _codex_refresh(refresh_tok: str) -> str | None:
    status, body = http_json(
        "https://auth.openai.com/oauth/token",
        method="POST",
        json_body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        },
    )
    if status == 200 and isinstance(body, dict) and body.get("access_token"):
        return body["access_token"]
    return None


def fetch_codex() -> dict[str, Any]:
    try:
        auth_path = HOME / ".codex" / "auth.json"
        if not auth_path.exists():
            raise RuntimeError("未找到 ~/.codex/auth.json，请先登录 Codex")
        creds = json.loads(auth_path.read_text(encoding="utf-8"))
        toks = creds.get("tokens") or {}
        acc = toks.get("access_token")
        refresh_tok = toks.get("refresh_token")
        acct_id = toks.get("account_id")
        if not acc:
            raise RuntimeError("~/.codex/auth.json 缺少 access_token")

        def call_wham(bearer: str) -> tuple[int, Any]:
            headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
            if acct_id:
                headers["ChatGPT-Account-Id"] = acct_id
            return http_json("https://chatgpt.com/backend-api/wham/usage", method="GET", headers=headers)

        status, body = call_wham(acc)
        if status == 401 and refresh_tok:
            new_tok = _codex_refresh(refresh_tok)
            if new_tok:
                status, body = call_wham(new_tok)

        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        email = body.get("email") or ""
        plan = body.get("plan_type") or ""
        subtitle = f"{email} ({plan})" if email and plan else (email or plan or "OpenAI")

        def window_label(secs: Any, default: str) -> str:
            if not secs:
                return default
            s = int(secs)
            if s <= 21600:
                return f"{max(1, s // 3600)}h Limit"
            days = round(s / 86400)
            if days == 7:
                return "Weekly Limit"
            return f"{days}d Limit"

        items: list[dict[str, Any]] = []
        rl = body.get("rate_limit") or {}
        for key, default_lbl in [("primary_window", "Primary"), ("secondary_window", "Secondary")]:
            win = rl.get(key)
            if isinstance(win, dict) and win.get("used_percent") is not None:
                used = float(win["used_percent"])
                rem = max(0.0, round(100.0 - used, 1))
                items.append(
                    {
                        "label": window_label(win.get("limit_window_seconds"), default_lbl),
                        "remaining_percent": rem,
                        "used_percent": round(used, 1),
                        "reset_at": _epoch_to_iso(win.get("reset_at")),
                        "detail": None,
                    }
                )

        cr = body.get("code_review_rate_limit") or {}
        cr_win = cr.get("primary_window")
        if isinstance(cr_win, dict) and cr_win.get("used_percent") is not None:
            used = float(cr_win["used_percent"])
            rem = max(0.0, round(100.0 - used, 1))
            items.append(
                {
                    "label": "Code Review",
                    "remaining_percent": rem,
                    "used_percent": round(used, 1),
                    "reset_at": _epoch_to_iso(cr_win.get("reset_at")),
                    "detail": None,
                }
            )

        credits = body.get("credits") or {}
        if credits.get("balance") is not None:
            items.append(
                {
                    "label": "Credits",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": None,
                    "detail": f"${float(credits['balance']):.2f}",
                }
            )
        return _ok_card("codex", "OpenAI Codex", "codex", subtitle, items)
    except Exception as exc:
        return _err_card("codex", "OpenAI Codex", "codex", str(exc))


# ---------------------------------------------------------------------------
# 6. Claude Code (claude)
# ---------------------------------------------------------------------------

def _claude_read_creds() -> dict[str, Any]:
    cred_file = HOME / ".claude" / ".credentials.json"
    if cred_file.exists():
        raw = json.loads(cred_file.read_text(encoding="utf-8"))
        return raw.get("claudeAiOauth") or raw
    res = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if res.returncode == 0 and res.stdout.strip():
        raw = json.loads(res.stdout.strip())
        return raw.get("claudeAiOauth") or raw
    raise RuntimeError("未找到 Claude Code 凭据，请先运行 claude 登录")


def _claude_refresh(refresh_tok: str) -> str | None:
    status, body = http_json(
        "https://console.anthropic.com/v1/oauth/token",
        method="POST",
        json_body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        },
    )
    if status == 200 and isinstance(body, dict) and body.get("access_token"):
        return body["access_token"]
    return None


def detect_claude() -> bool:
    return (HOME / ".claude" / ".credentials.json").exists() or _keychain_has("Claude Code-credentials")


def fetch_claude() -> dict[str, Any]:
    try:
        oauth = _claude_read_creds()
        tok = oauth.get("accessToken") or oauth.get("access_token")
        refresh_tok = oauth.get("refreshToken") or oauth.get("refresh_token")
        sub_type = oauth.get("subscriptionType") or oauth.get("rateLimitTier") or "Claude"
        if not tok:
            raise RuntimeError("Claude Code accessToken 为空")

        def call_usage(bearer: str) -> tuple[int, Any]:
            return http_json(
                "https://api.anthropic.com/api/oauth/usage",
                method="GET",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                },
            )

        status, body = call_usage(tok)
        if status == 401 and refresh_tok:
            new_tok = _claude_refresh(refresh_tok)
            if new_tok:
                status, body = call_usage(new_tok)

        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}（请重新登录 claude）")

        items: list[dict[str, Any]] = []
        for key, label in [
            ("five_hour", "5h Session"),
            ("seven_day", "Weekly All Models"),
            ("seven_day_sonnet", "Weekly Sonnet"),
            ("seven_day_opus", "Weekly Opus"),
        ]:
            bucket = body.get(key)
            if isinstance(bucket, dict) and bucket.get("utilization") is not None:
                used = float(bucket["utilization"])
                rem = max(0.0, round(100.0 - used, 1))
                items.append(
                    {
                        "label": label,
                        "remaining_percent": rem,
                        "used_percent": round(used, 1),
                        "reset_at": bucket.get("resets_at"),
                        "detail": None,
                    }
                )
        extra = body.get("extra_usage")
        if isinstance(extra, dict) and extra.get("is_enabled"):
            used_c = extra.get("used_credits") or 0
            limit_c = extra.get("monthly_limit") or 0
            items.append(
                {
                    "label": "Extra Usage",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": None,
                    "detail": f"${used_c / 100:.2f} / ${limit_c / 100:.2f}",
                }
            )
        return _ok_card("claude", "Claude Code", "claude", str(sub_type), items)
    except Exception as exc:
        return _err_card("claude", "Claude Code", "claude", str(exc))


# ---------------------------------------------------------------------------
# 7. GitHub Copilot (copilot)
# ---------------------------------------------------------------------------

def _copilot_token() -> str | None:
    gh_bin = "/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else shutil.which("gh")
    if gh_bin:
        res = subprocess.run([gh_bin, "auth", "token"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    for path in [
        HOME / ".config" / "github-copilot" / "apps.json",
        HOME / ".config" / "github-copilot" / "hosts.json",
    ]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for val in data.values():
                    if isinstance(val, dict) and val.get("oauth_token"):
                        return val["oauth_token"]
            except Exception:
                pass
    return None


def detect_copilot() -> bool:
    return _copilot_token() is not None


def fetch_copilot() -> dict[str, Any]:
    try:
        tok = _copilot_token()
        if not tok:
            raise RuntimeError("未找到 GitHub Copilot / gh 登录凭据")
        status, body = http_json(
            "https://api.github.com/copilot_internal/user",
            method="GET",
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept": "application/vnd.github+json",
            },
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        login = body.get("login") or ""
        plan = body.get("copilot_plan") or body.get("access_type_sku") or "Copilot"
        subtitle = f"{login} ({plan})" if login else str(plan)
        reset_at = body.get("quota_reset_date_utc") or body.get("quota_reset_date")
        snaps = body.get("quota_snapshots") or {}

        items: list[dict[str, Any]] = []
        for key, label in [
            ("premium_interactions", "Premium Requests"),
            ("chat", "Chat"),
            ("completions", "Completions"),
        ]:
            s = snaps.get(key)
            if not isinstance(s, dict):
                continue
            if s.get("unlimited"):
                items.append(
                    {
                        "label": label,
                        "remaining_percent": 100.0,
                        "used_percent": 0.0,
                        "reset_at": reset_at,
                        "detail": "Unlimited",
                    }
                )
                continue
            ent = s.get("entitlement")
            if key == "premium_interactions" and not s.get("has_quota") and (ent == 0 or ent is None):
                continue
            rem_pct = float(s.get("percent_remaining") or 0.0)
            rem = s.get("remaining")
            detail = f"{int(rem)}/{int(ent)}" if (rem is not None and ent) else None
            items.append(
                {
                    "label": label,
                    "remaining_percent": round(rem_pct, 1),
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": reset_at,
                    "detail": detail,
                }
            )
        return _ok_card("copilot", "GitHub Copilot", "copilot", subtitle, items)
    except Exception as exc:
        return _err_card("copilot", "GitHub Copilot", "copilot", str(exc))


# ---------------------------------------------------------------------------
# 8. Kimi Code (kimi)
# ---------------------------------------------------------------------------

def _kimi_load_token() -> tuple[str | None, str]:
    base_url = os.environ.get("KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1").rstrip("/")
    cred_dir = Path(os.environ.get("KIMI_CODE_HOME", str(HOME / ".kimi-code"))) / "credentials"
    for fname in ["kimi-code.json", "oauth_kimi-code.json"]:
        p = cred_dir / fname
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                tok = d.get("access_token") or d.get("api_key")
                if tok:
                    return tok, base_url
            except Exception:
                pass
    if os.environ.get("KIMI_API_KEY"):
        return os.environ["KIMI_API_KEY"], base_url
    return None, base_url


def detect_kimi() -> bool:
    tok, _ = _kimi_load_token()
    return tok is not None


def fetch_kimi() -> dict[str, Any]:
    try:
        tok, base_url = _kimi_load_token()
        if not tok:
            raise RuntimeError("未找到 Kimi Code 登录凭据，请运行 kimi login")
        status, body = http_json(
            f"{base_url}/usages",
            method="GET",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        items: list[dict[str, Any]] = []
        raw_usage = body.get("usage")
        if isinstance(raw_usage, dict) and raw_usage.get("limit"):
            limit = float(raw_usage["limit"])
            used = float(raw_usage.get("used") or 0.0)
            rem_pct = max(0.0, round((1.0 - used / limit) * 100.0, 1)) if limit > 0 else 0.0
            items.append(
                {
                    "label": raw_usage.get("name") or "Weekly Usage",
                    "remaining_percent": rem_pct,
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": raw_usage.get("resetTime"),
                    "detail": f"{int(limit - used)}/{int(limit)}",
                }
            )
        for item in body.get("limits") or []:
            if not isinstance(item, dict):
                continue
            detail = item.get("detail") or {}
            limit = float(detail.get("limit") or 0)
            used = float(detail.get("used") or 0)
            if limit <= 0:
                continue
            rem_pct = max(0.0, round((1.0 - used / limit) * 100.0, 1))
            win = item.get("window") or {}
            win_lbl = item.get("name") or (f"{win.get('duration', '')} {win.get('timeUnit', '')}".strip() or "Rate Limit")
            items.append(
                {
                    "label": win_lbl,
                    "remaining_percent": rem_pct,
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": detail.get("resetTime"),
                    "detail": None,
                }
            )
        return _ok_card("kimi", "Kimi Code", "kimi", "Kimi Coding", items)
    except Exception as exc:
        return _err_card("kimi", "Kimi Code", "kimi", str(exc))


# ---------------------------------------------------------------------------
# 9. Gemini CLI (gemini)
# ---------------------------------------------------------------------------

def detect_gemini() -> bool:
    return (HOME / ".gemini" / "oauth_creds.json").exists()


def fetch_gemini() -> dict[str, Any]:
    try:
        creds_path = HOME / ".gemini" / "oauth_creds.json"
        if not creds_path.exists():
            raise RuntimeError("未找到 ~/.gemini/oauth_creds.json，请先运行 gemini 登录")
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        tok = creds.get("access_token")
        if not tok:
            raise RuntimeError("~/.gemini/oauth_creds.json 缺少 access_token")

        accts_path = HOME / ".gemini" / "google_accounts.json"
        email = "Google"
        if accts_path.exists():
            try:
                email = json.loads(accts_path.read_text(encoding="utf-8")).get("active") or "Google"
            except Exception:
                pass

        _, lca = http_json(
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            method="POST",
            headers={"Authorization": f"Bearer {tok}"},
            json_body={"metadata": {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED"}},
        )
        proj = lca.get("cloudaicompanionProject") if isinstance(lca, dict) else None
        payload = {"project": proj} if proj else {}
        status, body = http_json(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
            method="POST",
            headers={"Authorization": f"Bearer {tok}"},
            json_body=payload,
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        pro_vals: list[tuple[float, str | None]] = []
        flash_vals: list[tuple[float, str | None]] = []
        for b in body.get("buckets") or []:
            if "remainingFraction" not in b:
                continue
            mid = (b.get("modelId") or "").lower()
            frac = float(b["remainingFraction"])
            rt = b.get("resetTime")
            if "flash" in mid:
                flash_vals.append((frac, rt))
            elif "pro" in mid or mid:
                pro_vals.append((frac, rt))

        items: list[dict[str, Any]] = []
        for lbl, arr in [("Gemini Pro", pro_vals), ("Gemini Flash", flash_vals)]:
            if not arr:
                continue
            min_f, rt = min(arr, key=lambda x: x[0])
            rem = round(min_f * 100.0, 1)
            items.append(
                {
                    "label": lbl,
                    "remaining_percent": rem,
                    "used_percent": round(100.0 - rem, 1),
                    "reset_at": rt,
                    "detail": None,
                }
            )
        return _ok_card("gemini", "Gemini CLI", "gemini", email, items)
    except Exception as exc:
        return _err_card("gemini", "Gemini CLI", "gemini", str(exc))


# ---------------------------------------------------------------------------
# 10. Cursor (cursor)
# ---------------------------------------------------------------------------

def _cursor_read_db() -> dict[str, str]:
    vscdb = HOME / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not vscdb.exists():
        return {}
    conn = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%'").fetchall()
        return {str(k): str(v) for k, v in rows}
    finally:
        conn.close()


def detect_cursor() -> bool:
    kv = _cursor_read_db()
    return bool(kv.get("cursorAuth/accessToken") or kv.get("cursorAuth/cachedEmail"))


def fetch_cursor() -> dict[str, Any]:
    try:
        kv = _cursor_read_db()
        tok = kv.get("cursorAuth/accessToken")
        email = kv.get("cursorAuth/cachedEmail") or "Cursor"
        if not tok:
            raise RuntimeError("Cursor 未登录或缺少 accessToken")

        status, body = http_json(
            "https://cursor.com/api/usage-summary",
            method="GET",
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept": "application/json",
            },
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        plan = ((body.get("individualUsage") or {}).get("plan")) or {}
        reset_at = body.get("billingCycleEnd")
        items: list[dict[str, Any]] = []
        if plan.get("limit"):
            used = float(plan.get("used") or 0)
            limit = float(plan["limit"])
            rem_pct = max(0.0, round((1.0 - used / limit) * 100.0, 1)) if limit > 0 else 0.0
            items.append(
                {
                    "label": "Included Usage",
                    "remaining_percent": rem_pct,
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": reset_at,
                    "detail": None,
                }
            )
        return _ok_card("cursor", "Cursor", "cursor", email, items)
    except Exception as exc:
        return _err_card("cursor", "Cursor", "cursor", str(exc))


# ---------------------------------------------------------------------------
# 11. Freebuff / Codebuff (freebuff)
# ---------------------------------------------------------------------------

def _freebuff_read_token() -> str | None:
    env_tok = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_API_KEY")
    if env_tok and env_tok.strip():
        return env_tok.strip()
    res = subprocess.run(
        ["security", "find-generic-password", "-s", "freebuff-cli", "-w"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return None


def detect_freebuff() -> bool:
    return bool(
        os.environ.get("CODEBUFF_API_KEY")
        or os.environ.get("FREEBUFF_API_KEY")
        or _has_bin("freebuff")
        or _has_bin("codebuff")
        or (HOME / ".config" / "freebuff-desktop").exists()
        or _keychain_has("freebuff-cli")
    )


def fetch_freebuff() -> dict[str, Any]:
    try:
        tok = _freebuff_read_token()
        if not tok:
            raise RuntimeError("未找到 Freebuff/Codebuff 凭据，请先运行 freebuff login")
        status, body = http_json(
            "https://www.codebuff.com/api/v1/usage",
            method="POST",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
            json_body={"authToken": tok, "fingerprintId": "freebuff-cli"},
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        usage = float(body.get("usage") or 0.0)
        rem_bal = float(body.get("remainingBalance") or 0.0)
        total = usage + rem_bal
        reset_at = body.get("next_quota_reset")
        breakdown = body.get("balanceBreakdown") or {}
        sub_bal = float(breakdown.get("subscription") or 0.0)
        free_bal = float(breakdown.get("free") or 0.0)

        items: list[dict[str, Any]] = []
        if total > 0:
            rem_pct = max(0.0, round(rem_bal / total * 100.0, 1))
            items.append(
                {
                    "label": "Credits",
                    "remaining_percent": rem_pct,
                    "used_percent": round(100.0 - rem_pct, 1),
                    "reset_at": reset_at,
                    "detail": f"余 {rem_bal:g} / 共 {total:g}",
                }
            )
        else:
            items.append(
                {
                    "label": "Freebuff Credits",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": reset_at,
                    "detail": f"已用 {usage:g} · 余 {rem_bal:g}",
                }
            )
        if sub_bal > 0 or free_bal > 0:
            items.append(
                {
                    "label": "Breakdown",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": None,
                    "detail": f"Free {free_bal:g} · Sub {sub_bal:g}",
                }
            )
        subtitle = "Auto Top-up" if body.get("autoTopupEnabled") else "Codebuff"
        return _ok_card("freebuff", "Freebuff", "freebuff", subtitle, items)
    except Exception as exc:
        return _err_card("freebuff", "Freebuff", "freebuff", str(exc))


# ---------------------------------------------------------------------------
# 12. Amazon Kiro (kiro)
# ---------------------------------------------------------------------------

def _kiro_db_path() -> Path:
    return HOME / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"


def _kiro_read_state() -> dict[str, Any]:
    db = _kiro_db_path()
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out: dict[str, Any] = {}
        for table, keys in [
            ("auth_kv", ["kirocli:odic:token", "kirocli:odic:device-registration"]),
            ("state", ["api.codewhisperer.profile", "auth.idc.region"]),
        ]:
            for key in keys:
                row = conn.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
                if row and row[0] is not None:
                    try:
                        out[key] = json.loads(row[0])
                    except Exception:
                        out[key] = row[0]
        return out
    finally:
        conn.close()


def _kiro_refresh_oidc(region: str, tok: dict[str, Any], reg: dict[str, Any]) -> str | None:
    if not (tok.get("refresh_token") and reg.get("client_id") and reg.get("client_secret")):
        return None
    status, body = http_json(
        f"https://oidc.{region}.amazonaws.com/token",
        method="POST",
        json_body={
            "clientId": reg["client_id"],
            "clientSecret": reg["client_secret"],
            "grantType": "refresh_token",
            "refreshToken": tok["refresh_token"],
        },
    )
    if status == 200 and isinstance(body, dict) and body.get("accessToken"):
        return str(body["accessToken"])
    return None


def detect_kiro() -> bool:
    return bool(_has_bin("kiro-cli") or _kiro_db_path().exists())


def fetch_kiro() -> dict[str, Any]:
    try:
        state = _kiro_read_state()
        tok = state.get("kirocli:odic:token")
        if not isinstance(tok, dict) or not tok.get("access_token"):
            raise RuntimeError("未找到 Kiro 登录凭据，请先运行 kiro-cli login")

        reg = state.get("kirocli:odic:device-registration") or {}
        region_raw = state.get("auth.idc.region")
        region = (
            region_raw.get("region")
            if isinstance(region_raw, dict)
            else (region_raw or tok.get("region") or reg.get("region") or "us-east-1")
        )
        profile = state.get("api.codewhisperer.profile") or {}
        arn = profile.get("arn") or ""
        profile_name = profile.get("profile_name") or region

        access_tok = str(tok["access_token"])
        # Refresh proactively if expires_at has passed
        exp_raw = tok.get("expires_at")
        expired = False
        if isinstance(exp_raw, str) and exp_raw:
            try:
                exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                expired = exp_dt <= datetime.now(timezone.utc)
            except Exception:
                pass
        if expired and isinstance(reg, dict):
            refreshed = _kiro_refresh_oidc(str(region), tok, reg)
            if refreshed:
                access_tok = refreshed

        url = f"https://q.{region}.amazonaws.com/getUsageLimits?origin=AI_EDITOR&resourceType=AGENTIC_REQUEST"
        if arn:
            url += f"&profileArn={urllib.parse.quote(str(arn), safe='')}"
        status, body = http_json(url, method="GET", headers={"Authorization": f"Bearer {access_tok}"})
        if status in (401, 403) and not expired and isinstance(reg, dict):
            refreshed = _kiro_refresh_oidc(str(region), tok, reg)
            if refreshed:
                status, body = http_json(url, method="GET", headers={"Authorization": f"Bearer {refreshed}"})

        if status in (401, 403):
            raise RuntimeError("Kiro 会话已过期，请运行 kiro-cli login 重新授权")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        items: list[dict[str, Any]] = []
        reset_at = _epoch_to_iso(body.get("nextDateReset"))
        for entry in body.get("usageBreakdownList") or []:
            if not isinstance(entry, dict):
                continue
            limit = entry.get("usageLimitWithPrecision")
            if limit is None:
                limit = entry.get("usageLimit")
            used = entry.get("currentUsageWithPrecision")
            if used is None:
                used = entry.get("currentUsage")
            lbl = entry.get("displayName") or entry.get("resourceType") or "Usage"
            entry_reset = _epoch_to_iso(entry.get("nextDateReset")) or reset_at
            if limit is not None and float(limit) > 0:
                lim_f = float(limit)
                used_f = float(used or 0.0)
                rem_pct = max(0.0, round((1.0 - used_f / lim_f) * 100.0, 1))
                items.append(
                    {
                        "label": str(lbl),
                        "remaining_percent": rem_pct,
                        "used_percent": round(100.0 - rem_pct, 1),
                        "reset_at": entry_reset,
                        "detail": f"{used_f:g}/{lim_f:g}",
                    }
                )
        sub = (body.get("subscriptionInfo") or {}).get("subscriptionTitle") or str(profile_name)
        return _ok_card("kiro", "Amazon Kiro", "kiro", sub, items)
    except Exception as exc:
        return _err_card("kiro", "Amazon Kiro", "kiro", str(exc))


# ---------------------------------------------------------------------------
# 13. GLM Coding Plan / Z.AI (glm)
# ---------------------------------------------------------------------------

def _read_opencode_auth() -> dict[str, Any]:
    p = HOME / ".local" / "share" / "opencode" / "auth.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _read_claude_settings_env() -> dict[str, str]:
    p = HOME / ".claude" / "settings.json"
    if not p.exists():
        return {}
    try:
        env = (json.loads(p.read_text(encoding="utf-8")) or {}).get("env") or {}
        return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    except Exception:
        return {}


def _glm_resolve_key_and_origin() -> tuple[str | None, str]:
    # 1. Explicit env var or glm-acp-agent credentials
    key = os.environ.get("Z_AI_API_KEY") or os.environ.get("ZHIPU_API_KEY")
    origin = os.environ.get("Z_AI_BASE_URL") or "https://api.z.ai"
    cred_p = HOME / ".config" / "glm-acp-agent" / "credentials.json"
    if not key and cred_p.exists():
        try:
            key = (json.loads(cred_p.read_text(encoding="utf-8")) or {}).get("z_ai_api_key")
        except Exception:
            pass
    # 2. Claude settings custom bigmodel/z.ai override
    if not key:
        cenv = _read_claude_settings_env()
        base = cenv.get("ANTHROPIC_BASE_URL", "")
        if any(h in base for h in ("bigmodel.cn", "api.z.ai")) and cenv.get("ANTHROPIC_AUTH_TOKEN"):
            key = cenv["ANTHROPIC_AUTH_TOKEN"]
            origin = base
    # 3. OpenCode auth.json
    if not key:
        oc = _read_opencode_auth()
        for kname, def_org in [("zai-coding-plan", "https://api.z.ai"), ("zai", "https://api.z.ai"), ("zhipuai", "https://open.bigmodel.cn"), ("zhipu", "https://open.bigmodel.cn")]:
            entry = oc.get(kname)
            if isinstance(entry, dict) and entry.get("key"):
                key = str(entry["key"])
                origin = def_org
                break

    parsed = urllib.parse.urlparse(origin)
    host = parsed.hostname or "api.z.ai"
    if host not in ("api.z.ai", "open.bigmodel.cn", "dev.bigmodel.cn"):
        host = "open.bigmodel.cn" if "bigmodel" in origin else "api.z.ai"
    return (key.strip() if key else None), f"https://{host}"


def detect_glm() -> bool:
    key, _ = _glm_resolve_key_and_origin()
    return bool(key)


def fetch_glm() -> dict[str, Any]:
    try:
        key, origin = _glm_resolve_key_and_origin()
        if not key:
            raise RuntimeError("未找到 Z_AI_API_KEY / ~/.config/glm-acp-agent/credentials.json")

        origins = [origin] + ([o for o in ("https://api.z.ai", "https://open.bigmodel.cn") if o != origin])
        body: Any = None
        used_origin = origin
        last_status = 0
        for org in origins:
            st, b = http_json(
                f"{org}/api/monitor/usage/quota/limit",
                method="GET",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
            last_status = st
            if st == 200 and isinstance(b, dict) and isinstance((b.get("data") or {}).get("limits"), list):
                body = b
                used_origin = org
                break
        if not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {last_status}")

        limits = (body.get("data") or {}).get("limits") or []
        items: list[dict[str, Any]] = []
        unit_mins = {"3": 60, "4": 1440, "6": 10080}
        for row in limits:
            if not isinstance(row, dict):
                continue
            rtype = row.get("type")
            if rtype not in ("CREDIT_LIMIT", "TOKENS_LIMIT", "TIME_LIMIT"):
                continue
            used = row.get("currentValue")
            lim = row.get("usage")
            pct = row.get("percentage")
            if pct is None and used is not None and lim:
                pct = float(used) / float(lim) * 100.0
            if pct is None:
                continue
            used_pct = min(100.0, max(0.0, float(pct)))
            rem_pct = round(100.0 - used_pct, 1)
            mins = unit_mins.get(str(row.get("unit")))
            cnt = row.get("number")
            dur_lbl = ""
            if mins and cnt:
                total_mins = int(mins) * int(cnt)
                if total_mins % 10080 == 0:
                    dur_lbl = f"{total_mins // 10080}w "
                elif total_mins % 1440 == 0:
                    dur_lbl = f"{total_mins // 1440}d "
                else:
                    dur_lbl = f"{max(1, total_mins // 60)}h "
            lbl = f"{dur_lbl}{'MCP Quota' if rtype == 'TIME_LIMIT' else 'Coding Plan'}"
            items.append(
                {
                    "label": lbl,
                    "remaining_percent": rem_pct,
                    "used_percent": round(used_pct, 1),
                    "reset_at": _epoch_to_iso(row.get("nextResetTime")),
                    "detail": f"{used}/{lim}" if used is not None and lim is not None else None,
                }
            )
        sub = "open.bigmodel.cn" if "bigmodel" in used_origin else "api.z.ai"
        return _ok_card("glm", "GLM Coding Plan", "glm", sub, items)
    except Exception as exc:
        return _err_card("glm", "GLM Coding Plan", "glm", str(exc))


# ---------------------------------------------------------------------------
# 14. DeepSeek API (deepseek)
# ---------------------------------------------------------------------------

def _deepseek_resolve_key() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()
    cenv = _read_claude_settings_env()
    if "deepseek" in cenv.get("ANTHROPIC_BASE_URL", "").lower() and cenv.get("ANTHROPIC_AUTH_TOKEN"):
        return cenv["ANTHROPIC_AUTH_TOKEN"].strip()
    oc = _read_opencode_auth()
    ds = oc.get("deepseek")
    if isinstance(ds, dict) and ds.get("key"):
        return str(ds["key"]).strip()
    cfg = HOME / ".config" / "deepseek" / "credentials.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            if d.get("api_key"):
                return str(d["api_key"]).strip()
        except Exception:
            pass
    return None


def detect_deepseek() -> bool:
    return bool(_deepseek_resolve_key())


def fetch_deepseek() -> dict[str, Any]:
    try:
        key = _deepseek_resolve_key()
        if not key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY")
        status, body = http_json(
            "https://api.deepseek.com/user/balance",
            method="GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"API HTTP {status}")

        items: list[dict[str, Any]] = []
        for info in body.get("balance_infos") or []:
            if not isinstance(info, dict):
                continue
            cur = info.get("currency") or "USD"
            sym = "¥" if cur == "CNY" else "$"
            total_b = info.get("total_balance") or "0"
            granted_b = info.get("granted_balance")
            topped_b = info.get("topped_up_balance")
            parts = []
            if topped_b is not None:
                parts.append(f"充值 {sym}{topped_b}")
            if granted_b is not None:
                parts.append(f"赠送 {sym}{granted_b}")
            items.append(
                {
                    "label": f"Balance ({cur})",
                    "remaining_percent": None,
                    "used_percent": None,
                    "reset_at": None,
                    "detail": f"{sym}{total_b}" + (f" ({' · '.join(parts)})" if parts else ""),
                }
            )
        sub = "可用" if body.get("is_available") else "余额不足"
        return _ok_card("deepseek", "DeepSeek", "deepseek", sub, items)
    except Exception as exc:
        return _err_card("deepseek", "DeepSeek", "deepseek", str(exc))


# ---------------------------------------------------------------------------
# 15. OpenRouter (openrouter)
# ---------------------------------------------------------------------------

def _openrouter_resolve_key() -> str | None:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"].strip()
    oc = _read_opencode_auth()
    entry = oc.get("openrouter")
    if isinstance(entry, dict) and entry.get("key"):
        return str(entry["key"]).strip()
    cfg = HOME / ".config" / "openrouter" / "credentials.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            if d.get("api_key"):
                return str(d["api_key"]).strip()
        except Exception:
            pass
    return None


def detect_openrouter() -> bool:
    return bool(_openrouter_resolve_key())


def fetch_openrouter() -> dict[str, Any]:
    try:
        key = _openrouter_resolve_key()
        if not key:
            raise RuntimeError("未配置 OPENROUTER_API_KEY")
        status, body = http_json(
            "https://openrouter.ai/api/v1/credits",
            method="GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise RuntimeError(f"API HTTP {status}")

        data = body["data"]
        total = float(data.get("total_credits") or 0.0)
        used = float(data.get("total_usage") or 0.0)
        rem = max(0.0, total - used)
        rem_pct = round(rem / total * 100.0, 1) if total > 0 else None
        used_pct = round(100.0 - rem_pct, 1) if rem_pct is not None else None
        items = [
            {
                "label": "Credits",
                "remaining_percent": rem_pct,
                "used_percent": used_pct,
                "reset_at": None,
                "detail": f"${rem:.2f} / ${total:.2f}",
            }
        ]
        return _ok_card("openrouter", "OpenRouter", "openrouter", f"已用 ${used:.2f}", items)
    except Exception as exc:
        return _err_card("openrouter", "OpenRouter", "openrouter", str(exc))


# ---------------------------------------------------------------------------
# Registry & CLI
# ---------------------------------------------------------------------------

PROVIDERS: list[dict[str, Any]] = [
    {"id": "agy", "name": "Antigravity", "cli": "agy", "vendor": "Google", "detect": detect_agy, "fetch": fetch_agy},
    {"id": "droid", "name": "Factory Droid", "cli": "droid", "vendor": "Factory", "detect": detect_droid, "fetch": fetch_droid},
    {"id": "grok", "name": "Grok Build", "cli": "grok", "vendor": "xAI", "detect": detect_grok, "fetch": fetch_grok},
    {"id": "amp", "name": "Amp", "cli": "amp", "vendor": "Sourcegraph", "detect": detect_amp, "fetch": fetch_amp},
    {"id": "codex", "name": "OpenAI Codex", "cli": "codex", "vendor": "OpenAI", "detect": detect_codex, "fetch": fetch_codex},
    {"id": "claude", "name": "Claude Code", "cli": "claude", "vendor": "Anthropic", "detect": detect_claude, "fetch": fetch_claude},
    {"id": "copilot", "name": "GitHub Copilot", "cli": "copilot", "vendor": "GitHub", "detect": detect_copilot, "fetch": fetch_copilot},
    {"id": "kimi", "name": "Kimi Code", "cli": "kimi", "vendor": "Moonshot", "detect": detect_kimi, "fetch": fetch_kimi},
    {"id": "gemini", "name": "Gemini CLI", "cli": "gemini", "vendor": "Google", "detect": detect_gemini, "fetch": fetch_gemini},
    {"id": "cursor", "name": "Cursor", "cli": "cursor", "vendor": "Anysphere", "detect": detect_cursor, "fetch": fetch_cursor},
    {"id": "freebuff", "name": "Freebuff", "cli": "freebuff", "vendor": "Codebuff", "detect": detect_freebuff, "fetch": fetch_freebuff},
    {"id": "kiro", "name": "Amazon Kiro", "cli": "kiro", "vendor": "AWS", "detect": detect_kiro, "fetch": fetch_kiro},
    {"id": "glm", "name": "GLM Coding Plan", "cli": "glm", "vendor": "Zhipu / Z.AI", "detect": detect_glm, "fetch": fetch_glm},
    {"id": "deepseek", "name": "DeepSeek", "cli": "deepseek", "vendor": "DeepSeek", "detect": detect_deepseek, "fetch": fetch_deepseek},
    {"id": "openrouter", "name": "OpenRouter", "cli": "openrouter", "vendor": "OpenRouter", "detect": detect_openrouter, "fetch": fetch_openrouter},
]

PROVIDER_MAP: dict[str, dict[str, Any]] = {p["id"]: p for p in PROVIDERS}


def get_catalog() -> dict[str, Any]:
    entries = []
    for p in PROVIDERS:
        try:
            detected = bool(p["detect"]())
        except Exception:
            detected = False
        entries.append(
            {
                "id": p["id"],
                "name": p["name"],
                "cli": p["cli"],
                "vendor": p["vendor"],
                "detected": detected,
            }
        )
    return {"providers": entries}


def collect_all(selected_ids: list[str] | None = None) -> dict[str, Any]:
    if selected_ids:
        chosen = [PROVIDER_MAP[pid] for pid in selected_ids if pid in PROVIDER_MAP]
    else:
        chosen = [p for p in PROVIDERS if p["detect"]()]
        if not chosen:
            chosen = PROVIDERS[:4]

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(chosen)))) as pool:
        results = list(pool.map(lambda p: p["fetch"](), chosen))

    return {
        "generated_at": now_iso(),
        "providers": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect AI coding agent quota limits.")
    parser.add_argument("--catalog", action="store_true", help="List all supported providers and local detection state.")
    parser.add_argument("--providers", type=str, default="", help="Comma-separated provider IDs to fetch.")
    parser.add_argument("--all", action="store_true", help="Fetch all providers regardless of local detection.")
    args = parser.parse_args()

    if args.catalog:
        print(json.dumps(get_catalog(), ensure_ascii=False, indent=2))
        return

    if args.all:
        ids = [p["id"] for p in PROVIDERS]
    elif args.providers.strip():
        ids = [s.strip() for s in args.providers.split(",") if s.strip()]
    else:
        ids = None

    print(json.dumps(collect_all(ids), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
update_releases.py — Amigo Flow Portal
Atualiza o bloco const releases no HTML com dados do Jira projeto GREEN.
"""

import os, re, sys, json, requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL  = os.environ["JIRA_BASE_URL"].rstrip("/")
EMAIL     = os.environ["JIRA_EMAIL"]
TOKEN     = os.environ["JIRA_API_TOKEN"]
PROJECT   = os.environ.get("JIRA_PROJECT", "GREEN")
HTML_FILE = os.environ.get("HTML_FILE", "amigo-flow-release-portal.html")

AUTH    = HTTPBasicAuth(EMAIL, TOKEN)
HEADERS = {"Accept": "application/json"}

IGNORED_TYPES = {"bug", "subtarefa", "sub-task", "testing", "impedibug", "product issue", "atividade"}

ACTIVE_STATUSES = {
    "released", "ready to deploy", "ready to test",
    "em andamento", "revisar", "done", "backlog downstream"
}

# ── Jira ──────────────────────────────────────────────────────────────────────
def jira_get(path, params=None):
    url = f"{BASE_URL}/rest/api/3{path}"
    r = requests.get(url, headers=HEADERS, auth=AUTH, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def fetch_versions():
    print("🔍 Buscando versões do projeto GREEN...")
    data = jira_get(f"/project/{PROJECT}/versions")
    versions = {v["name"]: v for v in data if re.search(r'delivery', v["name"], re.IGNORECASE)}
    print(f"   {len(versions)} deliveries encontrados: {sorted(versions.keys(), key=lambda x: int(re.search(r'\\d+', x).group()) if re.search(r'\\d+', x) else 0)}")
    return versions

def fetch_issues(versions_meta):
    """Busca issues de cada delivery individualmente via GET simples."""
    print("🔍 Buscando issues por delivery...")
    by_version = {}

    for vname in versions_meta:
        vnum = re.search(r'\d+', vname)
        if not vnum:
            continue
        # Usa a API de issues por version (não usa /search)
        try:
            params = {
                "jql": f'project = {PROJECT} AND fixVersion = "{vname}"',
                "fields": "summary,status,fixVersions,issuetype",
                "maxResults": 100,
                "startAt": 0,
            }
            url = f"{BASE_URL}/rest/api/3/search/jql"
            r = requests.get(url, headers=HEADERS, auth=AUTH, params=params, timeout=60)
            if r.status_code == 410:
                # fallback para endpoint legado correto
                url = f"{BASE_URL}/rest/api/2/search"
                r = requests.get(url, headers=HEADERS, auth=AUTH, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"   ⚠️  Erro ao buscar {vname}: {e}")
            by_version[vname] = []
            continue

        issues = data.get("issues", [])
        features = []
        for issue in issues:
            f = issue["fields"]
            itype  = f.get("issuetype", {}).get("name", "").lower().strip()
            status = f.get("status",    {}).get("name", "").lower().strip()
            if itype in IGNORED_TYPES:
                continue
            if status not in ACTIVE_STATUSES:
                continue
            title = re.sub(r'^\[.*?\]\s*', '', f.get("summary", "")).strip()
            features.append({
                "title": title,
                "key":   issue["key"],
                "status": f.get("status", {}).get("name", ""),
            })
        by_version[vname] = features
        print(f"   {vname}: {len(features)} features")

    return by_version

# ── Serialização JS ───────────────────────────────────────────────────────────
def js(s):
    s = str(s).replace("\\","\\\\").replace("'","\\'")
    s = s.replace("\n"," ").replace("\r","")
    return f"'{s}'"

def delivery_num(name):
    m = re.search(r'\d+', name)
    return int(m.group()) if m else 0

def build_js(versions_meta, by_version):
    lines = ["const releases = ["]
    for vname in sorted(by_version.keys(), key=delivery_num, reverse=True):
        vm    = versions_meta.get(vname, {})
        rel   = vm.get("released", False)
        rdate = vm.get("releaseDate", "")
        vdesc = vm.get("description", "") or ""
        year  = 2025
        if rdate:
            try: year = int(rdate.split("-")[0])
            except: pass
        vid = re.sub(r'[^a-z0-9]', '-', vname.lower())
        feats = by_version[vname]
        if not vdesc and feats:
            vdesc = ", ".join(f["title"] for f in feats[:4])

        feats_js = ",\n".join(
            f"      {{title:{js(f['title'])},jiraKey:{js(f['key'])},"
            f"status:{js(f['status'])},"
            f"contexto:'',feito:'',impacto:''}}"
            for f in feats
        ) if feats else ""

        lines.append(
            f"  {{\n"
            f"    year:{year}, id:{js(vid)}, name:{js(vname)}, "
            f"releaseDate:{js(rdate)}, released:{'true' if rel else 'false'},\n"
            f"    desc:{js(vdesc)},\n"
            f"    features:[{feats_js}]\n"
            f"  }},"
        )
    lines.append("];")
    return "\n".join(lines)

# ── Atualiza HTML ─────────────────────────────────────────────────────────────
def update_html(new_js):
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    timestamp = f"// Atualizado automaticamente em {now}\n"

    new_content = re.sub(
        r'(?:// Atualizado automaticamente[^\n]*\n)?const releases = \[.*?\];',
        timestamp + new_js,
        content, count=1, flags=re.DOTALL
    )

    if new_content == content:
        print("⚠️  Padrão 'const releases' não encontrado no HTML.")
        return False

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"✅ {HTML_FILE} atualizado.")
    return True

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    versions_meta = fetch_versions()
    if not versions_meta:
        print("⚠️  Nenhum delivery encontrado. Abortando.")
        sys.exit(1)

    by_version = fetch_issues(versions_meta)

    if not any(by_version.values()):
        print("⚠️  Nenhuma feature encontrada. Abortando.")
        sys.exit(1)

    print("🔨 Gerando bloco JS...")
    releases_js = build_js(versions_meta, by_version)

    print(f"💾 Atualizando {HTML_FILE}...")
    ok = update_html(releases_js)

    if ok:
        total = sum(len(v) for v in by_version.values())
        print(f"\n📊 Resumo:")
        print(f"   Deliveries: {len(by_version)}")
        print(f"   Features:   {total}")

if __name__ == "__main__":
    main()

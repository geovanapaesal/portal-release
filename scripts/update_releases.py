#!/usr/bin/env python3
"""
update_releases.py — Amigo Flow Portal
Atualiza status de features existentes e adiciona novos deliveries do Jira.
Preserva contexto/feito/impacto/rich/images já escritos manualmente.
"""
import os, re, sys, requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone

BASE_URL  = os.environ["JIRA_BASE_URL"].rstrip("/")
EMAIL     = os.environ["JIRA_EMAIL"]
TOKEN     = os.environ["JIRA_API_TOKEN"]
PROJECT   = os.environ.get("JIRA_PROJECT", "GREEN")
HTML_FILE = os.environ.get("HTML_FILE", "amigo-flow-release-portal.html")
AUTH      = HTTPBasicAuth(EMAIL, TOKEN)
HEADERS   = {"Accept": "application/json"}

IGNORED_TYPES = {"bug","subtarefa","sub-task","testing","impedibug","product issue","atividade"}

# ── Jira ──────────────────────────────────────────────────────────────────────
def jira_get_versions():
    r = requests.get(f"{BASE_URL}/rest/api/3/project/{PROJECT}/versions",
                     headers=HEADERS, auth=AUTH, timeout=60)
    r.raise_for_status()
    return {v["name"]: v for v in r.json()
            if re.search(r'delivery', v["name"], re.IGNORECASE)}

def jira_search(jql, fields):
    url = f"{BASE_URL}/rest/api/3/search/jql"
    issues, start = [], 0
    while True:
        r = requests.get(url, headers=HEADERS, auth=AUTH, timeout=60, params={
            "jql": jql, "fields": ",".join(fields),
            "maxResults": 100, "startAt": start
        })
        r.raise_for_status()
        data   = r.json()
        batch  = data.get("issues", [])
        issues.extend(batch)
        total  = data.get("total", 0)
        if start + 100 >= total:
            break
        start += 100
    return issues

def dnum(name):
    m = re.search(r'\d+', name)
    return int(m.group()) if m else 0

def js(s):
    if s is None: return 'null'
    s = str(s).replace("\\","\\\\").replace("'","\\'").replace("\n"," ").replace("\r","")
    return f"'{s}'"

# ── Lê HTML ───────────────────────────────────────────────────────────────────
def read_html():
    with open(HTML_FILE, encoding="utf-8") as f:
        return f.read()

def write_html(content):
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(content)

# ── Extrai features existentes do HTML ───────────────────────────────────────
def extract_existing(content):
    """
    Retorna dict keyed by jiraKey com todo o bloco da feature (string raw).
    """
    existing = {}
    # Encontra blocos { ... jiraKey:'GREEN-XXXX' ... }
    for m in re.finditer(r"\{[^{}]*jiraKey:'(GREEN-\d+)'[^{}]*\}", content, re.DOTALL):
        key   = m.group(1)
        block = m.group(0)
        existing[key] = block
    return existing

def get_field(block, field):
    """Extrai valor de campo JS simples: field:'valor'"""
    m = re.search(rf"{field}:'((?:[^'\\]|\\.)*)'", block)
    return m.group(1) if m else ''

def get_images(block):
    """Extrai images:[...] se existir."""
    m = re.search(r'images:\[.*?\]', block, re.DOTALL)
    return m.group(0) if m else None

def get_diagram(block):
    m = re.search(r"diagram:\s*(?:'([^']*)'|null)", block)
    if not m: return '__missing__'
    return m.group(1) if m.group(1) is not None else None

# ── Monta feature JS ──────────────────────────────────────────────────────────
def build_feature(jira_key, jira_title, jira_status, existing_block=None):
    if existing_block:
        contexto = get_field(existing_block, 'contexto')
        feito    = get_field(existing_block, 'feito')
        impacto  = get_field(existing_block, 'impacto')
        diag     = get_diagram(existing_block)
        imgs     = get_images(existing_block)
    else:
        contexto = feito = impacto = ''
        diag = '__missing__'
        imgs = None

    f = (f"      {{title:{js(jira_title)},jiraKey:{js(jira_key)},"
         f"status:{js(jira_status)},"
         f"contexto:{js(contexto)},feito:{js(feito)},impacto:{js(impacto)}")

    if diag != '__missing__':
        f += f",diagram:{js(diag)}"
    if imgs:
        f += f",{imgs}"
    f += "}"
    return f

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # 1. Versões do Jira
    print("🔍 Buscando versões do projeto GREEN...")
    versions = jira_get_versions()
    sorted_names = sorted(versions.keys(), key=dnum)
    print(f"   {len(versions)} deliveries: {sorted_names}")

    # 2. Issues por delivery
    print("🔍 Buscando issues por delivery...")
    by_version = {}
    for vname in sorted_names:
        issues = jira_search(
            jql=f'project = {PROJECT} AND fixVersion = "{vname}" ORDER BY created ASC',
            fields=["summary","status","issuetype"]
        )
        feats = []
        for iss in issues:
            f      = iss["fields"]
            itype  = f.get("issuetype",{}).get("name","").lower().strip()
            status = f.get("status",{}).get("name","")
            if itype in IGNORED_TYPES:
                continue
            title = re.sub(r'^\[.*?\]\s*', '', f.get("summary","")).strip()
            feats.append({"key": iss["key"], "title": title, "status": status})
        by_version[vname] = feats
        print(f"   {vname}: {len(feats)} features")

    # 3. Lê HTML e extrai bloco de releases atual
    print(f"📄 Lendo {HTML_FILE}...")
    content = read_html()

    if "const releases = [" not in content:
        print(f"❌ Padrão 'const releases = [' não encontrado em {HTML_FILE}")
        sys.exit(1)

    # 4. Extrai features existentes para preservar conteúdo rico
    existing = extract_existing(content)
    print(f"   {len(existing)} features existentes com conteúdo preservado")

    # 5. Extrai metadados de deliveries existentes (rich, desc, id)
    existing_metas = {}
    for m in re.finditer(
        r"year:\d+,\s*id:'(d[\w-]+)',\s*name:'(Delivery \d+)',\s*releaseDate:'([^']*)',\s*released:(true|false)",
        content
    ):
        did, dname, rdate, rel = m.group(1), m.group(2), m.group(3), m.group(4)
        # Pega rich e desc do bloco seguinte
        snippet = content[m.start():m.start()+300]
        rich    = bool(re.search(r'rich:\s*true', snippet))
        desc_m  = re.search(r"desc:'((?:[^'\\]|\\.)*)'", content[m.start():m.start()+500])
        existing_metas[dname] = {
            "id": did, "rich": rich,
            "desc": desc_m.group(1) if desc_m else "",
        }

    # 6. Constrói novo bloco JS
    print("🔨 Construindo novo bloco releases...")
    lines = ["const releases = ["]
    for vname in sorted(by_version.keys(), key=dnum, reverse=True):
        vm    = versions[vname]
        rel   = vm.get("released", False)
        rdate = vm.get("releaseDate", "")
        year  = int(rdate.split("-")[0]) if rdate else 2025
        ex    = existing_metas.get(vname, {})
        did   = ex.get("id") or f"d{dnum(vname)}"
        rich  = ex.get("rich", False)
        desc  = ex.get("desc", "")
        feats = by_version[vname]
        if not desc and feats:
            desc = ", ".join(f["title"] for f in feats[:4])

        feats_js = ",\n".join(
            build_feature(f["key"], f["title"], f["status"], existing.get(f["key"]))
            for f in feats
        )

        entry  = f"  {{\n    year:{year}, id:{js(did)}, name:{js(vname)}, "
        entry += f"releaseDate:{js(rdate)}, released:{'true' if rel else 'false'},\n"
        if rich:
            entry += "    rich: true,\n"
        entry += f"    desc:{js(desc)},\n    features:[\n{feats_js}\n    ]\n  }},"
        lines.append(entry)
    lines.append("];")
    new_js = "\n".join(lines)

    # 7. Substitui no HTML
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    replacement = f"// Atualizado automaticamente em {now}\n{new_js}"

    new_content = re.sub(
        r'(?:// Atualizado automaticamente[^\n]*\n)?const releases = \[.*?\];',
        replacement,
        content,
        count=1,
        flags=re.DOTALL
    )

    if new_content == content:
        print("⚠️  HTML não alterado — padrão não encontrado ou sem mudanças.")
        sys.exit(0)

    write_html(new_content)
    total = sum(len(v) for v in by_version.values())
    print(f"\n✅ Portal atualizado!")
    print(f"   {len(by_version)} deliveries | {total} features")
    for vname in sorted(by_version.keys(), key=dnum, reverse=True):
        rel = versions[vname].get("released", False)
        print(f"   {'✅' if rel else '🔜'} {vname}: {len(by_version[vname])} features")

if __name__ == "__main__":
    main()

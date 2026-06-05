#!/usr/bin/env python3
"""
update_releases.py
------------------
Busca issues do projeto GREEN no Jira agrupadas por fixVersion
e atualiza o bloco `const releases = [...]` no HTML do portal.
"""

import os, re, sys, json, requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
BASE_URL  = os.environ["JIRA_BASE_URL"].rstrip("/")
EMAIL     = os.environ["JIRA_EMAIL"]
TOKEN     = os.environ["JIRA_API_TOKEN"]
PROJECT   = os.environ.get("JIRA_PROJECT", "GREEN")
HTML_FILE = os.environ.get("HTML_FILE", "amigo-flow-release-portal.html")

AUTH    = HTTPBasicAuth(EMAIL, TOKEN)
HEADERS = {"Accept": "application/json"}

# Statuses que entram no portal
ACTIVE_STATUSES = {
    "released", "ready to deploy", "ready to test",
    "em andamento", "revisar", "done", "backlog downstream"
}

# Tipos ignorados (bugs, subtarefas, etc.)
IGNORED_TYPES = {
    "bug", "subtarefa", "sub-task", "testing",
    "impedibug", "product issue", "atividade"
}

# ── Jira helpers ─────────────────────────────────────────────────────────────
def jira_get(path, params=None):
    url = f"{BASE_URL}/rest/api/3{path}"
    r = requests.get(url, headers=HEADERS, auth=AUTH, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def fetch_all_issues(jql, fields):
    issues, start, page = [], 0, 50
    while True:
        data = jira_get("/search/jql", {
            "jql": jql, "fields": ",".join(fields),
            "maxResults": page, "startAt": start,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        total = data.get("total", 0)
        print(f"   Buscando issues: {len(issues)}/{total}...")
        if start + page >= total:
            break
        start += page
    return issues

def fetch_versions():
    data = jira_get(f"/project/{PROJECT}/versions")
    return {v["name"]: v for v in data}

# ── Extração de texto ADF ────────────────────────────────────────────────────
def adf_to_text(node):
    if node is None: return ""
    if isinstance(node, str): return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(adf_to_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return " ".join(adf_to_text(c) for c in node)
    return ""

# ── Extrai seções da descrição ────────────────────────────────────────────────
def parse_desc(desc_raw):
    """Tenta extrair Contexto / Feito / Impacto da descrição."""
    if isinstance(desc_raw, dict):
        text = adf_to_text(desc_raw)
    else:
        text = str(desc_raw or "")
    text = re.sub(r'\s+', ' ', text).strip()

    pats = {
        "contexto": r'(?:contexto|context|problema|cenário atual)[^\w]+(.*?)(?=(?:solu[cç][aã]o|feito|o que foi|hist[oó]ria|objetivo|descri[cç][aã]o|impacto|$))',
        "feito":    r'(?:solu[cç][aã]o|o que foi feito|feito|implementa[cç][aã]o|descri[cç][aã]o)[^\w]+(.*?)(?=(?:impacto|benef[ií]cio|resultado|crit[eé]rios|$))',
        "impacto":  r'(?:impacto|benefício|resultado esperado)[^\w]+(.*?)(?=(?:crit[eé]rios|regras|observa|$))',
    }

    out = {}
    for k, p in pats.items():
        m = re.search(p, text, re.IGNORECASE | re.DOTALL)
        if m:
            out[k] = m.group(1).strip()[:500]

    return out.get("contexto",""), out.get("feito",""), out.get("impacto","")

# ── Ordenação de deliveries ──────────────────────────────────────────────────
def delivery_num(name):
    m = re.search(r'\d+', name)
    return int(m.group()) if m else 0

# ── Serialização JS ──────────────────────────────────────────────────────────
def js(s):
    s = str(s).replace("\\","\\\\").replace("'","\\'")
    s = s.replace("\n"," ").replace("\r","")
    return f"'{s}'"

# ── Monta bloco JS ───────────────────────────────────────────────────────────
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
            f"contexto:{js(f['ctx'] or 'Não informado.')},"
            f"feito:{js(f['feito'] or 'Não informado.')},"
            f"impacto:{js(f['imp'] or 'Não informado.')}}}"
            for f in feats
        )
        lines.append(
            f"  {{\n"
            f"    year:{year}, id:{js(vid)}, name:{js(vname)}, "
            f"releaseDate:{js(rdate)}, released:{'true' if rel else 'false'},\n"
            f"    desc:{js(vdesc)},\n"
            f"    features:[\n{feats_js}\n    ]\n"
            f"  }},"
        )
    lines.append("];")
    return "\n".join(lines)

# ── Atualiza HTML ────────────────────────────────────────────────────────────
def update_html(new_js):
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = r'// Atualizado automaticamente.*?\n' if '// Atualizado' in content else ''
    new_content = re.sub(
        r'(?:// Atualizado automaticamente[^\n]*\n)?const releases = \[.*?\];',
        new_js,
        content, count=1, flags=re.DOTALL
    )
    if new_content == content:
        print("⚠️  Padrão 'const releases' não encontrado no HTML.")
        return False

    # Insere/atualiza timestamp
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    new_content = new_content.replace(
        "const releases = [",
        f"// Atualizado automaticamente em {now}\nconst releases = [",
        1
    )
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"✅ {HTML_FILE} atualizado.")
    return True

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"🔍 Buscando versões do projeto {PROJECT}...")
    versions_meta = fetch_versions()
    delivery_versions = {
        k: v for k, v in versions_meta.items()
        if re.search(r'delivery', k, re.IGNORECASE)
    }
    print(f"   Deliveries encontrados: {sorted(delivery_versions.keys(), key=delivery_num)}")

    if not delivery_versions:
        print("⚠️  Nenhuma versão 'Delivery' encontrada. Verifique o projeto no Jira.")
        sys.exit(1)

    print("🔍 Buscando issues com fixVersion...")
    issues = fetch_all_issues(
        jql=f'project = {PROJECT} AND fixVersion is not EMPTY ORDER BY fixVersion DESC, updated DESC',
        fields=["summary", "status", "fixVersions", "description", "issuetype"],
    )
    print(f"   {len(issues)} issues encontradas no total.")

    by_version: dict = {}
    skipped = []

    for issue in issues:
        f      = issue["fields"]
        itype  = f.get("issuetype", {}).get("name", "").lower().strip()
        status = f.get("status",    {}).get("name", "").lower().strip()

        # Pula tipos ignorados
        if itype in IGNORED_TYPES:
            skipped.append(f"{issue['key']} (tipo: {itype})")
            continue

        # Pula statuses inativos
        if status not in ACTIVE_STATUSES:
            skipped.append(f"{issue['key']} (status: {status})")
            continue

        # Limpa título
        title = re.sub(r'^\[.*?\]\s*', '', f.get("summary", "")).strip()
        ctx, feito, imp = parse_desc(f.get("description", ""))

        for v in f.get("fixVersions", []):
            vname = v["name"]
            # só inclui deliveries
            if not re.search(r'delivery', vname, re.IGNORECASE):
                continue
            if vname not in by_version:
                by_version[vname] = []
            by_version[vname].append({
                "title": title,
                "key":   issue["key"],
                "ctx":   ctx,
                "feito": feito,
                "imp":   imp,
            })

    print(f"   {len(skipped)} issues ignoradas por tipo/status.")
    print(f"   Deliveries com features: {sorted(by_version.keys(), key=delivery_num)}")

    # Inclui deliveries sem issues (para manter no portal com lista vazia)
    for vname in delivery_versions:
        if vname not in by_version:
            by_version[vname] = []

    if not any(by_version.values()):
        print("⚠️  Nenhuma feature encontrada para nenhum delivery.")
        # Não aborta — pode ser que todas estejam em status não mapeado
        # Lista os statuses encontrados para diagnóstico
        statuses = set(
            i["fields"].get("status",{}).get("name","?").lower()
            for i in issues
        )
        print(f"   Statuses encontrados nas issues: {statuses}")
        print("   Adicione os statuses necessários à lista ACTIVE_STATUSES no script.")
        sys.exit(1)

    print("🔨 Gerando bloco JS...")
    releases_js = build_js(versions_meta, by_version)

    print(f"💾 Atualizando {HTML_FILE}...")
    ok = update_html(releases_js)

    if ok:
        total = sum(len(v) for v in by_version.values())
        print(f"\n📊 Resumo final:")
        print(f"   Deliveries: {len(by_version)}")
        print(f"   Features:   {total}")
        for vname in sorted(by_version.keys(), key=delivery_num, reverse=True):
            print(f"   • {vname}: {len(by_version[vname])} features")

if __name__ == "__main__":
    main()

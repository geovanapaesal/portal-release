#!/usr/bin/env python3
"""
update_releases.py
------------------
Busca as issues do projeto GREEN no Jira, agrupa por fixVersion (Delivery X),
e atualiza o bloco `const releases = [...]` no arquivo HTML do portal.

Variáveis de ambiente necessárias:
  JIRA_BASE_URL   ex: https://amigotech-team.atlassian.net
  JIRA_EMAIL      email da conta Atlassian
  JIRA_API_TOKEN  token de API gerado em id.atlassian.com
  JIRA_PROJECT    ex: GREEN
  HTML_FILE       ex: amigo-flow-release-portal.html
"""

import os
import re
import json
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────
BASE_URL   = os.environ["JIRA_BASE_URL"].rstrip("/")
EMAIL      = os.environ["JIRA_EMAIL"]
TOKEN      = os.environ["JIRA_API_TOKEN"]
PROJECT    = os.environ.get("JIRA_PROJECT", "GREEN")
HTML_FILE  = os.environ.get("HTML_FILE", "amigo-flow-release-portal.html")

AUTH   = HTTPBasicAuth(EMAIL, TOKEN)
HEADERS = {"Accept": "application/json"}
JIRA_BROWSE = f"{BASE_URL}/browse"

# Issue types considerados "feature" para o portal
FEATURE_TYPES = {"história", "historia", "melhoria"}

# Statuses que indicam issue entregue ou em andamento (inclui no portal)
ACTIVE_STATUSES = {
    "released", "ready to deploy", "ready to test",
    "em andamento", "revisar", "done"
}

# ──────────────────────────────────────────────
# Helpers Jira
# ──────────────────────────────────────────────
def jira_get(path, params=None):
    url = f"{BASE_URL}/rest/api/3{path}"
    r = requests.get(url, headers=HEADERS, auth=AUTH, params=params)
    r.raise_for_status()
    return r.json()


def fetch_all_issues(jql, fields, max_results=500):
    """Pagina automaticamente até trazer todos os resultados."""
    issues = []
    start = 0
    page = 50
    while True:
        data = jira_get("/search", {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": page,
            "startAt": start,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        if start + page >= data.get("total", 0):
            break
        start += page
    return issues


def fetch_versions():
    """Retorna todas as versões do projeto com metadados."""
    data = jira_get(f"/project/{PROJECT}/versions")
    return {v["name"]: v for v in data}


def extract_text_from_adf(node):
    """Extrai texto plano de um nó ADF (Atlassian Document Format)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(extract_text_from_adf(c) for c in node.get("content", []))
    if isinstance(node, list):
        return " ".join(extract_text_from_adf(c) for c in node)
    return ""


def parse_description_sections(desc_raw):
    """
    Tenta extrair seções Contexto / Feito / Impacto da descrição da issue.
    Suporta tanto ADF (dict) quanto markdown puro (str).
    Retorna (contexto, feito, impacto) como strings.
    """
    if isinstance(desc_raw, dict):
        text = extract_text_from_adf(desc_raw)
    else:
        text = str(desc_raw or "")

    # Limpa espaços excessivos
    text = re.sub(r'\s+', ' ', text).strip()

    # Tenta extrair campos nomeados comuns
    patterns = {
        "contexto": r'(?:contexto|context|problema|problem)[^\w]+(.*?)(?=(?:solu[cç][aã]o|feito|o que foi|hist[oó]ria|objetivo|descri[cç][aã]o|crit[eé]rios|$))',
        "feito":    r'(?:solu[cç][aã]o proposta|o que foi feito|feito|implementa[cç][aã]o)[^\w]+(.*?)(?=(?:impacto|benef[ií]cio|crit[eé]rios|$))',
        "impacto":  r'(?:impacto|benefício|resultado)[^\w]+(.*?)(?=(?:crit[eé]rios|regras|$))',
    }

    results = {}
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            val = m.group(1).strip()[:400]
            results[key] = val

    return (
        results.get("contexto", ""),
        results.get("feito", ""),
        results.get("impacto", ""),
    )


# ──────────────────────────────────────────────
# Numero de versão para ordenação
# ──────────────────────────────────────────────
def delivery_sort_key(name: str):
    """Extrai o número de 'Delivery 21' → 21 para ordenação."""
    m = re.search(r'\d+', name)
    return int(m.group()) if m else 0


# ──────────────────────────────────────────────
# Monta o bloco JS de releases
# ──────────────────────────────────────────────
def build_releases_js(versions_meta, issues_by_version):
    lines = ["const releases = ["]

    # Ordena do mais recente para o mais antigo
    sorted_versions = sorted(
        issues_by_version.keys(),
        key=delivery_sort_key,
        reverse=True
    )

    for vname in sorted_versions:
        vmeta = versions_meta.get(vname, {})
        released = vmeta.get("released", False)
        release_date = vmeta.get("releaseDate", "")
        v_desc = vmeta.get("description", "") or ""

        # Ano a partir da data de release ou do nome
        year = 2025
        if release_date:
            try:
                year = int(release_date.split("-")[0])
            except Exception:
                pass

        # ID seguro para JS
        vid = re.sub(r'[^a-z0-9]', '-', vname.lower())

        # Features desta versão
        features = issues_by_version[vname]

        # Descrição da release: usa a da versão ou gera a partir das features
        if not v_desc and features:
            titles = [f["title"] for f in features[:4]]
            v_desc = ", ".join(titles) + ("." if not titles[-1].endswith(".") else "")

        # Serializa cada feature
        features_js = []
        for f in features:
            ctx   = js_str(f["contexto"] or "Não informado.")
            feito = js_str(f["feito"]    or "Não informado.")
            imp   = js_str(f["impacto"]  or "Não informado.")
            title = js_str(f["title"])
            key   = f["jiraKey"]
            features_js.append(
                f"      {{title:{title},jiraKey:'{key}',"
                f"contexto:{ctx},"
                f"feito:{feito},"
                f"impacto:{imp}}}"
            )

        features_block = ",\n".join(features_js)

        lines.append(f"""  {{
    year:{year}, id:'{vid}', name:{js_str(vname)}, releaseDate:{js_str(release_date)}, released:{'true' if released else 'false'},
    desc:{js_str(v_desc)},
    features:[
{features_block}
    ]
  }},""")

    lines.append("];")
    return "\n".join(lines)


def js_str(s: str) -> str:
    """Escapa uma string para uso seguro em JS com aspas simples."""
    s = str(s)
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace("\n", " ").replace("\r", "")
    return f"'{s}'"


# ──────────────────────────────────────────────
# Atualiza o arquivo HTML
# ──────────────────────────────────────────────
def update_html(new_releases_js: str):
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # Substitui o bloco entre "const releases = [" e "];" (primeira ocorrência)
    pattern = r'const releases = \[.*?\];'
    new_content = re.sub(pattern, new_releases_js, content, count=1, flags=re.DOTALL)

    if new_content == content:
        print("⚠️  Nenhuma substituição feita — padrão 'const releases' não encontrado.")
        return False

    # Atualiza timestamp no HTML (comentário no topo do bloco de dados)
    now_br = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    timestamp_comment = f"// Atualizado automaticamente em {now_br}\n"

    new_content = re.sub(
        r'// Atualizado automaticamente em .*?\n',
        timestamp_comment,
        new_content
    )
    # Se ainda não existe o comentário, insere antes do const releases
    if "// Atualizado automaticamente em" not in new_content:
        new_content = new_content.replace(
            "const releases = [",
            timestamp_comment + "const releases = ["
        )

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"✅ {HTML_FILE} atualizado com sucesso.")
    return True


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    print(f"🔍 Buscando versões do projeto {PROJECT}...")
    versions_meta = fetch_versions()
    print(f"   {len(versions_meta)} versões encontradas: {list(versions_meta.keys())}")

    print("🔍 Buscando issues com fixVersion definido...")
    issues = fetch_all_issues(
        jql=f'project = {PROJECT} AND fixVersion is not EMPTY ORDER BY fixVersion DESC, created DESC',
        fields=["summary", "status", "fixVersions", "description", "issuetype"],
    )
    print(f"   {len(issues)} issues com versão encontradas.")

    # Agrupa por versão, filtrando apenas features ativas
    issues_by_version: dict[str, list] = {}
    skipped = 0

    for issue in issues:
        f = issue["fields"]
        itype  = f.get("issuetype", {}).get("name", "").lower()
        status = f.get("status", {}).get("name", "").lower()

        # Filtra por tipo e status
        if itype not in FEATURE_TYPES:
            skipped += 1
            continue
        if status not in ACTIVE_STATUSES:
            skipped += 1
            continue

        summary = f.get("summary", "")
        # Remove prefixos técnicos comuns: [SEMAVY], [Flow], [API Amigo] etc.
        title = re.sub(r'^\[.*?\]\s*', '', summary).strip()

        desc_raw = f.get("description", "")
        contexto, feito, impacto = parse_description_sections(desc_raw)

        for version in f.get("fixVersions", []):
            vname = version["name"]
            if vname not in issues_by_version:
                issues_by_version[vname] = []
            issues_by_version[vname].append({
                "title":    title,
                "jiraKey":  issue["key"],
                "contexto": contexto,
                "feito":    feito,
                "impacto":  impacto,
            })

    print(f"   {skipped} issues ignoradas (tipo/status fora do escopo).")
    print(f"   Versões com features: {list(issues_by_version.keys())}")

    if not issues_by_version:
        print("⚠️  Nenhuma issue encontrada para atualizar. Abortando.")
        return

    print("🔨 Gerando bloco JS de releases...")
    releases_js = build_releases_js(versions_meta, issues_by_version)

    print(f"💾 Atualizando {HTML_FILE}...")
    updated = update_html(releases_js)

    if updated:
        total_features = sum(len(v) for v in issues_by_version.values())
        print(f"\n📊 Resumo:")
        print(f"   Deliveries: {len(issues_by_version)}")
        print(f"   Features:   {total_features}")
        for vname in sorted(issues_by_version.keys(), key=delivery_sort_key, reverse=True):
            print(f"   • {vname}: {len(issues_by_version[vname])} features")
    else:
        print("⚠️  Arquivo não foi modificado.")


if __name__ == "__main__":
    main()

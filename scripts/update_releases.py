#!/usr/bin/env python3
"""
update_releases.py — Amigo Flow Portal
Atualiza APENAS deliveries não lançados (released: false):
  - Atualiza status de features existentes
  - Adiciona features novas (sem contexto/feito/impacto)
  - Adiciona novos deliveries do Jira
  - Deliveries lançados (released: true) são preservados intactos
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

IGNORED_TYPES = {
    "bug","subtarefa","sub-task","testing",
    "impedibug","product issue","atividade"
}

# ── Jira ──────────────────────────────────────────────────────────────────────
def jira_get_versions():
    r = requests.get(
        f"{BASE_URL}/rest/api/3/project/{PROJECT}/versions",
        headers=HEADERS, auth=AUTH, timeout=60
    )
    r.raise_for_status()
    return {
        v["name"]: v for v in r.json()
        if re.search(r'delivery', v["name"], re.IGNORECASE)
    }

def jira_search(jql, fields):
    url    = f"{BASE_URL}/rest/api/3/search/jql"
    issues, start = [], 0
    while True:
        r = requests.get(url, headers=HEADERS, auth=AUTH, timeout=60, params={
            "jql": jql, "fields": ",".join(fields),
            "maxResults": 100, "startAt": start
        })
        r.raise_for_status()
        data  = r.json()
        batch = data.get("issues", [])
        issues.extend(batch)
        if start + 100 >= data.get("total", 0):
            break
        start += 100
    return issues

def dnum(name):
    m = re.search(r'\d+', name)
    return int(m.group()) if m else 0

def js(s):
    if s is None: return 'null'
    s = str(s).replace("\\","\\\\").replace("'","\\'")
    s = s.replace("\n"," ").replace("\r","")
    return f"'{s}'"

# ── Lê / escreve HTML ─────────────────────────────────────────────────────────
def read_html():
    with open(HTML_FILE, encoding="utf-8") as f:
        return f.read()

def write_html(content):
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(content)

# ── Extrai bloco de um delivery do HTML ───────────────────────────────────────
def find_delivery_block(content, vname):
    """
    Retorna (start, end) do objeto JS de um delivery pelo nome.
    Busca 'name:'Delivery N'' e depois acha o {} balanceado.
    """
    pattern = re.compile(
        r'\{\s*\n\s*year:\d+,\s*id:\'[^\']+\',\s*name:' + re.escape(js(vname))
    )
    m = pattern.search(content)
    if not m:
        return None, None
    # percorre para frente contando chaves
    start = m.start()
    depth, i = 0, start
    while i < len(content):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    return None, None

def is_released(block):
    return bool(re.search(r'released:\s*true', block))

# ── Atualiza um delivery não lançado ─────────────────────────────────────────
def update_unreleased_block(block, jira_features, jira_released):
    """
    Dado o bloco JS atual de um delivery:
    1. Atualiza released se o Jira diz que foi lançado
    2. Atualiza status de features existentes
    3. Adiciona features novas no final da lista
    Retorna o bloco atualizado.
    """
    # 1. Atualiza released
    if jira_released:
        block = re.sub(r'released:\s*false', 'released:true', block)
        print("   → marcado como released:true")

    # 2. Para cada feature do Jira, atualiza status ou adiciona
    for feat in jira_features:
        key    = feat["key"]
        status = feat["status"]
        title  = feat["title"]

        if key in block:
            # Atualiza só o status
            old = re.search(rf"jiraKey:'{re.escape(key)}',\s*status:'([^']*)'", block)
            if old and old.group(1) != status:
                block = block.replace(
                    old.group(0),
                    f"jiraKey:'{key}',status:'{status}'"
                )
                print(f"   → {key} status: '{old.group(1)}' → '{status}'")
        else:
            # Feature nova — adiciona antes do fechamento da lista features:[...]
            new_feat = (
                f"\n      {{title:{js(title)},jiraKey:'{key}',"
                f"status:'{status}',"
                f"contexto:'',feito:'',impacto:''}}"
            )
            # Insere antes do último ] da lista features
            insert_point = block.rfind(']')
            if insert_point != -1:
                # Acha o ] da lista features (não o do objeto delivery)
                feat_list_end = block.rfind('\n    ]')
                if feat_list_end != -1:
                    block = block[:feat_list_end] + ',' + new_feat + block[feat_list_end:]
                    print(f"   → {key} adicionado (novo)")

    return block

# ── Cria bloco para delivery novo ─────────────────────────────────────────────
def create_new_delivery_block(vname, vm, jira_features):
    rel   = vm.get("released", False)
    rdate = vm.get("releaseDate", "")
    year  = int(rdate.split("-")[0]) if rdate else 2025
    vid   = f"d{dnum(vname)}"
    desc  = ", ".join(f["title"] for f in jira_features[:4]) if jira_features else ""

    feats_js = ",\n".join(
        f"      {{title:{js(f['title'])},jiraKey:'{f['key']}',"
        f"status:'{f['status']}',"
        f"contexto:'',feito:'',impacto:''}}"
        for f in jira_features
    )

    block = (
        f"  {{\n"
        f"    year:{year}, id:'{vid}', name:{js(vname)}, "
        f"releaseDate:{js(rdate)}, released:{'true' if rel else 'false'},\n"
        f"    desc:{js(desc)},\n"
        f"    features:[\n{feats_js}\n    ]\n"
        f"  }}"
    )
    return block

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # 1. Versões do Jira
    print("🔍 Buscando versões do projeto GREEN...")
    versions = jira_get_versions()
    sorted_names = sorted(versions.keys(), key=dnum)
    print(f"   {len(versions)} deliveries: {sorted_names}")

    # 2. Identifica deliveries não lançados no Jira
    unreleased_jira = {
        name: v for name, v in versions.items()
        if not v.get("released", False)
    }
    print(f"   Não lançados no Jira: {sorted(unreleased_jira.keys(), key=dnum)}")

    # 3. Lê HTML
    print(f"\n📄 Lendo {HTML_FILE}...")
    content = read_html()

    if "const releases = [" not in content:
        print("❌ 'const releases = [' não encontrado no HTML.")
        sys.exit(1)

    changed = False

    # 4. Para cada delivery não lançado, busca issues e atualiza
    for vname in sorted(unreleased_jira.keys(), key=dnum, reverse=True):
        vm = versions[vname]
        jira_released = vm.get("released", False)

        print(f"\n🔄 Processando {vname} (Jira released={jira_released})...")

        # Busca issues do Jira
        issues = jira_search(
            jql=f'project = {PROJECT} AND fixVersion = "{vname}" ORDER BY created ASC',
            fields=["summary","status","issuetype"]
        )
        jira_feats = []
        for iss in issues:
            f     = iss["fields"]
            itype = f.get("issuetype",{}).get("name","").lower().strip()
            if itype in IGNORED_TYPES:
                continue
            title = re.sub(r'^\[.*?\]\s*', '', f.get("summary","")).strip()
            jira_feats.append({
                "key":    iss["key"],
                "title":  title,
                "status": f.get("status",{}).get("name",""),
            })
        print(f"   {len(jira_feats)} features no Jira")

        # Verifica se o delivery já existe no HTML
        start, end = find_delivery_block(content, vname)

        if start is not None:
            # Delivery existe — atualiza
            old_block = content[start:end]
            if is_released(old_block) and not jira_released:
                print(f"   → já marcado como released no HTML, pulando")
                continue
            new_block = update_unreleased_block(old_block, jira_feats, jira_released)
            if new_block != old_block:
                content = content[:start] + new_block + content[end:]
                changed = True
            else:
                print(f"   → sem alterações")
        else:
            # Delivery novo — cria e insere no início do array
            print(f"   → delivery novo, criando bloco...")
            new_block = create_new_delivery_block(vname, vm, jira_feats)
            # Insere depois de "const releases = ["
            insert_after = "const releases = ["
            pos = content.find(insert_after)
            if pos == -1:
                print("❌ Posição de inserção não encontrada.")
                continue
            pos += len(insert_after)
            content = content[:pos] + "\n" + new_block + "," + content[pos:]
            changed = True
            print(f"   → inserido")

    # 5. Também marca como released no HTML os deliveries que o Jira diz que foram lançados
    for vname, vm in versions.items():
        if not vm.get("released", False):
            continue
        start, end = find_delivery_block(content, vname)
        if start is None:
            continue
        block = content[start:end]
        if not is_released(block):
            new_block = re.sub(r'released:\s*false', 'released:true', block)
            content   = content[:start] + new_block + content[end:]
            changed   = True
            print(f"\n✅ {vname} marcado como released:true (Jira confirma lançamento)")

    # 6. Adiciona timestamp e salva
    if changed:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        content = re.sub(
            r'// Atualizado automaticamente em [^\n]+\n',
            '',
            content
        )
        content = content.replace(
            "const releases = [",
            f"// Atualizado automaticamente em {now}\nconst releases = [",
            1
        )
        write_html(content)
        print(f"\n✅ {HTML_FILE} salvo com sucesso!")
    else:
        print("\n✅ Nenhuma alteração necessária.")

if __name__ == "__main__":
    main()

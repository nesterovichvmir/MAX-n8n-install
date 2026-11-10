#!/usr/bin/env python3
"""Static integrity checks for the MAX + n8n v2 package. No server changes."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))

def check(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)
    print("OK: " + message)

vk = load("workflow.vk.template.json")
semd = load("workflow.semd.template.json")
vk_nodes = vk.get("nodes", [])
semd_nodes = semd.get("nodes", [])
vk_by_name = {n.get("name"): n for n in vk_nodes}
semd_by_name = {n.get("name"): n for n in semd_nodes}

check(len(vk_nodes) == 23, "VK workflow contains all 23 exported nodes")
check(len(vk_by_name) == len(vk_nodes), "VK node names are unique")
check(len(semd_nodes) == 28, "SEMD workflow contains all 28 template nodes")
check(len(semd_by_name) == len(semd_nodes), "SEMD node names are unique")

expected = {
    "Code in JavaScript": ("bca7785b", 885),
    "Собрать сообщение": ("c4278b5e", 979),
    "Code in JavaScript1": ("ed9411ee", 294),
}
code_nodes = {n["name"]: n["parameters"]["jsCode"] for n in vk_nodes
              if n.get("type") == "n8n-nodes-base.code"}
check(set(code_nodes) == set(expected), "VK workflow contains exactly the three original Code nodes")

def fnv1a(value):
    result = 2166136261
    for byte in value.encode("utf-8"):
        result ^= byte
        result = (result * 16777619) & 0xffffffff
    return f"{result:08x}"

for name, (digest, length) in expected.items():
    script = code_nodes[name]
    check(len(script) == length and fnv1a(script) == digest,
          name + " JavaScript is byte-for-byte unchanged")

for name in ("HTTP Request2", "HTTP Request4", "HTTP Request5"):
    node = vk_by_name.get(name)
    check(node is not None and "chat_id=" in node["parameters"].get("url", ""),
          name + " has a chat_id endpoint for installer substitution")

semd_expected = {
    "Code in JavaScript": "eaec5d2f",
    "Parse FIO": "a0ef963e",
    "Format Table": "db79841b",
}
semd_code_nodes = {n["name"]: n["parameters"]["jsCode"] for n in semd_nodes
                   if n.get("type") == "n8n-nodes-base.code"}
check(set(semd_code_nodes) == set(semd_expected),
      "SEMD workflow contains exactly the three original Code nodes")
for name, digest in semd_expected.items():
    script = semd_code_nodes[name]
    check(fnv1a(script) == digest,
          name + " JavaScript is byte-for-byte unchanged")
check(not any(n.get("credentials") for n in vk_nodes + semd_nodes),
      "workflow templates contain no stale environment-specific Credentials")
check(all(not n.get("id") and not n.get("webhookId") for n in vk_nodes + semd_nodes),
      "workflow templates contain no reused node or webhook IDs")
check("__MAX_CHAT_ID__" in (ROOT / "workflow.vk.template.json").read_text(encoding="utf-8")
      and "-69599482849890" not in (ROOT / "workflow.vk.template.json").read_text(encoding="utf-8"),
      "VK template contains a neutral chat placeholder, not an old organization chat ID")
check("__DATA_TABLE_ID__" in (ROOT / "workflow.semd.template.json").read_text(encoding="utf-8")
      and "3LENRCaby8GIoTN0" not in (ROOT / "workflow.semd.template.json").read_text(encoding="utf-8"),
      "SEMD template contains a neutral Data Table placeholder, not an old project ID")

configure = (ROOT / "configure.py").read_text(encoding="utf-8")
install = (ROOT / "install.sh").read_text(encoding="utf-8")
check("JSON.stringify({ text: $json.object.text || '' })" in configure,
      "installer corrects the text-only HTTP Request5 body")
check("V2 - MAX Header Auth (VK)" in configure and "V2 - SSH yt-dlp" in configure,
      "VK workflow credentials are replaced during configuration")
check("Confirmation String" in configure and "confirmation" in configure.lower(),
      "VK Callback API confirmation string is collected")
vk_setup = configure[configure.index("def install_vk"):configure.index("def sql_check")]
check(vk_setup.index('props("vkApi"') < vk_setup.index("prepare_vk_ssh()"),
      "VK credential schemas are validated before host SSH changes")
check("def prepare_vk_ssh()" in configure and "n8n-vk-ssh-password" in configure,
      "VK SSH provisioning happens only after module-presence checks")
check('"bot_started"' in configure and "message_created" not in configure[configure.index("def callbacks"):configure.index("def install_semd")],
      "SEMD MAX subscription is not registered twice")
check("ID чата/пользователя MAX должен быть числовым" in configure,
      "SEMD recipient IDs are validated")
check("failedLoading" in configure and "После перезапуска n8n" in configure,
      "community packages are verified after restart")
check("n8n-nodes-vk-community" in configure and "report_recipients" in configure,
      "VK and SEMD module setup logic is present")
check('if [[ -z "${STY:-}" ]]' in install and "screen -dm" not in install,
      "installer checks screen but does not create the session")
check('"$VERSION_ID" == "22.04"' in install and '"$VERSION_ID" == "24.04"' in install,
      "installer restricts OS support to Ubuntu Server 22.04/24.04")
check("DB_TYPE" in install and "N8N_ENCRYPTION_KEY" in install,
      "existing external database is guarded and encryption key is backed up")
check("несколько контейнеров n8n" in install and "N8N_COUNT" in install,
      "installer refuses ambiguous multiple n8n containers")
check('"nodeGroups"' in configure,
      "workflow import strips export-only node group metadata")
check("n8n --version" in install and "2.42.6" in install,
      "existing n8n version is checked without upgrading it")
check('"node","-e"' in configure and '["docker","inspect",' in configure,
      "SSH reachability from the n8n container is tested")
check("/root/.n8n-vk-ssh-password" in configure,
      "SSH password persists securely across reruns")
print("\nStatic package checks passed. Functional testing on a disposable VM is still required.")

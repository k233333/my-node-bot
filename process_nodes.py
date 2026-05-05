import os
import requests
import base64
import time
import re
import smtplib
import socket
import urllib.parse
import random
import concurrent.futures
import json
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header


# ============================================================
# Node-Master：GitHub Actions 安全版节点聚合体检系统
#
# 核心策略：
# 1. 主池优先，大比例保留
# 2. 池2/池3/池4 少量进入备用池
# 3. GitHub Actions 只做粗筛：格式、去重、TCP 端口连通
# 4. 本地 Clash Verge 负责真实 URL-Test 测速
# 5. Clash YAML 内分组：
#    - ♻️ 主池自动选择
#    - 🧪 备用池自动选择
#    - 🚀 节点选择
#
# 输出文件：
# - sub.txt
# - clash.yaml
# - report.html
# - nodes.json
# ============================================================


# ====================== 环境变量 ======================

PRIMARY_SUB_URL = os.environ.get("SUB_URL", "").strip()

EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "").strip()
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "").strip()
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "").strip()

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.163.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SEND_EMAIL = os.environ.get("SEND_EMAIL", "true").lower() == "true"


# ====================== GitHub Actions 安全限制 ======================
# 不建议设置太大，避免 Actions 跑太久。

TOTAL_LIMIT = int(os.environ.get("TOTAL_LIMIT", "320"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "16"))
TCP_TIMEOUT = float(os.environ.get("TCP_TIMEOUT", "2.0"))
ALIVE_LIMIT_MS = int(os.environ.get("ALIVE_LIMIT_MS", "3000"))

REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "10"))
MAX_SOURCE_LINES = int(os.environ.get("MAX_SOURCE_LINES", "2500"))

MAX_IPINFO_LOOKUPS = int(os.environ.get("MAX_IPINFO_LOOKUPS", "180"))
IPINFO_SLEEP_SEC = float(os.environ.get("IPINFO_SLEEP_SEC", "0.35"))


# ====================== 池子策略 ======================
# 这里是重点：主池保留多，公开池只少量补充。

POOLS = [
    {
        "name": "主池",
        "url": PRIMARY_SUB_URL,
        "role": "primary",
        "fetch_limit": int(os.environ.get("PRIMARY_FETCH_LIMIT", "240")),
        "final_limit": int(os.environ.get("PRIMARY_FINAL_LIMIT", "180")),
    },
    {
        "name": "池2",
        "url": "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/vless_configs.txt",
        "role": "backup",
        "fetch_limit": int(os.environ.get("POOL2_FETCH_LIMIT", "45")),
        "final_limit": int(os.environ.get("POOL2_FINAL_LIMIT", "15")),
    },
    {
        "name": "池3",
        "url": "https://raw.githubusercontent.com/F0rc3Run/F0rc3Run/main/splitted-by-protocol/vless.txt",
        "role": "backup",
        "fetch_limit": int(os.environ.get("POOL3_FETCH_LIMIT", "45")),
        "final_limit": int(os.environ.get("POOL3_FINAL_LIMIT", "15")),
    },
    {
        "name": "池4",
        "url": "https://raw.githubusercontent.com/NiREvil/vless/main/sub/vless.txt",
        "role": "backup",
        "fetch_limit": int(os.environ.get("POOL4_FETCH_LIMIT", "45")),
        "final_limit": int(os.environ.get("POOL4_FINAL_LIMIT", "15")),
    },
]

BACKUP_VMESS_POOL = "https://raw.githubusercontent.com/aiboboxx/v2rayfree/main/v2"


# ====================== 地区映射 ======================

COUNTRY_CODE_MAP = {
    "US": "美国",
    "JP": "日本",
    "SG": "新加坡",
    "HK": "香港",
    "TW": "台湾",
    "KR": "韩国",
    "GB": "英国",
    "CA": "加拿大",
    "RU": "俄罗斯",
    "MY": "马来西亚",
    "DE": "德国",
    "FR": "法国",
    "NL": "荷兰",
    "AU": "澳大利亚",
    "IN": "印度",
    "BR": "巴西",
    "TR": "土耳其",
    "VN": "越南",
    "TH": "泰国",
    "ID": "印度尼西亚",
    "PH": "菲律宾",
}

FLAG_MAP = {
    "美国": "🇺🇸",
    "香港": "🇭🇰",
    "台湾": "🇨🇳",
    "日本": "🇯🇵",
    "新加坡": "🇸🇬",
    "韩国": "🇰🇷",
    "英国": "🇬🇧",
    "加拿大": "🇨🇦",
    "俄罗斯": "🇷🇺",
    "马来西亚": "🇲🇾",
    "德国": "🇩🇪",
    "法国": "🇫🇷",
    "荷兰": "🇳🇱",
    "澳大利亚": "🇦🇺",
    "印度": "🇮🇳",
    "巴西": "🇧🇷",
}

DATACENTER_KEYWORDS = [
    "cloudflare",
    "amazon",
    "aws",
    "google",
    "digitalocean",
    "microsoft",
    "azure",
    "oracle",
    "alibaba",
    "tencent",
    "cdn",
    "ovh",
    "hetzner",
    "linode",
    "vultr",
    "host",
    "hosting",
    "data center",
    "datacenter",
    "colo",
    "leaseweb",
    "akamai",
    "fastly",
]


# ====================== 基础工具 ======================

def safe_print(msg):
    print(str(msg), flush=True)


def now_cn_str():
    cn_tz = timezone(timedelta(hours=8))
    return datetime.now(cn_tz).strftime("%Y-%m-%d %H:%M:%S")


def get_pages_base_url():
    if GITHUB_REPOSITORY and "/" in GITHUB_REPOSITORY:
        user, repo = GITHUB_REPOSITORY.split("/", 1)
        return f"https://{user}.github.io/{repo}"
    return ""


def fetch_text(url):
    headers = {
        "User-Agent": "Mozilla/5.0 Node-Master/3.0",
        "Accept": "*/*",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text.strip()


def try_base64_decode(text):
    raw = text.strip()

    if "://" in raw[:300]:
        return raw

    try:
        raw_fix = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(raw_fix).decode("utf-8", errors="ignore")
        if "://" in decoded:
            return decoded
    except Exception:
        pass

    return raw


def fetch_and_decode(url):
    if not url:
        return []

    try:
        text = fetch_text(url)
        text = try_base64_decode(text)

        links = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "://" in line:
                links.append(line)

        if len(links) > MAX_SOURCE_LINES:
            links = random.sample(links, MAX_SOURCE_LINES)

        safe_print(f"源拉取成功：{url}，读取节点 {len(links)} 个")
        return links

    except Exception as e:
        safe_print(f"源拉取失败：{url}，原因：{e}")
        return []


def normalize_link(link):
    return link.strip().replace("\r", "").replace("\n", "")


def strip_fragment(link):
    return link.split("#", 1)[0].strip()


def parse_vmess_link(link):
    try:
        raw = link[8:]
        raw += "=" * (-len(raw) % 4)
        data = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
        host = data.get("add")
        port = int(data.get("port"))
        name = data.get("ps", host)
        return {
            "scheme": "vmess",
            "host": host,
            "port": port,
            "name": name,
            "raw": data,
        }
    except Exception:
        return None


def extract_node_info(link):
    """
    提取节点基本信息，用于去重和 TCP 测试。
    支持：
    - vless://
    - trojan://
    - vmess://
    - ss:// 的一部分标准格式
    """
    link = normalize_link(link)

    if link.lower().startswith("vmess://"):
        vm = parse_vmess_link(link)
        if not vm:
            return None

        host = vm["host"]
        port = vm["port"]
        base_link = strip_fragment(link)

        if not host or not port:
            return None

        return {
            "scheme": "vmess",
            "host": host,
            "port": port,
            "base_link": base_link,
            "name": vm.get("name") or host,
            "dedup_key": f"vmess|{host}|{port}|{base_link[:100]}",
        }

    try:
        parsed = urllib.parse.urlparse(link)
        scheme = parsed.scheme.lower()

        if scheme not in ["vless", "trojan", "ss"]:
            return None

        host = parsed.hostname
        port = parsed.port

        if not host or not port:
            return None

        base_link = strip_fragment(link)
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else host
        username = parsed.username or ""

        return {
            "scheme": scheme,
            "host": host,
            "port": int(port),
            "base_link": base_link,
            "name": name,
            "dedup_key": f"{scheme}|{host}|{port}|{username[:40]}",
        }

    except Exception:
        return None


def resolve_host(host):
    try:
        socket.inet_aton(host)
        return host
    except Exception:
        pass

    try:
        return socket.gethostbyname(host)
    except Exception:
        return host


def test_tcp_ping(item):
    host = item["host"]
    port = item["port"]

    start_time = time.time()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TCP_TIMEOUT)
            s.connect((host, port))

        ping_ms = int((time.time() - start_time) * 1000)

        return {
            **item,
            "alive": True,
            "ping_ms": ping_ms,
        }

    except Exception:
        return {
            **item,
            "alive": False,
            "ping_ms": 9999,
        }


def get_ip_info(ip):
    try:
        headers = {"User-Agent": "Node-Master/3.0"}
        resp = requests.get(f"https://ipinfo.io/{ip}/json", headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass

    return {}


def country_flag(country_zh):
    return next((v for k, v in FLAG_MAP.items() if k in country_zh), "🌐")


def classify_quality(ping_ms):
    if ping_ms <= 300:
        return "S级"
    if ping_ms <= 800:
        return "A级"
    if ping_ms <= 1500:
        return "B级"
    if ping_ms <= 3000:
        return "C级"
    return "淘汰"


def classify_node_type(org):
    org_l = (org or "").lower()
    if any(k in org_l for k in DATACENTER_KEYWORDS):
        return "🏢 机房"
    return "🏠 家宽"


def make_node_name(pool_name, flag, country_zh, node_type, quality, ping_ms, index):
    """
    注意：
    这里的 ping_ms 是 GitHub 云端 TCP 粗筛延迟，不是你本地 Clash 实际延迟。
    所以名字里写“云测”，避免误导。
    """
    return f"张牛13 [{pool_name}] {flag} {country_zh} | {node_type} | {quality} | 云测{ping_ms}ms #{index}"


def encode_name_to_link(base_link, name):
    return f"{base_link}#{urllib.parse.quote(name)}"


# ====================== 简易 YAML 输出器 ======================
# 不依赖 pyyaml，适合 GitHub Actions。

def yaml_quote(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_dump(obj, indent=0):
    space = " " * indent
    lines = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{space}{k}:")
                lines.extend(yaml_dump(v, indent + 2))
            else:
                lines.append(f"{space}{k}: {yaml_quote(v)}")
        return lines

    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                lines.append(f"{space}-")
                lines.extend(yaml_dump(item, indent + 2))
            elif isinstance(item, list):
                lines.append(f"{space}-")
                lines.extend(yaml_dump(item, indent + 2))
            else:
                lines.append(f"{space}- {yaml_quote(item)}")
        return lines

    return [f"{space}{yaml_quote(obj)}"]


# ====================== Clash 节点转换 ======================

def convert_vless_to_clash(link):
    try:
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme.lower() != "vless":
            return None

        uuid = parsed.username
        server = parsed.hostname
        port = parsed.port
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else server

        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        proxy = {
            "name": name,
            "type": "vless",
            "server": server,
            "port": port,
            "uuid": uuid,
            "udp": True,
            "tls": q.get("security") in ["tls", "reality"],
            "network": q.get("type", "tcp"),
            "encryption": q.get("encryption", "none"),
        }

        if q.get("flow"):
            proxy["flow"] = q.get("flow")

        if q.get("sni"):
            proxy["servername"] = q.get("sni")

        if q.get("fp"):
            proxy["client-fingerprint"] = q.get("fp")

        if q.get("security") == "reality":
            reality_opts = {}

            if q.get("pbk"):
                reality_opts["public-key"] = q.get("pbk")

            if q.get("sid"):
                reality_opts["short-id"] = q.get("sid")

            if reality_opts:
                proxy["reality-opts"] = reality_opts

        if q.get("type") == "ws":
            proxy["ws-opts"] = {
                "path": q.get("path", "/"),
                "headers": {
                    "Host": q.get("host", q.get("sni", server))
                }
            }

        if q.get("type") == "grpc":
            proxy["grpc-opts"] = {
                "grpc-service-name": q.get("serviceName", "")
            }

        return proxy

    except Exception as e:
        safe_print(f"VLESS 转 Clash 失败：{e}")
        return None


def convert_trojan_to_clash(link):
    try:
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme.lower() != "trojan":
            return None

        password = parsed.username
        server = parsed.hostname
        port = parsed.port
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else server

        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        proxy = {
            "name": name,
            "type": "trojan",
            "server": server,
            "port": port,
            "password": password,
            "udp": True,
            "sni": q.get("sni", server),
            "skip-cert-verify": q.get("allowInsecure", "0") in ["1", "true", "True"],
        }

        if q.get("type") == "ws":
            proxy["network"] = "ws"
            proxy["ws-opts"] = {
                "path": q.get("path", "/"),
                "headers": {
                    "Host": q.get("host", q.get("sni", server))
                }
            }

        return proxy

    except Exception as e:
        safe_print(f"Trojan 转 Clash 失败：{e}")
        return None


def convert_vmess_to_clash(link):
    try:
        raw = link[8:]
        raw += "=" * (-len(raw) % 4)
        data = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))

        proxy = {
            "name": data.get("ps") or data.get("add"),
            "type": "vmess",
            "server": data.get("add"),
            "port": int(data.get("port")),
            "uuid": data.get("id"),
            "alterId": int(data.get("aid", 0)),
            "cipher": "auto",
            "udp": True,
            "tls": data.get("tls") == "tls",
            "network": data.get("net", "tcp"),
        }

        if data.get("sni"):
            proxy["servername"] = data.get("sni")

        if data.get("net") == "ws":
            proxy["ws-opts"] = {
                "path": data.get("path", "/"),
                "headers": {
                    "Host": data.get("host", data.get("add"))
                }
            }

        return proxy

    except Exception as e:
        safe_print(f"VMESS 转 Clash 失败：{e}")
        return None


def convert_to_clash_proxy(link):
    l = link.lower()

    if l.startswith("vless://"):
        return convert_vless_to_clash(link)

    if l.startswith("trojan://"):
        return convert_trojan_to_clash(link)

    if l.startswith("vmess://"):
        return convert_vmess_to_clash(link)

    return None


def unique_proxy_name(name, seen_names):
    if name not in seen_names:
        seen_names.add(name)
        return name

    i = 2
    original = name

    while True:
        new_name = f"{original} #{i}"
        if new_name not in seen_names:
            seen_names.add(new_name)
            return new_name
        i += 1


def generate_clash_yaml(enriched_nodes):
    proxies = []
    primary_proxy_names = []
    backup_proxy_names = []
    seen_names = set()

    for node in enriched_nodes:
        proxy = convert_to_clash_proxy(node["new_link"])

        if not proxy:
            continue

        name = proxy.get("name") or node["new_name"] or node["host"]
        name = unique_proxy_name(name, seen_names)
        proxy["name"] = name

        proxies.append(proxy)

        if node["pool_role"] == "primary":
            primary_proxy_names.append(name)
        else:
            backup_proxy_names.append(name)

    # 防止组为空导致 Clash 报错
    if not primary_proxy_names and backup_proxy_names:
        primary_proxy_names = backup_proxy_names[:]

    if not backup_proxy_names and primary_proxy_names:
        backup_proxy_names = primary_proxy_names[:]

    all_proxy_names = primary_proxy_names + [
        x for x in backup_proxy_names if x not in primary_proxy_names
    ]

    if not all_proxy_names:
        safe_print("没有可转换为 Clash 的节点，跳过 clash.yaml")
        return {
            "clash_count": 0,
            "primary_clash_count": 0,
            "backup_clash_count": 0,
        }

    config = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "global-client-fingerprint": "chrome",

        "profile": {
            "store-selected": True,
            "store-fake-ip": True,
        },

        "proxies": proxies,

        "proxy-groups": [
            {
                "name": "🚀 节点选择",
                "type": "select",
                "proxies": [
                    "♻️ 主池自动选择",
                    "🧪 备用池自动选择",
                    "DIRECT",
                ] + primary_proxy_names[:20] + backup_proxy_names[:10],
            },
            {
                "name": "♻️ 主池自动选择",
                "type": "url-test",
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 80,
                "proxies": primary_proxy_names,
            },
            {
                "name": "🧪 备用池自动选择",
                "type": "url-test",
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 120,
                "proxies": backup_proxy_names,
            },
            {
                "name": "🤖 AI专用",
                "type": "select",
                "proxies": [
                    "♻️ 主池自动选择",
                    "🧪 备用池自动选择",
                    "🚀 节点选择",
                    "DIRECT",
                ],
            },
            {
                "name": "🎬 流媒体",
                "type": "select",
                "proxies": [
                    "♻️ 主池自动选择",
                    "🧪 备用池自动选择",
                    "🚀 节点选择",
                    "DIRECT",
                ],
            },
            {
                "name": "🎮 游戏",
                "type": "select",
                "proxies": [
                    "DIRECT",
                    "♻️ 主池自动选择",
                    "🧪 备用池自动选择",
                    "🚀 节点选择",
                ],
            },
            {
                "name": "🌍 国外网站",
                "type": "select",
                "proxies": [
                    "♻️ 主池自动选择",
                    "🧪 备用池自动选择",
                    "🚀 节点选择",
                    "DIRECT",
                ],
            },
        ],

        "rules": [
            "DOMAIN-SUFFIX,openai.com,🤖 AI专用",
            "DOMAIN-SUFFIX,chatgpt.com,🤖 AI专用",
            "DOMAIN-SUFFIX,oaistatic.com,🤖 AI专用",
            "DOMAIN-SUFFIX,oaiusercontent.com,🤖 AI专用",
            "DOMAIN-SUFFIX,anthropic.com,🤖 AI专用",
            "DOMAIN-SUFFIX,claude.ai,🤖 AI专用",
            "DOMAIN-SUFFIX,gemini.google.com,🤖 AI专用",

            "DOMAIN-SUFFIX,youtube.com,🎬 流媒体",
            "DOMAIN-SUFFIX,netflix.com,🎬 流媒体",
            "DOMAIN-SUFFIX,disneyplus.com,🎬 流媒体",

            "DOMAIN-SUFFIX,telegram.org,🌍 国外网站",
            "DOMAIN-SUFFIX,t.me,🌍 国外网站",
            "DOMAIN-SUFFIX,google.com,🌍 国外网站",
            "DOMAIN-SUFFIX,github.com,🌍 国外网站",

            "DOMAIN-SUFFIX,xboxlive.com,🎮 游戏",
            "DOMAIN-SUFFIX,callofduty.com,🎮 游戏",
            "DOMAIN-SUFFIX,activision.com,🎮 游戏",

            "GEOSITE,category-ads-all,REJECT",
            "GEOIP,CN,DIRECT",
            "MATCH,🚀 节点选择",
        ],
    }

    yaml_text = "\n".join(yaml_dump(config)) + "\n"

    with open("clash.yaml", "w", encoding="utf-8") as f:
        f.write(yaml_text)

    safe_print(
        f"clash.yaml 已生成，总节点 {len(proxies)}，主池 {len(primary_proxy_names)}，备用池 {len(backup_proxy_names)}"
    )

    return {
        "clash_count": len(proxies),
        "primary_clash_count": len(primary_proxy_names),
        "backup_clash_count": len(backup_proxy_names),
    }


# ====================== 邮件报告 ======================

def build_report_html(stats, classified_nodes, urls, backup_vmess_links):
    update_time = now_cn_str()

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#222;line-height:1.6;">
        <h2>🚀 聚合订阅更新成功</h2>

        <p>更新时间：<b>{update_time}</b></p>

        <div style="background:#f8fafc;border-left:4px solid #2563eb;padding:12px 14px;margin:12px 0;">
            <p style="margin:4px 0;">抓取节点：<b>{stats['raw_count']}</b> 个</p>
            <p style="margin:4px 0;">格式有效去重：<b>{stats['valid_count']}</b> 个</p>
            <p style="margin:4px 0;">云端 TCP 可连接：<b>{stats['alive_count']}</b> 个</p>
            <p style="margin:4px 0;">最终保留：<b>{stats['final_count']}</b> 个</p>
            <p style="margin:4px 0;">主池保留：<b>{stats['primary_final_count']}</b> 个</p>
            <p style="margin:4px 0;">备用池保留：<b>{stats['backup_final_count']}</b> 个</p>
            <p style="margin:4px 0;">Clash 可导入：<b>{stats['clash_count']}</b> 个</p>
        </div>

        <div style="background:#fff7ed;border-left:4px solid #f97316;padding:12px 14px;margin:12px 0;">
            <b>重要说明：</b><br>
            GitHub Actions 只能做云端 TCP 粗筛，不能代表你本地真实速度。<br>
            本地真实速度以 Clash Verge 的 <b>♻️ 主池自动选择</b> 和 <b>🧪 备用池自动选择</b> 测速结果为准。<br>
            默认建议使用：<b>🚀 节点选择 → ♻️ 主池自动选择</b>。
        </div>

        <h3>📊 云端质量分级</h3>
        <ul>
            <li>S级 0-300ms：<b>{stats['quality_count'].get('S级', 0)}</b> 个</li>
            <li>A级 301-800ms：<b>{stats['quality_count'].get('A级', 0)}</b> 个</li>
            <li>B级 801-1500ms：<b>{stats['quality_count'].get('B级', 0)}</b> 个</li>
            <li>C级 1501-3000ms：<b>{stats['quality_count'].get('C级', 0)}</b> 个</li>
        </ul>

        <h3>🔗 订阅地址</h3>
        <div style="background:#f4f4f4;padding:12px;margin:12px 0;border-left:4px solid #27ae60;">
            <p style="margin:6px 0;"><b>普通订阅：</b><a href="{urls['sub']}">{urls['sub']}</a></p>
            <p style="margin:6px 0;"><b>Clash Verge：</b><a href="{urls['clash']}">{urls['clash']}</a></p>
            <p style="margin:6px 0;"><b>体检报告：</b><a href="{urls['report']}">{urls['report']}</a></p>
            <p style="margin:6px 0;"><b>节点数据：</b><a href="{urls['json']}">{urls['json']}</a></p>
        </div>

        <h3>🌍 地区分类</h3>
    """

    for country, items in classified_nodes.items():
        flag = country_flag(country)
        html += f"""
        <h4>{flag} {country} ({len(items)} 个)</h4>
        <ul style="font-size:13px;">
        """

        for item in items[:80]:
            role_text = "主池" if item["pool_role"] == "primary" else "备用"
            html += (
                f"<li>"
                f"[{role_text}] [{item['pool_name']}] {item['node_type']} "
                f"{item['quality']} 云测 {item['ping_ms']}ms "
                f"- IP: {item['ip']} "
                f"- ISP: {item['org']}"
                f"</li>"
            )

        if len(items) > 80:
            html += f"<li>还有 {len(items) - 80} 个未在邮件中展开，可查看 nodes.json。</li>"

        html += "</ul>"

    if backup_vmess_links:
        backup_text = "\n".join(backup_vmess_links)
        html += f"""
        <hr style="border:1px solid #eee;margin:20px 0;">
        <h3 style="color:#e74c3c;">🆘 紧急灾备 VMESS 节点</h3>
        <p style="font-size:12px;color:#666;">
            随机抽取 20 个 VMESS 节点。订阅失效时可复制到支持 VMESS 的客户端临时使用。
        </p>
        <textarea style="width:100%;height:130px;font-size:10px;background:#f8f9fa;border:1px solid #ced4da;padding:8px;border-radius:4px;" readonly>{backup_text}</textarea>
        """

    html += """
        <hr style="border:1px solid #eee;margin:20px 0;">
        <p style="font-size:12px;color:#777;">
            Node-Master 自动生成。云端延迟仅用于粗筛，不代表本地实际体验。
        </p>
    </div>
    """

    return html


def send_html_email(subject, html_content):
    if not SEND_EMAIL:
        safe_print("SEND_EMAIL=false，跳过邮件发送。")
        return False

    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVER:
        safe_print("邮件环境变量不完整，跳过邮件发送。")
        return False

    msg = MIMEText(html_content, "html", "utf-8")
    msg["From"] = Header("Node-Master", "utf-8")
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = Header(subject, "utf-8")

    try:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        server.quit()
        safe_print("邮件发送成功。")
        return True

    except Exception as e:
        safe_print(f"邮件发送失败：{e}")
        return False


# ====================== 主流程 ======================

def main():
    safe_print("开始执行 Node-Master 主池优先版...")
    safe_print(f"当前时间：{now_cn_str()}")
    safe_print(f"MAX_WORKERS={MAX_WORKERS}, TCP_TIMEOUT={TCP_TIMEOUT}, ALIVE_LIMIT_MS={ALIVE_LIMIT_MS}")

    valid_pools = [p for p in POOLS if p.get("url")]

    if not valid_pools:
        safe_print("没有可用节点源，请检查 SUB_URL。")
        return

    # 1. 分池抓取
    all_raw_nodes = []

    for pool in valid_pools:
        links = fetch_and_decode(pool["url"])

        if not links:
            continue

        fetch_limit = pool["fetch_limit"]

        if len(links) > fetch_limit:
            # 主池尽量保留前面的稳定顺序，备用池随机抽样
            if pool["role"] == "primary":
                links = links[:fetch_limit]
            else:
                links = random.sample(links, fetch_limit)

        for idx, link in enumerate(links):
            all_raw_nodes.append({
                "pool_name": pool["name"],
                "pool_role": pool["role"],
                "pool_final_limit": pool["final_limit"],
                "pool_source_index": idx,
                "link": normalize_link(link),
            })

    raw_count = len(all_raw_nodes)
    safe_print(f"抓取完成，原始节点数：{raw_count}")

    if raw_count > TOTAL_LIMIT:
        # 理论上不会超过太多，这里再兜底一次
        primary_raw = [n for n in all_raw_nodes if n["pool_role"] == "primary"]
        backup_raw = [n for n in all_raw_nodes if n["pool_role"] != "primary"]
        remain = max(0, TOTAL_LIMIT - len(primary_raw))

        if len(backup_raw) > remain:
            backup_raw = random.sample(backup_raw, remain)

        all_raw_nodes = primary_raw + backup_raw
        raw_count = len(all_raw_nodes)
        safe_print(f"触发 TOTAL_LIMIT 兜底，调整后原始节点数：{raw_count}")

    # 2. 解析 + 去重
    valid_nodes = []
    seen = set()

    for item in all_raw_nodes:
        info = extract_node_info(item["link"])

        if not info:
            continue

        dedup_key = info["dedup_key"]

        if dedup_key in seen:
            continue

        seen.add(dedup_key)

        valid_nodes.append({
            **item,
            **info,
        })

    valid_count = len(valid_nodes)
    safe_print(f"格式有效并去重后：{valid_count}")

    if not valid_nodes:
        safe_print("没有格式有效节点，结束。")
        return

    # 3. TCP 粗筛
    safe_print("开始云端 TCP 粗筛...")
    tested_nodes = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for res in executor.map(test_tcp_ping, valid_nodes):
            tested_nodes.append(res)

    alive_nodes = [
        n for n in tested_nodes
        if n["alive"] and n["ping_ms"] < ALIVE_LIMIT_MS
    ]

    safe_print(f"云端 TCP 可连接节点：{len(alive_nodes)}")

    # 4. 按池子选择最终节点
    # 主池：保留较多，按源顺序优先，兼顾云端能连
    # 备用池：少量保留，按云端 TCP 延迟粗排
    final_nodes = []

    for pool in valid_pools:
        pool_name = pool["name"]
        pool_role = pool["role"]
        final_limit = pool["final_limit"]

        pool_alive = [n for n in alive_nodes if n["pool_name"] == pool_name]

        if pool_role == "primary":
            pool_alive.sort(key=lambda x: (x["pool_source_index"], x["ping_ms"]))
        else:
            pool_alive.sort(key=lambda x: x["ping_ms"])

        selected = pool_alive[:final_limit]
        final_nodes.extend(selected)

        safe_print(
            f"{pool_name}：可连接 {len(pool_alive)} 个，最终保留 {len(selected)} 个"
        )

    if not final_nodes:
        safe_print("没有最终可用节点，结束。")
        return

    # 5. IP 信息查询 + 分类 + 改名
    ip_cache = {}
    resolved_cache = {}

    enriched_nodes = []
    classified_nodes = {}
    final_subscription_links = []

    safe_print("开始 IP 信息查询和节点分类...")

    for index, node in enumerate(final_nodes, start=1):
        host = node["host"]

        if host not in resolved_cache:
            resolved_cache[host] = resolve_host(host)

        ip = resolved_cache[host]

        if ip not in ip_cache:
            if len(ip_cache) < MAX_IPINFO_LOOKUPS:
                time.sleep(IPINFO_SLEEP_SEC)
                ip_cache[ip] = get_ip_info(ip)
            else:
                ip_cache[ip] = {}

        info = ip_cache.get(ip, {})

        country_code = info.get("country", "未知")
        country_zh = COUNTRY_CODE_MAP.get(country_code, country_code)
        org = info.get("org", "未知ISP")

        flag = country_flag(country_zh)
        node_type = classify_node_type(org)
        quality = classify_quality(node["ping_ms"])

        new_name = make_node_name(
            node["pool_name"],
            flag,
            country_zh,
            node_type,
            quality,
            node["ping_ms"],
            index,
        )

        new_link = encode_name_to_link(node["base_link"], new_name)

        enriched = {
            **node,
            "ip": ip,
            "country_code": country_code,
            "country_zh": country_zh,
            "flag": flag,
            "org": org,
            "node_type": node_type,
            "quality": quality,
            "new_name": new_name,
            "new_link": new_link,
        }

        enriched_nodes.append(enriched)
        final_subscription_links.append(new_link)

        classified_nodes.setdefault(country_zh, []).append(enriched)

    # 每个地区内部按主池优先 + 延迟排序
    for country in classified_nodes:
        classified_nodes[country].sort(
            key=lambda x: (0 if x["pool_role"] == "primary" else 1, x["ping_ms"])
        )

    # 地区按数量排序
    classified_nodes = dict(
        sorted(classified_nodes.items(), key=lambda kv: len(kv[1]), reverse=True)
    )

    # 6. 生成普通订阅 sub.txt
    final_sub_content = "\n".join(final_subscription_links)
    new_sub_base64 = base64.b64encode(final_sub_content.encode("utf-8")).decode("utf-8")

    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write(new_sub_base64)

    safe_print(f"sub.txt 已生成，节点数：{len(final_subscription_links)}")

    # 7. 生成 Clash YAML
    clash_stats = generate_clash_yaml(enriched_nodes)

    # 8. 生成 nodes.json
    json_nodes = []

    for n in enriched_nodes:
        json_nodes.append({
            "pool_name": n["pool_name"],
            "pool_role": n["pool_role"],
            "scheme": n["scheme"],
            "host": n["host"],
            "port": n["port"],
            "ip": n["ip"],
            "country_code": n["country_code"],
            "country_zh": n["country_zh"],
            "org": n["org"],
            "node_type": n["node_type"],
            "quality": n["quality"],
            "cloud_tcp_ping_ms": n["ping_ms"],
            "name": n["new_name"],
        })

    with open("nodes.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": now_cn_str(),
                "note": "cloud_tcp_ping_ms 是 GitHub Actions 云端 TCP 粗筛延迟，不代表本地 Clash 实际测速。",
                "total": len(json_nodes),
                "nodes": json_nodes,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    safe_print("nodes.json 已生成。")

    # 9. 灾备 VMESS
    backup_vmess_links = []

    try:
        safe_print("开始抓取灾备 VMESS 节点...")
        raw_backup = fetch_and_decode(BACKUP_VMESS_POOL)
        vmess_only = [
            link for link in raw_backup
            if link.lower().startswith("vmess://")
        ]

        if vmess_only:
            backup_vmess_links = random.sample(vmess_only, min(20, len(vmess_only)))

    except Exception as e:
        safe_print(f"灾备 VMESS 抓取异常：{e}")

    # 10. 统计信息
    quality_count = {}
    for n in enriched_nodes:
        quality_count[n["quality"]] = quality_count.get(n["quality"], 0) + 1

    primary_final_count = len([n for n in enriched_nodes if n["pool_role"] == "primary"])
    backup_final_count = len([n for n in enriched_nodes if n["pool_role"] != "primary"])

    stats = {
        "raw_count": raw_count,
        "valid_count": valid_count,
        "alive_count": len(alive_nodes),
        "final_count": len(enriched_nodes),
        "primary_final_count": primary_final_count,
        "backup_final_count": backup_final_count,
        "quality_count": quality_count,
        **clash_stats,
    }

    pages_base = get_pages_base_url()

    if pages_base:
        urls = {
            "sub": f"{pages_base}/sub.txt",
            "clash": f"{pages_base}/clash.yaml",
            "report": f"{pages_base}/report.html",
            "json": f"{pages_base}/nodes.json",
        }
    else:
        urls = {
            "sub": "未获取到 GitHub Pages 地址",
            "clash": "未获取到 GitHub Pages 地址",
            "report": "未获取到 GitHub Pages 地址",
            "json": "未获取到 GitHub Pages 地址",
        }

    # 11. 生成 HTML 报告
    report_html = build_report_html(stats, classified_nodes, urls, backup_vmess_links)

    with open("report.html", "w", encoding="utf-8") as f:
        f.write(report_html)

    safe_print("report.html 已生成。")

    # 12. 发送邮件
    subject = (
        f"自动化聚合节点体检完成｜"
        f"主池 {primary_final_count}｜"
        f"备用 {backup_final_count}｜"
        f"总计 {len(enriched_nodes)}｜"
        f"{now_cn_str()}"
    )

    send_html_email(subject, report_html)

    safe_print("全部流程完成。")


if __name__ == "__main__":
    main()

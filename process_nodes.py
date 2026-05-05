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
# GitHub Actions 安全版节点聚合体检系统
# 功能：
# 1. 多源订阅抓取
# 2. base64 自动解码
# 3. 节点去重
# 4. TCP 连通性测速
# 5. IP 地区 / ISP 分类
# 6. 家宽 / 机房识别
# 7. 生成 sub.txt 普通订阅
# 8. 生成 clash.yaml，给 Clash Verge / Mihomo 使用
# 9. 生成 report.html 邮件报告
# 10. 生成 nodes.json 结构化数据
# 11. 发送每日 HTML 邮件
# ============================================================


# ====================== 环境变量配置 ======================

PRIMARY_SUB_URL = os.environ.get("SUB_URL", "").strip()

EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "").strip()
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "").strip()
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "").strip()

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()

# 163 邮箱默认配置
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.163.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

# 是否发送邮件：默认发送
SEND_EMAIL = os.environ.get("SEND_EMAIL", "true").lower() == "true"

# ====================== GitHub Actions 安全限制 ======================
# 这些参数是为了避免 GitHub Actions 运行过久、请求过多、输出过大。

TOTAL_LIMIT = int(os.environ.get("TOTAL_LIMIT", "300"))             # 抓取后参与测试的总节点上限
FINAL_LIMIT = int(os.environ.get("FINAL_LIMIT", "220"))             # 最终写入订阅的节点上限
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "16"))              # TCP 测速并发，不建议超过 30
TCP_TIMEOUT = float(os.environ.get("TCP_TIMEOUT", "2.0"))           # 单节点 TCP 超时
ALIVE_LIMIT_MS = int(os.environ.get("ALIVE_LIMIT_MS", "3000"))      # 超过这个延迟淘汰
MAX_IPINFO_LOOKUPS = int(os.environ.get("MAX_IPINFO_LOOKUPS", "180"))  # IP 信息查询上限
IPINFO_SLEEP_SEC = float(os.environ.get("IPINFO_SLEEP_SEC", "0.35"))   # 查询 IP 信息间隔
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "10"))    # 拉取订阅超时
MAX_SOURCE_LINES = int(os.environ.get("MAX_SOURCE_LINES", "2500"))  # 单个源最多读取多少行节点


# ====================== 节点源 ======================

POOLS = [
    {"name": "主池", "url": PRIMARY_SUB_URL},
    {"name": "池2", "url": "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/vless_configs.txt"},
    {"name": "池3", "url": "https://raw.githubusercontent.com/F0rc3Run/F0rc3Run/main/splitted-by-protocol/vless.txt"},
    {"name": "池4", "url": "https://raw.githubusercontent.com/NiREvil/vless/main/sub/vless.txt"},
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


# ====================== 工具函数 ======================

def now_cn_str():
    cn_tz = timezone(timedelta(hours=8))
    return datetime.now(cn_tz).strftime("%Y-%m-%d %H:%M:%S")


def safe_print(msg):
    print(str(msg), flush=True)


def get_pages_base_url():
    if GITHUB_REPOSITORY and "/" in GITHUB_REPOSITORY:
        user, repo = GITHUB_REPOSITORY.split("/", 1)
        return f"https://{user}.github.io/{repo}"
    return ""


def fetch_text(url):
    headers = {
        "User-Agent": "Mozilla/5.0 Node-Master/2.0",
        "Accept": "*/*",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text.strip()


def try_base64_decode(text):
    raw = text.strip()

    # 如果前 200 个字符已经有协议，认为是明文订阅
    if "://" in raw[:200]:
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

        # 控制单源读取量，避免异常大源拖垮 Actions
        if len(links) > MAX_SOURCE_LINES:
            links = random.sample(links, MAX_SOURCE_LINES)

        safe_print(f"源拉取成功：{url}，读取节点 {len(links)} 个")
        return links

    except Exception as e:
        safe_print(f"源拉取失败：{url}，原因：{e}")
        return []


def strip_fragment(link):
    return link.split("#", 1)[0].strip()


def normalize_link(link):
    return link.strip().replace("\r", "").replace("\n", "")


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
    返回：
    {
        scheme,
        host,
        port,
        base_link,
        name,
        dedup_key
    }
    """
    link = normalize_link(link)

    if link.lower().startswith("vmess://"):
        vm = parse_vmess_link(link)
        if not vm:
            return None
        host = vm["host"]
        port = vm["port"]
        base_link = strip_fragment(link)
        return {
            "scheme": "vmess",
            "host": host,
            "port": port,
            "base_link": base_link,
            "name": vm.get("name") or host,
            "dedup_key": f"vmess|{host}|{port}|{base_link[:80]}",
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
        dedup_key = f"{scheme}|{host}|{port}|{username[:36]}"

        return {
            "scheme": scheme,
            "host": host,
            "port": int(port),
            "base_link": base_link,
            "name": name,
            "dedup_key": dedup_key,
        }
    except Exception:
        return None


def resolve_host(host):
    """
    ipinfo 最好查 IP。
    如果是域名，先解析一个 IP。
    """
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
    pool_name = item["pool_name"]
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
            "ping_ms": ping_ms,
            "alive": True,
        }
    except Exception:
        return {
            **item,
            "ping_ms": 9999,
            "alive": False,
        }


def get_ip_info(ip):
    try:
        headers = {"User-Agent": "Node-Master/2.0"}
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


def make_node_name(pool_name, flag, country_zh, node_type, quality, ping_ms):
    return f"张牛13 [{pool_name}] {flag} {country_zh} | {node_type} | {quality} | ⚡ {ping_ms}ms"


def encode_name_to_link(base_link, name):
    return f"{base_link}#{urllib.parse.quote(name)}"


# ====================== Clash YAML 生成 ======================

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


def generate_clash_yaml(final_links):
    proxies = []
    seen_names = set()

    for link in final_links:
        proxy = convert_to_clash_proxy(link)
        if not proxy:
            continue

        name = proxy.get("name") or proxy.get("server")
        original_name = name
        i = 2
        while name in seen_names:
            name = f"{original_name} #{i}"
            i += 1

        proxy["name"] = name
        seen_names.add(name)
        proxies.append(proxy)

    proxy_names = [p["name"] for p in proxies]

    if not proxy_names:
        safe_print("没有可转换为 Clash 的节点，跳过 clash.yaml")
        return 0

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
                "proxies": ["♻️ 自动选择", "DIRECT"] + proxy_names,
            },
            {
                "name": "♻️ 自动选择",
                "type": "url-test",
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 80,
                "proxies": proxy_names,
            },
            {
                "name": "🤖 AI专用",
                "type": "select",
                "proxies": ["🚀 节点选择", "♻️ 自动选择", "DIRECT"] + proxy_names,
            },
            {
                "name": "🎬 流媒体",
                "type": "select",
                "proxies": ["🚀 节点选择", "♻️ 自动选择", "DIRECT"] + proxy_names,
            },
            {
                "name": "🎮 游戏",
                "type": "select",
                "proxies": ["DIRECT", "🚀 节点选择", "♻️ 自动选择"] + proxy_names,
            },
            {
                "name": "🌍 国外网站",
                "type": "select",
                "proxies": ["🚀 节点选择", "♻️ 自动选择", "DIRECT"] + proxy_names,
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
            "DOMAIN-SUFFIX,google.com,🌍 国外网站",
            "DOMAIN-SUFFIX,youtube.com,🎬 流媒体",
            "DOMAIN-SUFFIX,netflix.com,🎬 流媒体",
            "DOMAIN-SUFFIX,telegram.org,🌍 国外网站",
            "DOMAIN-SUFFIX,t.me,🌍 国外网站",
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

    safe_print(f"clash.yaml 已生成，Clash 节点数：{len(proxies)}")
    return len(proxies)


# ====================== 报告生成 ======================

def build_report_html(stats, classified_nodes, urls, backup_vmess_links):
    update_time = now_cn_str()

    normal_sub_url = urls.get("sub", "")
    clash_url = urls.get("clash", "")
    report_url = urls.get("report", "")
    json_url = urls.get("json", "")

    quality = stats["quality_count"]

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; color: #222; line-height: 1.6;">
        <h2>🚀 聚合订阅更新成功</h2>

        <p>更新时间：<b>{update_time}</b></p>

        <div style="background:#f8fafc;border-left:4px solid #2563eb;padding:12px 14px;margin:12px 0;">
            <p style="margin:4px 0;">抓取节点：<b>{stats['raw_count']}</b> 个</p>
            <p style="margin:4px 0;">格式有效：<b>{stats['valid_count']}</b> 个</p>
            <p style="margin:4px 0;">TCP 可连接：<b>{stats['alive_count']}</b> 个</p>
            <p style="margin:4px 0;">最终入库：<b>{stats['final_count']}</b> 个</p>
            <p style="margin:4px 0;">Clash 可导入：<b>{stats['clash_count']}</b> 个</p>
        </div>

        <h3>📊 质量分级</h3>
        <ul>
            <li>S级 0-300ms：<b>{quality.get('S级', 0)}</b> 个</li>
            <li>A级 301-800ms：<b>{quality.get('A级', 0)}</b> 个</li>
            <li>B级 801-1500ms：<b>{quality.get('B级', 0)}</b> 个</li>
            <li>C级 1501-3000ms：<b>{quality.get('C级', 0)}</b> 个</li>
        </ul>

        <h3>🔗 订阅地址</h3>
        <div style="background:#f4f4f4;padding:12px;margin:12px 0;border-left:4px solid #27ae60;">
            <p style="margin:6px 0;"><b>普通订阅：</b><a href="{normal_sub_url}">{normal_sub_url}</a></p>
            <p style="margin:6px 0;"><b>Clash Verge：</b><a href="{clash_url}">{clash_url}</a></p>
            <p style="margin:6px 0;"><b>体检报告：</b><a href="{report_url}">{report_url}</a></p>
            <p style="margin:6px 0;"><b>节点数据：</b><a href="{json_url}">{json_url}</a></p>
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
            html += (
                f"<li>"
                f"[{item['pool_name']}] {item['node_type']} "
                f"{item['quality']} ⚡ {item['ping_ms']}ms "
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
            说明：当前测速为 TCP 端口连通性测速，不等同于完整代理可用性测试。延迟越低通常越好，但仍需客户端实际连接验证。
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
    safe_print("开始执行 GitHub Actions 安全版节点聚合体检系统...")
    safe_print(f"当前时间：{now_cn_str()}")
    safe_print(f"TOTAL_LIMIT={TOTAL_LIMIT}, FINAL_LIMIT={FINAL_LIMIT}, MAX_WORKERS={MAX_WORKERS}")

    valid_pools = [p for p in POOLS if p.get("url")]
    if not valid_pools:
        safe_print("没有可用节点源，请检查 SUB_URL 或 POOLS。")
        return

    per_pool_limit = max(1, TOTAL_LIMIT // len(valid_pools))

    # 1. 多源抓取
    all_raw_nodes = []
    for pool in valid_pools:
        links = fetch_and_decode(pool["url"])
        if not links:
            continue

        if len(links) > per_pool_limit:
            links = random.sample(links, per_pool_limit)

        for link in links:
            all_raw_nodes.append({
                "pool_name": pool["name"],
                "link": normalize_link(link),
            })

    raw_count = len(all_raw_nodes)
    safe_print(f"抓取完成，原始节点数：{raw_count}")

    # 2. 格式解析 + 去重
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
            "pool_name": item["pool_name"],
            "source_link": item["link"],
            **info,
        })

    valid_count = len(valid_nodes)
    safe_print(f"格式有效并去重后：{valid_count}")

    if not valid_nodes:
        safe_print("没有格式有效节点，结束。")
        return

    # 3. TCP 测速
    safe_print("开始 TCP 测速...")
    tested_nodes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for res in executor.map(test_tcp_ping, valid_nodes):
            tested_nodes.append(res)

    alive_nodes = [n for n in tested_nodes if n["alive"] and n["ping_ms"] < ALIVE_LIMIT_MS]
    alive_nodes.sort(key=lambda x: x["ping_ms"])

    safe_print(f"TCP 可连接节点：{len(alive_nodes)}")

    # 4. 最终数量限制
    final_nodes = alive_nodes[:FINAL_LIMIT]

    # 5. 查询 IP 信息 + 分类 + 改名
    ip_cache = {}
    resolved_cache = {}

    enriched_nodes = []
    classified_nodes = {}
    final_subscription_links = []

    safe_print("开始 IP 信息查询和节点分类...")

    for idx, node in enumerate(final_nodes):
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

    # 每个地区内部按延迟排序
    for country in classified_nodes:
        classified_nodes[country].sort(key=lambda x: x["ping_ms"])

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
    clash_count = generate_clash_yaml(final_subscription_links)

    # 8. 生成 nodes.json
    json_nodes = []
    for n in enriched_nodes:
        json_nodes.append({
            "pool_name": n["pool_name"],
            "scheme": n["scheme"],
            "host": n["host"],
            "port": n["port"],
            "ip": n["ip"],
            "country_code": n["country_code"],
            "country_zh": n["country_zh"],
            "org": n["org"],
            "node_type": n["node_type"],
            "quality": n["quality"],
            "ping_ms": n["ping_ms"],
            "name": n["new_name"],
        })

    with open("nodes.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": now_cn_str(),
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
        vmess_only = [link for link in raw_backup if link.lower().startswith("vmess://")]
        if vmess_only:
            backup_vmess_links = random.sample(vmess_only, min(20, len(vmess_only)))
    except Exception as e:
        safe_print(f"灾备 VMESS 抓取异常：{e}")

    # 10. 统计信息
    quality_count = {}
    for n in enriched_nodes:
        quality_count[n["quality"]] = quality_count.get(n["quality"], 0) + 1

    stats = {
        "raw_count": raw_count,
        "valid_count": valid_count,
        "alive_count": len(alive_nodes),
        "final_count": len(enriched_nodes),
        "clash_count": clash_count,
        "quality_count": quality_count,
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

    # 12. 邮件发送
    subject = (
        f"自动化聚合节点体检完成｜"
        f"{stats['final_count']} 个可用｜"
        f"S级 {quality_count.get('S级', 0)} 个｜"
        f"{now_cn_str()}"
    )

    send_html_email(subject, report_html)

    safe_print("全部流程完成。")


if __name__ == "__main__":
    main()

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
from email.mime.text import MIMEText
from email.header import Header

# ================= 配置区域 =================
# 获取环境变量
PRIMARY_SUB_URL = os.environ.get("SUB_URL")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")

# 节点池定义 (支持无限扩展)
POOLS = [
    {"name": "主池", "url": PRIMARY_SUB_URL},
    {"name": "池2", "url": "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/vless_configs.txt"},
    {"name": "池3", "url": "https://raw.githubusercontent.com/F0rc3Run/F0rc3Run/main/splitted-by-protocol/vless.txt"},
    {"name": "池4", "url": "https://raw.githubusercontent.com/NiREvil/vless/main/sub/vless.txt"}
]

TOTAL_LIMIT = 300 # 严格限制最终抽取的总节点数

COUNTRY_CODE_MAP = {
    "US": "美国", "JP": "日本", "SG": "新加坡", "HK": "香港",
    "TW": "台湾", "KR": "韩国", "GB": "英国", "CA": "加拿大",
    "RU": "俄罗斯", "MY": "马来西亚", "DE": "德国", "FR": "法国",
    "NL": "荷兰", "AU": "澳大利亚", "IN": "印度", "BR": "巴西"
}

FLAG_MAP = {
    "美国": "🇺🇸", "香港": "🇭🇰", "台湾": "🇨🇳", "日本": "🇯🇵", 
    "新加坡": "🇸🇬", "韩国": "🇰🇷", "英国": "🇬🇧", "加拿大": "🇨🇦",
    "俄罗斯": "🇷🇺", "马来西亚": "🇲🇾", "德国": "🇩🇪", "法国": "🇫🇷"
}

# ================= 核心逻辑 =================

def fetch_and_decode(url):
    """抓取并智能解码订阅链接"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            text = resp.text.strip()
            # 智能判断是否为 Base64 编码
            if "://" not in text[:100]:
                try:
                    text += '=' * (-len(text) % 4) # 补齐等号
                    text = base64.b64decode(text).decode('utf-8', errors='ignore')
                except:
                    pass
            return [line.strip() for line in text.split('\n') if "://" in line.strip()]
    except Exception as e:
        print(f"拉取失败 {url}: {e}")
    return []

def extract_node_info(link):
    """正则提取 IP 和 端口"""
    match = re.search(r'(?i)^([a-z]+)://[^@]+@([a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\]):(\d+)', link)
    if match:
        host = match.group(2).strip('[]')
        port = int(match.group(3))
        base_link = link.split('#')[0] if '#' in link else link
        return host, port, base_link
    return None, None, None

def test_tcp_ping(item):
    """并发测速工作线程"""
    pool_name, ip, port, base_link = item
    start_time = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0) # 严格的 2 秒超时熔断
        s.connect((ip, port))
        s.close()
        ping_ms = int((time.time() - start_time) * 1000)
        return (pool_name, ip, port, base_link, ping_ms)
    except:
        return (pool_name, ip, port, base_link, 9999)

def get_ip_info(ip):
    """通过 ipinfo 获取真实 BGP 归属"""
    try:
        headers = {'User-Agent': 'curl/7.68.0'}
        resp = requests.get(f"https://ipinfo.io/{ip}/json", headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return {}

def main():
    print("开始执行多源节点聚合系统...")
    
    # 1. 动态计算每个池子的配额
    valid_pools = [p for p in POOLS if p["url"]]
    per_pool_limit = TOTAL_LIMIT // len(valid_pools) if valid_pools else 0
    
    all_raw_nodes = []
    
    # 2. 拉取并随机抽样
    for pool in valid_pools:
        print(f"正在拉取: {pool['name']}...")
        links = fetch_and_decode(pool["url"])
        if links:
            # 如果节点数超过配额，则随机抽取
            if len(links) > per_pool_limit:
                links = random.sample(links, per_pool_limit)
            for link in links:
                all_raw_nodes.append((pool["name"], link))
                
    print(f"成功收集并抽样 {len(all_raw_nodes)} 个初始节点。")

    # 3. 格式过滤
    valid_format_nodes = []
    for name, link in all_raw_nodes:
        ip, port, base_link = extract_node_info(link)
        if ip and port:
            valid_format_nodes.append((name, ip, port, base_link))

    # 4. 🛡️ 并发测速与死节点熔断
    alive_nodes = []
    print("启动 20 线程并发测速...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for res in executor.map(test_tcp_ping, valid_format_nodes):
            if res[4] < 3000: # 抛弃所有超过 3 秒或超时的节点
                alive_nodes.append(res)
                
    # 按延迟从小到大排序
    alive_nodes.sort(key=lambda x: x[4])
    print(f"淘汰死节点后，存活高优节点 {len(alive_nodes)} 个。")

    classified_nodes = {}
    new_subscription_links = []
    datacenter_keywords = ["cloudflare", "amazon", "google", "digitalocean", "microsoft", "oracle", "alibaba", "tencent", "cdn"]

    # 5. 🛡️ 内存缓存去重 & API 安全查询
    ip_cache = {} 
    
    for pool_name, ip, port, base_link, ping_ms in alive_nodes:
        # 缓存机制：查过的 IP 绝对不再查第二次
        if ip not in ip_cache:
            time.sleep(1.2) # 安全限速，防止 IPinfo 封禁
            ip_cache[ip] = get_ip_info(ip)
            
        info = ip_cache[ip]
        
        country_code = info.get("country", "未知")
        org = info.get("org", "未知ISP").lower()
        
        country_zh = COUNTRY_CODE_MAP.get(country_code, country_code)
        flag = next((v for k, v in FLAG_MAP.items() if k in country_zh), "🌐")
        node_type = "🏢 机房" if any(k in org for k in datacenter_keywords) else "🏠 家宽"
        
        # 构建专属名字，带上池子标记
        ping_str = f"⚡ {ping_ms}ms"
        new_node_name = f"张牛13 [{pool_name}] {flag} {country_zh} | {node_type} | {ping_str}"
        new_link = f"{base_link}#{urllib.parse.quote(new_node_name)}"
        new_subscription_links.append(new_link)
        
        if country_zh not in classified_nodes:
            classified_nodes[country_zh] = []
        classified_nodes[country_zh].append(f"[{pool_name}] [{node_type}] {ping_str} - IP: {ip}")

    # 6. 生成小火箭兼容文件
    final_sub_content = "\n".join(new_subscription_links)
    new_sub_base64 = base64.b64encode(final_sub_content.encode('utf-8')).decode('utf-8')

    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write(new_sub_base64)

    # 7. 汇报邮件
    pages_url = "未获取到直链"
    if GITHUB_REPOSITORY and "/" in GITHUB_REPOSITORY:
        user, repo = GITHUB_REPOSITORY.split("/")
        pages_url = f"https://{user}.github.io/{repo}/sub.txt"

    html_content = f"""
    <div style="font-family: sans-serif; color: #333;">
        <h2>🚀 聚合订阅更新成功</h2>
        <p>系统已从多个源随机抽样，并剔除了死节点。本次最终为您精选 <b>{len(alive_nodes)}</b> 个极速节点。</p>
        <div style="background-color: #f4f4f4; padding: 12px; margin: 15px 0; border-left: 4px solid #27ae60;">
            <b>直链：{pages_url}</b>
        </div>
    """
    for country, items in classified_nodes.items():
        flag = next((v for k, v in FLAG_MAP.items() if k in country), "🌐")
        html_content += f"<h4>{flag} {country} ({len(items)}个)</h4><ul style='font-size: 13px;'>"
        for item in items:
            html_content += f"<li>{item}</li>"
        html_content += "</ul>"
    html_content += "</div>"

    msg = MIMEText(html_content, 'html', 'utf-8')
    msg['From'] = Header("Node-Master", 'utf-8')
    msg['To'] = EMAIL_RECEIVER 
    msg['Subject'] = Header("自动化聚合节点体检完成", 'utf-8')

    try:
        server = smtplib.SMTP_SSL("smtp.163.com", 465)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        server.quit()
        print("邮件发送成功。")
    except Exception as e:
        print(f"邮件报错: {e}")

if __name__ == "__main__":
    main()

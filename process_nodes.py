import os
import requests
import base64
import time
import re
import smtplib
import socket
import urllib.parse
from email.mime.text import MIMEText
from email.header import Header

# 获取环境变量
SUB_URL = os.environ.get("SUB_URL")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

# 国家与国旗映射字典
FLAG_MAP = {
    "美国": "🇺🇸", "香港": "🇭🇰", "台湾": "🇨🇳", "日本": "🇯🇵", 
    "新加坡": "🇸🇬", "韩国": "🇰🇷", "英国": "🇬🇧", "加拿大": "🇨🇦",
    "俄罗斯": "🇷🇺", "马来西亚": "🇲🇾", "德国": "🇩🇪", "法国": "🇫🇷"
}

def get_sub_data():
    """拉取并解码原始订阅"""
    print("正在拉取订阅链接...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(SUB_URL, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.text.strip()
            try:
                missing_padding = len(data) % 4
                if missing_padding:
                    data += '=' * (4 - missing_padding)
                decoded_text = base64.b64decode(data).decode('utf-8', errors='ignore')
            except Exception:
                decoded_text = ""
            if "://" not in decoded_text and "://" in response.text:
                decoded_text = response.text
            return [line.strip() for line in decoded_text.split('\n') if line.strip()]
    except Exception as e:
        print(f"请求报错: {e}")
    return []

def extract_node_info(link):
    """提取 IP、端口和原始基础链接（不含名称）"""
    # 匹配 vless/vmess/trojan 格式: 协议://uuid@host:port?query#name
    match = re.search(r'(?i)^([a-z]+)://[^@]+@([a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\]):(\d+)', link)
    if match:
        host = match.group(2).strip('[]')
        port = int(match.group(3))
        # 分离出 '#' 之前的基础链接
        base_link = link.split('#')[0] if '#' in link else link
        return host, port, base_link
    return None, None, None

def test_tcp_ping(ip, port, timeout=2):
    """测试 TCP 握手延迟"""
    start_time = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return int((time.time() - start_time) * 1000)
    except:
        return 9999 # 连不上或超时

def get_ip_info(ip):
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5)
        return resp.json()
    except:
        return {}

def main():
    nodes = get_sub_data()
    if not nodes:
        print("未获取到节点。")
        return

    classified_nodes = {}
    new_subscription_links = []
    
    datacenter_keywords = [
        "cloudflare", "amazon", "google", "digitalocean", "microsoft", 
        "oracle", "alibaba", "tencent", "ovh", "hetzner", "linode", 
        "vultr", "choopa", "akamai", "fastly", "hosting", "datacenter", "cdn"
    ]

    valid_nodes_count = 0
    print(f"开始全面体检，共 {len(nodes)} 个节点 (包含测速，预计耗时较长)...")
    
    for link in nodes:
        ip, port, base_link = extract_node_info(link)
        if not ip or not port:
            continue
            
        valid_nodes_count += 1
        info = get_ip_info(ip)
        time.sleep(1.5) # 防止查 IP 接口封禁
        
        # 测速 (在 GitHub 云端测速)
        ping_ms = test_tcp_ping(ip, port)
        ping_str = f"⚡ {ping_ms}ms" if ping_ms < 9999 else "❌ 超时"
        
        country = info.get("country", "未知")
        isp = info.get("isp", "未知")
        isp_lower = isp.lower()
        
        # 匹配国旗
        flag = next((v for k, v in FLAG_MAP.items() if k in country), "🌐")
        
        # 判断家宽/机房
        if any(keyword in isp_lower for keyword in datacenter_keywords):
            node_type = "🏢 机房"
        else:
            node_type = "🏠 家宽"
            
        # 🌟 核心：构造全新的节点名字 🌟
        new_node_name = f"{flag} {country} | {node_type} | {ping_str}"
        
        # 将新名字 URL 编码后拼接到链接末尾
        new_link = f"{base_link}#{urllib.parse.quote(new_node_name)}"
        new_subscription_links.append(new_link)
        
        # 加入字典用于生成邮件报告
        if country not in classified_nodes:
            classified_nodes[country] = []
        classified_nodes[country].append(f"[{node_type}] {ping_str} - IP: {ip} ({isp})")

    # 🌟 将处理后的所有新链接，重新打包成 Base64 订阅字符串 🌟
    final_sub_content = "\n".join(new_subscription_links)
    new_sub_base64 = base64.b64encode(final_sub_content.encode('utf-8')).decode('utf-8')

    # 生成极其舒适的 HTML 邮件
    html_content = f"""
    <h2>🚀 你的专属优化订阅已生成</h2>
    <p>经过重新测速和梳理，共提取 <b>{valid_nodes_count}</b> 个有效节点。请直接复制下方的 Base64 字符串，导入到 Clash Verge 或 v2rayN 中：</p>
    
    <div style="background-color: #f4f4f4; padding: 15px; border-radius: 5px; word-break: break-all; font-family: monospace; font-size: 12px; margin-bottom: 20px;">
        {new_sub_base64}
    </div>
    
    <hr>
    <h2>📊 节点区域与质量明细</h2>
    """
    
    # 将字典按国家排列输出
    for country, items in classified_nodes.items():
        flag = next((v for k, v in FLAG_MAP.items() if k in country), "🌐")
        html_content += f"<h3 style='color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 5px;'>{flag} {country} ({len(items)}个)</h3>"
        html_content += "<ul style='line-height: 1.6;'>"
        for item in items:
            # 用颜色区分低延迟和家宽
            color = "#333"
            if "家宽" in item: color = "#27ae60" # 绿色
            if "超时" in item: color = "#e74c3c" # 红色
            html_content += f"<li style='color: {color};'>{item}</li>"
        html_content += "</ul>"

    msg = MIMEText(html_content, 'html', 'utf-8')
    msg['From'] = Header("Node-Master", 'utf-8')
    msg['To'] = EMAIL_RECEIVER 
    msg['Subject'] = Header("🚀 专属优化订阅 & 节点体检报告", 'utf-8')

    try:
        server = smtplib.SMTP_SSL("smtp.163.com", 465)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        server.quit()
        print("🎉 高级订阅邮件发送成功！")
    except Exception as e:
        print(f"❌ 发送失败: {e}")

if __name__ == "__main__":
    main()

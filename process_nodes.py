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

# 获取 GitHub Secrets 环境变量
SUB_URL = os.environ.get("SUB_URL")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

FLAG_MAP = {
    "美国": "🇺🇸", "香港": "🇭🇰", "台湾": "🇨🇳", "日本": "🇯🇵", 
    "新加坡": "🇸🇬", "韩国": "🇰🇷", "英国": "🇬🇧", "加拿大": "🇨🇦",
    "俄罗斯": "🇷🇺", "马来西亚": "🇲🇾", "德国": "🇩🇪", "法国": "🇫🇷"
}

def get_sub_data():
    print("正在拉取原始订阅链接...")
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
    match = re.search(r'(?i)^([a-z]+)://[^@]+@([a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\]):(\d+)', link)
    if match:
        host = match.group(2).strip('[]')
        port = int(match.group(3))
        base_link = link.split('#')[0] if '#' in link else link
        return host, port, base_link
    return None, None, None

def test_tcp_ping(ip, port, timeout=2.5):
    start_time = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return int((time.time() - start_time) * 1000)
    except:
        return 9999 

def get_ip_info(ip):
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5)
        return resp.json()
    except:
        return {}

def main():
    nodes = get_sub_data()
    if not nodes:
        print("未获取到节点，脚本终止。")
        return

    classified_nodes = {}
    new_subscription_links = []
    
    datacenter_keywords = [
        "cloudflare", "amazon", "google", "digitalocean", "microsoft", 
        "oracle", "alibaba", "tencent", "ovh", "hetzner", "linode", 
        "vultr", "choopa", "akamai", "fastly", "hosting", "datacenter", "cdn"
    ]

    valid_nodes_count = 0
    print(f"开始全面体检，共 {len(nodes)} 个节点...")
    
    for link in nodes:
        ip, port, base_link = extract_node_info(link)
        if not ip or not port:
            continue
            
        valid_nodes_count += 1
        info = get_ip_info(ip)
        time.sleep(1.5) 
        
        ping_ms = test_tcp_ping(ip, port)
        ping_str = f"⚡ {ping_ms}ms" if ping_ms < 9999 else "❌ 超时"
        
        country = info.get("country", "未知")
        isp = info.get("isp", "未知")
        isp_lower = isp.lower()
        
        flag = next((v for k, v in FLAG_MAP.items() if k in country), "🌐")
        
        if any(keyword in isp_lower for keyword in datacenter_keywords):
            node_type = "🏢 机房"
        else:
            node_type = "🏠 家宽"
            
        new_node_name = f"{flag} {country} | {node_type} | {ping_str}"
        new_link = f"{base_link}#{urllib.parse.quote(new_node_name)}"
        new_subscription_links.append(new_link)
        
        if country not in classified_nodes:
            classified_nodes[country] = []
        classified_nodes[country].append(f"[{node_type}] {ping_str} - IP: {ip} ({isp})")

    # 生成 Base64
    final_sub_content = "\n".join(new_subscription_links)
    new_sub_base64 = base64.b64encode(final_sub_content.encode('utf-8')).decode('utf-8')

    # 将订阅内容保存为本地文件 sub.txt
    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write(new_sub_base64)
        print("本地文件 sub.txt 生成成功！")

    # 生成发给你的汇总邮件
    html_content = f"""
    <div style="font-family: sans-serif; color: #333;">
        <h2>🚀 自动订阅已更新</h2>
        <p>本次成功提取并重命名 <b>{valid_nodes_count}</b> 个节点。文件已自动推送到你的 GitHub Pages，你的小火箭客户端即将自动同步。</p>
        <hr>
        <h3>📊 节点区域明细</h3>
    """
    for country, items in classified_nodes.items():
        flag = next((v for k, v in FLAG_MAP.items() if k in country), "🌐")
        html_content += f"<h4>{flag} {country} ({len(items)}个)</h4><ul style='font-size: 14px;'>"
        for item in items:
            color = "#1c1c1e"
            if "家宽" in item: color = "#27ae60" 
            if "超时" in item: color = "#e74c3c" 
            html_content += f"<li style='color: {color};'>{item}</li>"
        html_content += "</ul>"
    html_content += "</div>"

    print("开始发送邮件...")
    msg = MIMEText(html_content, 'html', 'utf-8')
    msg['From'] = Header("Node-Master", 'utf-8')
    msg['To'] = EMAIL_RECEIVER 
    msg['Subject'] = Header("🚀 今日专属订阅更新成功", 'utf-8')

    try:
        server = smtplib.SMTP_SSL("smtp.163.com", 465)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        server.quit()
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败: {e}")

if __name__ == "__main__":
    main()

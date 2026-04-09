import os
import requests
import base64
import time
import re
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# 从 GitHub Secrets 获取环境变量
SUB_URL = os.environ.get("SUB_URL")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

def get_sub_data():
    """获取并解码 Base64 订阅链接内容"""
    print("正在拉取订阅链接...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(SUB_URL, headers=headers, timeout=15)
        if response.status_code == 200:
            # 修复可能存在的 Base64 补齐问题
            data = response.text.strip()
            missing_padding = len(data) % 4
            if missing_padding:
                data += '=' * (4 - missing_padding)
            decoded_text = base64.b64decode(data).decode('utf-8', errors='ignore')
            return [line for line in decoded_text.split('\n') if line]
    except Exception as e:
        print(f"获取订阅失败: {e}")
    return []

def extract_ip_from_link(link):
    """用正则从 vless/vmess/trojan 链接中提取 IP 或域名"""
    match = re.search(r'://[^@]+@([^:]+):', link)
    if match:
        return match.group(1)
    return None

def get_ip_info(ip):
    """调用免费 API 查询 IP 归属地和运营商"""
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5)
        return resp.json()
    except:
        return {}

def main():
    nodes = get_sub_data()
    if not nodes:
        print("没有获取到节点内容，请检查订阅链接是否有效。")
        return

    classified_nodes = {"美洲节点": [], "亚洲节点": [], "欧洲及其他": [], "解析失败": []}
    
    print(f"共发现 {len(nodes)} 个节点，开始测速分类...")
    for link in nodes:
        ip = extract_ip_from_link(link)
        if not ip:
            continue
            
        info = get_ip_info(ip)
        time.sleep(1.5)  # 【关键】暂停 1.5 秒，防止查 IP 接口被封禁
        
        if info.get("status") == "success":
            country = info.get("country", "未知国家")
            isp = info.get("isp", "未知ISP")
            node_str = f"[{country}] {isp} - {ip}"
            
            # 智能分类逻辑
            if any(k in country for k in ["美国", "加拿大", "巴西", "阿根廷"]):
                classified_nodes["美洲节点"].append(node_str)
            elif any(k in country for k in ["中国", "香港", "台湾", "日本", "新加坡", "韩国"]):
                classified_nodes["亚洲节点"].append(node_str)
            else:
                classified_nodes["欧洲及其他"].append(node_str)
        else:
            classified_nodes["解析失败"].append(f"解析失败: {ip}")
            
    # 将分类结果排版为 HTML 邮件格式
    html_content = "<h2>⚡ 每日节点分类与归属地报告</h2>"
    for category, items in classified_nodes.items():
        if items:
            html_content += f"<h3>{category} ({len(items)}个)</h3><ul>"
            for item in items:
                html_content += f"<li>{item}</li>"
            html_content += "</ul>"
            
    # 开始发送邮件
    print("分类完成，开始发送邮件...")
    msg = MIMEText(html_content, 'html', 'utf-8')
    msg['From'] = Header("Node-Bot", 'utf-8')
    msg['To'] = Header("张同学", 'utf-8')
    msg['Subject'] = Header("⚡ 今日代理节点归属地解析", 'utf-8')

    # 【已修改】网易 163 邮箱的 SMTP 服务器地址和端口
    smtp_server = "smtp.163.com" 
    smtp_port = 465

    try:
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        server.quit()
        print("🎉 邮件发送成功！请前往邮箱查收。")
    except Exception as e:
        print(f"❌ 邮件发送失败，报错信息: {e}")

if __name__ == "__main__":
    main()

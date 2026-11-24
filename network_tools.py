# -*- coding: utf-8 -*-
"""
网络优化引擎
负责网络状态检测、测速、DNS 优选、LSP 检查、hosts 管理以及连通性诊断。

设计原则：
1. 只依赖 Python 3.12 标准库 + 主程序已有的 scanner 模块，绝不引入第三方库（项目用 PyInstaller 单文件打包）。
2. 不包含任何 tkinter / UI 代码，只做引擎逻辑，进度通过 progress_callback(percent, message) 上报。
3. 所有系统调用（subprocess / socket / 文件读写）都必须容错，捕获全部常见异常，绝不让异常冒泡。
4. 绝不伪造测速结果或 DNS 延迟：测不出来就返回失败并说明原因。
"""

import os
import re
import ssl
import json
import time
import shutil
import socket
import threading
import subprocess
import ipaddress
import http.client
from datetime import datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scanner import ScanResult, get_size, format_size


# ==================== 全局常量 ====================

# 常见浏览器 UA，部分测速源会拒绝无 UA / 非浏览器 UA 的请求
_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# hosts 文件路径
_HOSTS_PATH = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                           'System32', 'drivers', 'etc', 'hosts')

# 各类子进程超时（秒）
_TIMEOUT_PING = 20       # ping
_TIMEOUT_NETSH = 15      # netsh / ipconfig 等系统命令
_TIMEOUT_NSLOOKUP = 10   # nslookup
_TIMEOUT_SPEED = 30      # 测速（下载为单次读取超时，上传为整次请求超时）
_TIMEOUT_PS = 20         # PowerShell 查询

# DNS 延迟测量使用的域名（真实解析，避免用不存在域名导致误判）
_TEST_DOMAINS = ['www.baidu.com', 'www.qq.com']

# 候选公共 DNS（默认列表，带服务商标注）
_DEFAULT_DNS_CANDIDATES = [
    {'name': '阿里DNS', 'ip': '223.5.5.5'},
    {'name': '阿里DNS', 'ip': '223.6.6.6'},
    {'name': '腾讯DNS(DNSPod)', 'ip': '119.29.29.29'},
    {'name': '腾讯DNS(DNSPod)', 'ip': '182.254.116.116'},
    {'name': '114DNS', 'ip': '114.114.114.114'},
    {'name': '百度DNS', 'ip': '180.76.76.76'},
    {'name': '国外公共DNS(Google)', 'ip': '8.8.8.8'},
    {'name': '国外公共DNS(Cloudflare)', 'ip': '1.1.1.1'},
]

# 下载测速候选地址（逐个尝试，任一成功即可；均为公网大文件/开放测速接口）
_DOWNLOAD_CANDIDATES = [
    'https://mirrors.aliyun.com/ubuntu/ls-lR.gz',            # 阿里云镜像，约 38MB
    'https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz',     # 腾讯云镜像，约 38MB
    'https://speed.cloudflare.com/__down?bytes=26214400',    # Cloudflare 开放测速接口，25MB
]

# 上传测速候选：chunked 流式上传 + 固定长度 POST 回显服务
_UPLOAD_CANDIDATES = [
    ('https://speed.cloudflare.com/__up', 'chunked'),
    ('https://httpbin.org/post', 'fixed'),
    ('https://postman-echo.com/post', 'fixed'),
]

# 正则：IPv4 / MAC / IPv6（宽松匹配，仅用于识别，不做严格校验）
_IP_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')
_MAC_RE = re.compile(r'\b(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}\b')
_IPV6_RE = re.compile(r'(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4}')

# LSP 条目数量阈值：全新系统 64 位 + 32 位目录各约 14 条，合计约 28 条；
# 明显多于该量级（> 40）才提示可能被注入。
_LSP_ENTRY_THRESHOLD = 40

# Windows 默认 hosts 内容（仅注释头，无任何映射）
_DEFAULT_HOSTS_CONTENT = """# Copyright (c) 1993-2009 Microsoft Corp.
#
# This is a sample HOSTS file used by Microsoft TCP/IP for Windows.
#
# This file contains the mappings of IP addresses to host names. Each
# entry should be kept on an individual line. The IP address should
# be placed in the first column followed by the corresponding host name.
# The IP address and the host name should be separated by at least one
# space.
#
# Additionally, comments (such as these) may be inserted on individual
# lines or following the machine name denoted by a '#' symbol.
#
# For example:
#
#      102.54.94.97     rhino.acme.com          # source server
#       38.25.63.10     x.acme.com              # x client host

# localhost name resolution is handled within DNS itself.
#\t127.0.0.1       localhost
#\t::1             localhost
"""


def _is_admin():
    """判断当前进程是否具有管理员权限（调用失败一律按无权限处理）"""
    try:
        return bool(_ctypes_windll().shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ctypes_windll():
    """延迟导入 ctypes.windll，避免非 Windows 环境导入即报错"""
    import ctypes
    return ctypes.windll


def _smart_decode(raw):
    """
    尽量正确地解码 Windows 命令行输出。
    中文系统命令输出多为 GBK，部分工具（PowerShell）为 UTF-8，
    因此按 utf-8 → gbk → cp936 → mbcs 顺序尝试严格解码，最后兜底忽略错误。
    """
    if not raw:
        return ''
    if isinstance(raw, str):
        return raw
    for enc in ('utf-8', 'gbk', 'cp936', 'mbcs'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError, TypeError):
            continue
    try:
        return raw.decode('utf-8', errors='ignore')
    except Exception:
        return ''


def _admin_required_msg():
    """统一的管理员权限提示文案"""
    return "需要管理员权限，请以管理员身份重启本程序后再试"


def _is_private_ip(ip):
    """判断是否为内网/保留地址（真实解析，不做猜测）"""
    try:
        addr = ipaddress.ip_address(ip)
        return bool(addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified)
    except Exception:
        return False


class NetworkEngine:
    """网络检测与优化引擎"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def _emit(self, percent, message):
        """上报进度（回调异常不允许影响主流程）"""
        if not self.progress_callback:
            return
        try:
            self.progress_callback(percent, message)
        except Exception:
            pass

    # ==================== 通用系统命令执行 ====================

    def _run(self, args, timeout=_TIMEOUT_NETSH):
        """
        执行系统命令并返回 (返回码, 标准输出, 标准错误)。
        命令本身按字节捕获，再按 utf-8/gbk 智能解码（中文系统兜底）。
        超时、找不到命令等异常一律转成可读文本，绝不抛出。
        """
        try:
            proc = subprocess.run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return -1, '', f'命令执行超时（>{timeout}s）：{args[0] if args else ""}'
        except FileNotFoundError:
            return -1, '', f'命令不存在：{args[0] if args else ""}'
        except Exception as exc:
            return -1, '', f'命令执行失败：{type(exc).__name__}: {exc}'
        return (proc.returncode,
                _smart_decode(proc.stdout),
                _smart_decode(proc.stderr))

    @staticmethod
    def _cmd_error_text(rc, out, err):
        """把命令输出中与权限/失败相关的信息提取成一句可读文本"""
        text = f'{out}\n{err}'.strip()
        low = text.lower()
        if ('elevation' in low or 'run as administrator' in low
                or '拒绝访问' in text or 'access is denied' in low
                or 'requires elevation' in low):
            return _admin_required_msg()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[0][:200] if lines else f'命令返回码 {rc}，无输出信息'

    # ==================== 网络信息 ====================

    def get_network_info(self) -> dict:
        """
        获取当前网络适配器信息。
        返回 {"adapter": 网卡名, "ip": 本机IP, "gateway": 网关, "dns_servers": [...],
              "mac": ..., "is_connected": bool, "link_speed": str}
        实现：解析 `ipconfig /all`；解析失败时回退 PowerShell `Get-NetIPConfiguration`。
        选取策略：优先挑"有 IPv4 地址 + 默认网关非空 + 媒体未断开"的活动网卡，
        并排除 VPN / 隧道类虚拟网卡。
        """
        info = {
            "adapter": "", "ip": "", "gateway": "", "dns_servers": [],
            "mac": "", "is_connected": False, "link_speed": "未知",
        }
        rc, out, err = self._run(['ipconfig', '/all'], timeout=_TIMEOUT_NETSH)
        blocks = self._parse_ipconfig_blocks(out) if rc == 0 and out.strip() else []
        picked = self._pick_adapter(blocks)
        if not picked:
            # ipconfig 解析不到时回退 PowerShell（同样容错解析）
            picked = self._info_from_powershell()
        if picked:
            info.update(picked)
            info['is_connected'] = bool(info['ip']) and bool(info['gateway'])
            if info['adapter']:
                info['link_speed'] = self._get_link_speed(info['adapter'])
        return info

    def _parse_ipconfig_blocks(self, text):
        """
        解析 ipconfig /all 输出，返回网卡块列表：
        [{'header': 网卡名, 'header_lower': 小写标题, 'fields': {字段名: 值}, 'dns': [DNS列表]}]
        字段名做归一化（去掉尾部点号、统一小写），同时兼容中英文系统输出。
        """
        blocks = []
        cur = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            # 非缩进行且以冒号结尾：视为网卡标题（如 "Ethernet adapter 以太网 2:" / "以太网适配器 以太网 2:"）
            if not line.startswith((' ', '\t')):
                head = line.strip()
                if head.endswith(':') and ('adapter' in head.lower() or '适配器' in head):
                    clean = self._clean_adapter_name(head[:-1].strip())
                    cur = {'header': clean,
                           'header_lower': head.lower(),
                           'fields': {}, 'dns': [], 'dns_pending': False}
                    blocks.append(cur)
                    continue
            if cur is None or ':' not in line:
                # 无冒号的行：可能是 DNS 服务器续行
                if cur is not None and cur.get('dns_pending'):
                    for ip in _IP_RE.findall(line):
                        cur['dns'].append(ip)
                continue
            key, _, val = line.partition(':')
            key_n = key.strip().rstrip('.').strip().lower()
            key_n = re.sub(r'\s+', ' ', key_n)
            val = val.strip()
            if not key_n:
                continue
            low = key_n
            is_dns = ('dns' in low and ('server' in low or '服务器' in low)
                      and 'suffix' not in low and '后缀' not in low)
            if is_dns:
                cur['dns'].extend(_IP_RE.findall(val))
                cur['dns_pending'] = True
            else:
                cur['fields'][low] = val
                cur['dns_pending'] = False
        return blocks

    @staticmethod
    def _clean_adapter_name(header):
        """
        去掉网卡标题中的类型前缀，得到 netsh / Get-NetAdapter 使用的网卡名。
        例如 "Ethernet adapter 以太网 2" → "以太网 2"，
        "无线局域网适配器 WLAN" → "WLAN"，"Unknown adapter 本地连接" → "本地连接"。
        """
        if not header:
            return ''
        m = re.match(r'^.*?(?:adapter|适配器)\s+(.+)$', header, re.I)
        if m:
            return m.group(1).strip()
        return header.strip()

    @staticmethod
    def _get_field_ip(block, keywords):
        """在网卡块的字段中查找匹配关键字的第一个 IP"""
        for key, val in block['fields'].items():
            if any(k in key for k in keywords):
                m = _IP_RE.search(val)
                if m:
                    return m.group(0)
        return ''

    def _pick_adapter(self, blocks):
        """从网卡块中挑选当前活动网卡，返回与 get_network_info 同结构的字典片段"""
        best = None
        best_score = -999
        for block in blocks:
            ip = self._get_field_ip(block, ('ipv4',))
            gateway = self._get_field_ip(block, ('gateway', '网关'))
            dns_list = list(block.get('dns') or [])
            mac = ''
            for key, val in block['fields'].items():
                if 'physical address' in key or '物理地址' in key:
                    m = _MAC_RE.search(val)
                    if m:
                        mac = m.group(0)
                        break
            media = ''
            for key, val in block['fields'].items():
                if 'media state' in key or '媒体状态' in key:
                    media = val.lower()
                    break
            disconnected = ('disconnected' in media or '已断开' in media
                            or '媒体已断开' in media)
            h = block['header_lower']
            score = 0
            if ip:
                score += 4
            if gateway:
                score += 2
            if not disconnected:
                score += 2
            if any(k in h for k in ('ethernet', '以太网', 'wireless', 'wlan', 'wi-fi', '无线')):
                score += 3
            if any(k in h for k in ('unknown adapter', '未知适配器', 'vpn', 'tunnel', '隧道')):
                score -= 2
            if score > best_score:
                best_score = score
                best = {'adapter': block['header'], 'ip': ip, 'gateway': gateway,
                        'dns_servers': dns_list, 'mac': mac}
        if not best or not best['ip']:
            return None
        return best

    def _info_from_powershell(self):
        """回退方案：用 PowerShell Get-NetIPConfiguration 拿活动网卡信息（容错解析）"""
        cmd = ['powershell', '-NoProfile', '-Command',
               'Get-NetIPConfiguration | Select-Object InterfaceAlias,IPv4Address,'
               'IPv4DefaultGateway,DNSServer,NetAdapter | ConvertTo-Json -Compress -Depth 6']
        rc, out, err = self._run(cmd, timeout=_TIMEOUT_PS)
        if rc != 0 or not out.strip():
            return None
        try:
            data = json.loads(out.strip())
        except Exception:
            return None
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            ip = ''
            ipv4 = item.get('IPv4Address')
            if isinstance(ipv4, list) and ipv4:
                ip = (ipv4[0] or {}).get('IPAddress', '') if isinstance(ipv4[0], dict) else ''
            elif isinstance(ipv4, dict):
                ip = ipv4.get('IPAddress', '')
            if not ip:
                continue
            gateway = ''
            gw = item.get('IPv4DefaultGateway')
            if isinstance(gw, list) and gw:
                gateway = (gw[0] or {}).get('NextHop', '') if isinstance(gw[0], dict) else ''
            elif isinstance(gw, dict):
                gateway = gw.get('NextHop', '')
            dns_list = []
            dns = item.get('DNSServer')
            if isinstance(dns, list):
                for d in dns:
                    if isinstance(d, dict):
                        for addr in (d.get('ServerAddresses') or []):
                            dns_list.append(str(addr))
            elif isinstance(dns, dict):
                for addr in (dns.get('ServerAddresses') or []):
                    dns_list.append(str(addr))
            return {'adapter': item.get('InterfaceAlias', '') or '',
                    'ip': ip, 'gateway': gateway,
                    'dns_servers': dns_list, 'mac': ''}
        return None

    def _get_link_speed(self, adapter):
        """
        查询网卡链路速率（如 "1 Gbps"）。
        通过 PowerShell Get-NetAdapter 读取；查询失败返回 "未知"，绝不编造数值。
        """
        if not adapter:
            return "未知"
        cmd = ['powershell', '-NoProfile', '-Command',
               'Get-NetAdapter | Select-Object Name,LinkSpeed | ConvertTo-Json -Compress']
        rc, out, err = self._run(cmd, timeout=_TIMEOUT_PS)
        if rc != 0 or not out.strip():
            return "未知"
        try:
            data = json.loads(out.strip())
        except Exception:
            return "未知"
        items = data if isinstance(data, list) else [data]
        target = adapter.strip().lower()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get('Name', ''))
            if not name:
                continue
            if name.strip().lower() == target or target in name.strip().lower():
                speed = item.get('LinkSpeed')
                return str(speed) if speed else "未知"
        return "未知"

    # ==================== 网络测速 ====================

    def test_ping(self, host="baidu.com", count=4) -> dict:
        """
        真实执行 `ping -n <count> <host>` 并解析中英文输出。
        解析不到就返回 success=False 并给出原因，绝不编造延迟数据。
        jitter_ms 为各次回包时间的标准差；<1ms 的回包按 0.5ms 近似计入。
        """
        result = {"host": host, "success": False, "avg_ms": None, "min_ms": None,
                  "max_ms": None, "loss_percent": None, "jitter_ms": None, "message": ""}
        try:
            count = max(1, min(int(count), 10))
        except Exception:
            count = 4
        self._emit(10, f'正在 ping {host}（{count} 个数据包）...')
        rc, out, err = self._run(['ping', '-n', str(count), str(host)], timeout=_TIMEOUT_PING)
        text = f'{out}\n{err}'.strip()
        if not text.strip():
            result['message'] = f'ping {host} 无输出（目标不可达或命令不可用）'
            self._emit(100, result['message'])
            return result

        min_m = re.search(r'(?:Minimum|最短)\s*=\s*(\d+)\s*ms', text, re.I)
        max_m = re.search(r'(?:Maximum|最长)\s*=\s*(\d+)\s*ms', text, re.I)
        avg_m = re.search(r'(?:Average|平均)\s*=\s*(\d+)\s*ms', text, re.I)
        loss_m = re.search(r'(?:Lost|丢失)\s*=\s*\d+\s*\((\d+(?:\.\d+)?)\s*%', text, re.I)

        if loss_m:
            try:
                result['loss_percent'] = float(loss_m.group(1))
            except Exception:
                result['loss_percent'] = None

        # 单次回包时间，用于计算抖动（真实数据，不做任何模拟）
        times = []
        for op, val in re.findall(r'(?:time|时间)\s*([=<])\s*(\d+(?:\.\d+)?)\s*ms', text, re.I):
            try:
                num = float(val)
            except Exception:
                continue
            times.append(0.5 if op == '<' else num)

        if avg_m is None:
            low = text.lower()
            if 'request timed out' in low or '请求超时' in low:
                reason = '请求超时（目标未响应）'
            elif 'unreachable' in low or '无法访问' in low or '不可达' in low:
                reason = '目标不可达'
            elif 'could not find host' in low or '找不到主机' in low or 'ping request could not find' in low:
                reason = '无法解析主机名'
            else:
                reason = '无法解析 ping 统计信息'
            if result['loss_percent'] is not None:
                reason += f'，丢包率 {result["loss_percent"]:.0f}%'
            result['message'] = f'ping {host} 失败：{reason}'
            self._emit(100, result['message'])
            return result

        try:
            result['avg_ms'] = float(avg_m.group(1))
            result['min_ms'] = float(min_m.group(1)) if min_m else None
            result['max_ms'] = float(max_m.group(1)) if max_m else None
        except Exception:
            result['message'] = f'ping {host} 统计解析失败'
            self._emit(100, result['message'])
            return result

        if len(times) >= 2:
            mean = sum(times) / len(times)
            var = sum((x - mean) ** 2 for x in times) / len(times)
            result['jitter_ms'] = round(var ** 0.5, 2)
        elif len(times) == 1:
            result['jitter_ms'] = 0.0
        elif result['min_ms'] is not None and result['max_ms'] is not None:
            result['jitter_ms'] = round(result['max_ms'] - result['min_ms'], 2)

        result['success'] = True
        loss_txt = f'{result["loss_percent"]:.0f}%' if result['loss_percent'] is not None else '未知'
        result['message'] = (f'ping {host} 成功：平均 {result["avg_ms"]:.0f}ms，'
                             f'最小 {result["min_ms"]:.0f}ms，最大 {result["max_ms"]:.0f}ms，'
                             f'丢包 {loss_txt}')
        self._emit(100, result['message'])
        return result

    def _download_once(self, url, duration):
        """
        从单个 URL 流式下载并计时，返回 (字节数, 秒数, 错误信息)。
        读取到 duration 秒即停止，单次 socket 超时按 _TIMEOUT_SPEED 控制为 10 秒。
        """
        t0 = time.perf_counter()
        total = 0
        try:
            req = Request(url, headers={
                'User-Agent': _USER_AGENT,
                'Accept': '*/*',
                'Accept-Encoding': 'identity',
                'Connection': 'keep-alive',
            })
            resp = urlopen(req, timeout=10)
        except Exception as exc:
            return 0, 0.0, f'{type(exc).__name__}: {exc}'
        try:
            while True:
                if time.perf_counter() - t0 >= duration:
                    break
                try:
                    chunk = resp.read(262144)
                except Exception:
                    break  # 读取中途出错，已下载的字节仍然计入
                if not chunk:
                    break  # 文件已下完
                total += len(chunk)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return total, time.perf_counter() - t0, ''

    def test_download_speed(self, duration=8) -> dict:
        """
        真实下载测速：依次尝试国内镜像与开放测速接口，任一成功即返回结果。
        返回 {"success":bool, "mbps":float, "bytes":int, "seconds":float, "url":str, "message":str}
        全部候选失败则 success=False 并汇总失败原因，绝不伪造数值。
        """
        result = {"success": False, "mbps": 0.0, "bytes": 0, "seconds": 0.0,
                  "url": "", "message": ""}
        try:
            duration = max(3, min(int(duration), 30))
        except Exception:
            duration = 8

        errors = []
        for idx, url in enumerate(_DOWNLOAD_CANDIDATES):
            self._emit(10 + idx * 20, f'正在测速第 {idx + 1}/{len(_DOWNLOAD_CANDIDATES)} 个测速源...')
            total, seconds, error = self._download_once(url, duration)
            if error:
                errors.append(f'{url} → {error}')
                continue
            if total < 1024 * 1024 or seconds < 0.2:
                errors.append(f'{url} → 数据量不足（{total} 字节 / {seconds:.2f}s），结果不可靠')
                continue
            mbps = total * 8 / seconds / 1_000_000
            result.update({
                'success': True,
                'mbps': round(mbps, 2),
                'bytes': total,
                'seconds': round(seconds, 2),
                'url': url,
                'message': (f'下载速度 {mbps:.2f} Mbps（{format_size(total)} / {seconds:.2f}s，'
                            f'测速源 {url}）'),
            })
            self._emit(80, result['message'])
            return result

        result['message'] = ('下载测速失败：所有候选测速源均不可用。'
                             + ('原因：' + '；'.join(errors[:3]) if errors else ''))
        return result

    def _upload_chunked(self, url, duration):
        """
        chunked 流式上传并计时，返回 (字节数, 秒数, 错误信息)。
        上传字节在 duration 秒内持续发送，随后正常结束请求并等待服务器响应，
        因此计时的字节都是服务器真实接收的数据。
        """
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return 0, 0.0, 'URL 无法解析'
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        use_tls = parts.scheme == 'https'
        total = 0
        conn = None
        t0 = time.perf_counter()
        try:
            if use_tls:
                conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=10,
                                                   context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, parts.port or 80, timeout=10)
            conn.putrequest('POST', path)
            conn.putheader('User-Agent', _USER_AGENT)
            conn.putheader('Content-Type', 'application/octet-stream')
            conn.putheader('Transfer-Encoding', 'chunked')
            conn.endheaders()
            chunk = b'x' * 262144
            while time.perf_counter() - t0 < duration and total < 64 * 1024 * 1024:
                conn.send(b'%x\r\n' % len(chunk) + chunk + b'\r\n')
                total += len(chunk)
            conn.send(b'0\r\n\r\n')
            resp = conn.getresponse()
            try:
                resp.read()
            except Exception:
                pass
            return total, time.perf_counter() - t0, ''
        except Exception as exc:
            return total, time.perf_counter() - t0, f'{type(exc).__name__}: {exc}'
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _upload_fixed(self, url, size_bytes):
        """
        固定长度 POST 上传并计时（用于 httpbin / postman-echo 这类回显服务），
        返回 (字节数, 秒数, 错误信息)。
        """
        payload = b'x' * size_bytes
        t0 = time.perf_counter()
        try:
            req = Request(url, data=payload, headers={
                'User-Agent': _USER_AGENT,
                'Content-Type': 'application/octet-stream',
                'Content-Length': str(size_bytes),
            })
            resp = urlopen(req, timeout=_TIMEOUT_SPEED)
            try:
                resp.read(1024)
            except Exception:
                pass
            return size_bytes, time.perf_counter() - t0, ''
        except Exception as exc:
            return size_bytes, time.perf_counter() - t0, f'{type(exc).__name__}: {exc}'

    def test_upload_speed(self, duration=8) -> dict:
        """
        上传测速：流式上传到公开测速接口，或 POST 一段数据到回显服务并计时。
        标准库无法像专业测速工具那样长连接压测，因此结果仅为实测近似值；
        服务不可达时返回 success=False + "上传测速服务不可用"，绝不伪造数据。
        返回结构同 test_download_speed。
        """
        result = {"success": False, "mbps": 0.0, "bytes": 0, "seconds": 0.0,
                  "url": "", "message": ""}
        try:
            duration = max(3, min(int(duration), 30))
        except Exception:
            duration = 8
        fixed_size = int(max(1, min(8, duration)) * 512 * 1024)  # 1~4MB，控制耗时

        errors = []
        for idx, (url, kind) in enumerate(_UPLOAD_CANDIDATES):
            self._emit(20 + idx * 25, f'正在测试上传通道 {idx + 1}/{len(_UPLOAD_CANDIDATES)}...')
            if kind == 'chunked':
                total, seconds, error = self._upload_chunked(url, duration)
            else:
                total, seconds, error = self._upload_fixed(url, fixed_size)
            if error:
                errors.append(f'{url} → {error}')
                continue
            if total <= 0 or seconds < 0.3:
                errors.append(f'{url} → 上传数据量不足或耗时过短，结果不可靠')
                continue
            mbps = total * 8 / seconds / 1_000_000
            result.update({
                'success': True,
                'mbps': round(mbps, 2),
                'bytes': total,
                'seconds': round(seconds, 2),
                'url': url,
                'message': (f'上传速度 {mbps:.2f} Mbps（{format_size(total)} / {seconds:.2f}s，'
                            f'通道 {url}）'),
            })
            self._emit(90, result['message'])
            return result

        result['message'] = '上传测速服务不可用：' + (
            '；'.join(errors[:3]) if errors else '所有候选服务器均不可达')
        return result

    # ==================== DNS 优选 ====================

    def _measure_one_dns(self, dns_ip):
        """
        用 `nslookup <domain> <dns_ip>` 逐个域名真实测量解析耗时（毫秒）。
        返回 (平均耗时或 None, 成功域名数, 失败原因列表)。
        """
        costs = []
        failures = []
        for domain in _TEST_DOMAINS:
            t0 = time.perf_counter()
            rc, out, err = self._run(['nslookup', domain, dns_ip], timeout=_TIMEOUT_NSLOOKUP)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            text = f'{out}\n{err}'
            if self._nslookup_has_answer(text):
                costs.append(elapsed_ms)
            else:
                low = text.lower()
                if 'timed' in low or '超时' in text:
                    failures.append(f'{domain}: 请求超时')
                elif "can't find" in low or '找不到' in text:
                    failures.append(f'{domain}: 无解析结果')
                elif not text.strip():
                    failures.append(f'{domain}: 无输出（rc={rc}）')
                else:
                    failures.append(f'{domain}: 未获得解析结果')
        if not costs:
            return None, 0, failures
        return round(sum(costs) / len(costs), 1), len(costs), failures

    @staticmethod
    def _nslookup_has_answer(text):
        """
        判断 nslookup 输出中是否存在真实解析结果。
        跳过开头 "服务器/Server + Address" 段落（那里的地址是 DNS 服务器本身），
        只看空行之后的应答段，避免把服务器地址误判为解析成功。
        """
        if not text:
            return False
        parts = re.split(r'\r?\n\s*\r?\n', text, maxsplit=1)
        tail = parts[1] if len(parts) > 1 else ''
        if not tail.strip():
            return False
        low = tail.lower()
        for bad in ("can't find", 'timed', 'non-existent', 'no servers could be reached',
                    '找不到', '超时', '不存在', '不可达', 'unreachable'):
            if bad in low or bad in tail:
                return False
        return bool(_IP_RE.search(tail) or _IPV6_RE.search(tail))

    def measure_dns_servers(self, candidates=None) -> list:
        """
        测量一批公共 DNS 的真实解析延迟，返回：
        [{"name": 服务商, "ip": "223.5.5.5", "dns": "223.5.5.5",
          "avg_ms": float或None, "success": bool, "score": 分数}]
        按 avg_ms 升序排列，失败项排在最后（avg_ms=None）。
        测量方式：对每个 DNS 用 nslookup 真实解析 2 个常见域名并取平均耗时；
        多个 DNS 并发测量以控制总耗时；测不到就 success=False，绝不编造延迟。
        """
        if candidates is None:
            candidates = _DEFAULT_DNS_CANDIDATES
        norm = []
        for item in candidates:
            if isinstance(item, dict):
                ip = str(item.get('ip', '')).strip()
                name = str(item.get('name', '') or ip)
            else:
                ip = str(item).strip()
                name = ip
                for c in _DEFAULT_DNS_CANDIDATES:
                    if c['ip'] == ip:
                        name = c['name']
                        break
            if not _IP_RE.fullmatch(ip):
                continue
            norm.append({'name': name, 'ip': ip})
        if not norm:
            return []

        results = [None] * len(norm)
        lock = threading.Lock()
        done = [0]

        def worker(idx, item):
            try:
                avg, ok_count, failures = self._measure_one_dns(item['ip'])
            except Exception as exc:
                avg, ok_count, failures = None, 0, [f'{type(exc).__name__}: {exc}']
            entry = {
                'name': item['name'], 'ip': item['ip'], 'dns': item['ip'],
                'avg_ms': avg, 'success': avg is not None,
                'score': round(max(0.0, 100.0 - (avg or 0) / 4.0), 1) if avg is not None else 0,
                'domains_tested': ok_count,
                'detail': '' if avg is not None else ('；'.join(failures[:2]) or '测量失败'),
            }
            with lock:
                results[idx] = entry
                done[0] += 1
                self._emit(min(95, 10 + done[0] * 80 // len(norm)),
                           f'DNS 延迟测量进度 {done[0]}/{len(norm)}')

        threads = []
        for i, item in enumerate(norm):
            t = threading.Thread(target=worker, args=(i, item), daemon=True)
            threads.append(t)
            t.start()
        # 单个 DNS 需依次解析若干个域名，单个 nslookup 超时 10 秒，因此整体等待时间按
        # 域名数量放宽，避免个别不可用 DNS 卡住时其它结果无法返回
        deadline = time.time() + _TIMEOUT_NSLOOKUP * len(_TEST_DOMAINS) + 5
        for t in threads:
            remain = max(0.1, deadline - time.time())
            t.join(timeout=remain)

        out = []
        for i, res in enumerate(results):
            if res is None:
                res = {'name': norm[i]['name'], 'ip': norm[i]['ip'], 'dns': norm[i]['ip'],
                       'avg_ms': None, 'success': False, 'score': 0,
                       'domains_tested': 0, 'detail': '测量超时未返回结果'}
            out.append(res)
        out.sort(key=lambda x: (x['avg_ms'] is None, x['avg_ms'] if x['avg_ms'] is not None else 0))
        best = next((x for x in out if x['success']), None)
        if best:
            self._emit(100, f'DNS 测量完成，最快：{best["name"]} {best["ip"]} {best["avg_ms"]:.0f}ms')
        else:
            self._emit(100, 'DNS 测量完成，但没有任何候选 DNS 可用')
        return out

    # ==================== DNS 设置 ====================

    def _resolve_adapter(self, adapter_name):
        """获取用于 netsh 的网卡名，未指定时从当前活动网卡推断"""
        if adapter_name and str(adapter_name).strip():
            return str(adapter_name).strip()
        info = self.get_network_info()
        return info.get('adapter', '') or ''

    def set_dns_servers(self, dns_list, adapter_name=None):
        """
        使用 netsh 设置静态 DNS：
          netsh interface ip set dns name="<网卡名>" static <ip> primary
          netsh interface ip add dns name="<网卡名>" <ip2> index=2
        需要管理员权限，权限不足返回 (False, "需要管理员权限，请以管理员身份重启")。
        返回 (是否成功, 可读中文信息)；全程真实执行命令，不伪造结果。
        """
        try:
            ips = [str(ip).strip() for ip in (dns_list or []) if _IP_RE.fullmatch(str(ip).strip())]
        except Exception:
            ips = []
        if not ips:
            return False, "未提供有效的 DNS 地址，请检查输入格式（例如 223.5.5.5）"
        if len(ips) > 4:
            return False, "DNS 地址最多支持 4 个，请减少后再试"
        if not _is_admin():
            return False, _admin_required_msg()
        name = self._resolve_adapter(adapter_name)
        if not name:
            return False, "未能识别当前活动网卡，请手动指定网卡名称后再试"

        commands = []
        first_ip = ips[0]
        commands.append((['netsh', 'interface', 'ip', 'set', 'dns', f'name={name}',
                          'static', first_ip, 'primary'], first_ip))
        for index, ip in enumerate(ips[1:], start=2):
            commands.append((['netsh', 'interface', 'ip', 'add', 'dns', f'name={name}',
                              ip, f'index={index}'], ip))

        done = []
        for cmd, ip in commands:
            rc, out, err = self._run(cmd, timeout=_TIMEOUT_NETSH)
            text = f'{out}\n{err}'.strip()
            if rc != 0:
                return False, (f'设置 DNS 失败（{ip}）：'
                               f'{self._cmd_error_text(rc, out, err)}；已完成 {done or "无"}')
            done.append(ip)
        return True, (f'已将网卡「{name}」的 DNS 设置为 {"、".join(ips)}，'
                      f'建议刷新 DNS 缓存后生效')

    def reset_dns_servers(self, adapter_name=None):
        """
        恢复网卡 DNS 为自动获取（DHCP）：
          netsh interface ip set dns name="<网卡名>" source=dhcp
        需要管理员权限。返回 (是否成功, 可读中文信息)。
        """
        if not _is_admin():
            return False, _admin_required_msg()
        name = self._resolve_adapter(adapter_name)
        if not name:
            return False, "未能识别当前活动网卡，请手动指定网卡名称后再试"
        cmd = ['netsh', 'interface', 'ip', 'set', 'dns', f'name={name}', 'source=dhcp']
        rc, out, err = self._run(cmd, timeout=_TIMEOUT_NETSH)
        if rc != 0:
            return False, f'恢复自动获取 DNS 失败：{self._cmd_error_text(rc, out, err)}'
        return True, f'网卡「{name}」的 DNS 已恢复为自动获取（DHCP）'

    def flush_dns_cache(self) -> bool:
        """执行 `ipconfig /flushdns` 刷新 DNS 解析缓存，返回是否成功"""
        self._emit(50, '正在刷新 DNS 解析缓存...')
        rc, out, err = self._run(['ipconfig', '/flushdns'], timeout=_TIMEOUT_NETSH)
        text = f'{out}\n{err}'.strip()
        low = text.lower()
        if rc != 0:
            return False
        ok = ('successfully flushed' in low or '成功刷新' in text
              or '已刷新' in text or 'flush' in low and 'fail' not in low and '失败' not in text)
        bad = ('failed' in low or '失败' in text or 'denied' in low or '拒绝' in text)
        return bool(ok and not bad)

    # ==================== LSP 检查与修复 ====================

    def check_lsp(self) -> dict:
        """
        检查 Winsock LSP（分层服务提供程序）目录是否异常。
        实现：`netsh winsock show catalog`，统计协议条目数量并检查是否存在
        位于 SystemRoot 之外的第三方提供程序；条目数明显多于正常量级
        （> 40，全新系统 64 位 + 32 位目录合计约 28 条）即提示可能被劫持。
        解析失败时 success=False。全部结论基于真实命令输出。
        """
        result = {"success": False, "entry_count": 0, "suspicious": False,
                  "detail": "", "message": ""}
        self._emit(30, '正在检查 Winsock LSP 协议目录...')
        rc, out, err = self._run(['netsh', 'winsock', 'show', 'catalog'], timeout=_TIMEOUT_NETSH)
        text = out if out.strip() else err
        if rc != 0 or not text.strip():
            result['message'] = f'无法读取 Winsock 目录：{self._cmd_error_text(rc, out, err)}'
            return result

        entry_count = 0
        foreign = []
        layered = []
        cur_path = ''
        for line in text.splitlines():
            low = line.lower()
            if 'entry type' in low or '条目类型' in line:
                entry_count += 1
                if 'layered' in low or '分层' in line:
                    layered.append(line.strip())
            if 'provider path' in low or '提供程序路径' in line:
                _, _, val = line.partition(':')
                cur_path = val.strip()
                if cur_path and 'systemroot' not in cur_path.lower() \
                        and 'system32' not in cur_path.lower() \
                        and 'syswow64' not in cur_path.lower():
                    foreign.append(cur_path)
            if 'description' in low:
                _, _, val = line.partition(':')
                desc = val.strip()
                if ('layered' in low) or ('分层' in desc):
                    layered.append(desc)

        if entry_count <= 0:
            result['message'] = 'Winsock 目录输出中未找到任何协议条目，无法判断'
            return result

        suspicious = entry_count > _LSP_ENTRY_THRESHOLD or bool(foreign) or bool(layered)
        details = [f'协议条目数 {entry_count}（正常全新系统约 28 条）']
        if foreign:
            details.append('存在系统目录之外的提供程序：' + '；'.join(sorted(set(foreign))[:5]))
        if layered:
            details.append('存在分层服务提供程序(LSP)条目：' + '；'.join(layered[:3]))
        result.update({
            'success': True,
            'entry_count': entry_count,
            'suspicious': suspicious,
            'detail': '；'.join(details),
            'message': ('Winsock 目录疑似被第三方注入，建议重置 LSP（需管理员权限与重启）'
                        if suspicious else 'Winsock 目录条目数量正常，未见明显劫持迹象'),
        })
        return result

    def reset_lsp(self):
        """
        【高风险操作】重置 Winsock 目录：`netsh winsock reset catalog`
        执行后需要重启电脑才能生效；重置会导致部分安全软件、网络加速/代理类软件
        的 Winsock 挂载失效，可能需要重新安装或重新初始化网络组件。
        需要管理员权限。返回 (是否成功, 可读中文信息)。
        """
        if not _is_admin():
            return False, _admin_required_msg()
        self._emit(40, '正在重置 Winsock 目录（高风险操作）...')
        rc, out, err = self._run(['netsh', 'winsock', 'reset', 'catalog'], timeout=_TIMEOUT_NETSH)
        if rc != 0:
            return False, f'Winsock 目录重置失败：{self._cmd_error_text(rc, out, err)}'
        return True, "Winsock 目录已重置，请重启电脑后生效"

    def reset_tcpip(self):
        """
        【高风险操作】重置 TCP/IP 协议栈：`netsh int ip reset`
        需要管理员权限，且重置后需要重启电脑才能生效；
        重置可能清除自定义的路由、DNS、静态 IP 等网络配置。
        返回 (是否成功, 可读中文信息)。
        """
        if not _is_admin():
            return False, _admin_required_msg()
        self._emit(50, '正在重置 TCP/IP 协议栈（高风险操作）...')
        rc, out, err = self._run(['netsh', 'int', 'ip', 'reset'], timeout=_TIMEOUT_NETSH)
        if rc != 0:
            return False, f'TCP/IP 协议栈重置失败：{self._cmd_error_text(rc, out, err)}'
        return True, "TCP/IP 协议栈已重置，请重启电脑后生效"

    # ==================== hosts 管理 ====================

    @staticmethod
    def _parse_hosts_line(line):
        """
        解析单行 hosts 内容，返回 {ip, domain, comment, enabled, raw}。
        空白行、以及"只有注释没有映射"的行返回 None（不当作映射条目）。
        以 # 开头（或被行内 # 注释掉）的映射行依然解析出来，但标记 enabled=False。
        """
        raw = line.strip().strip('\x00').strip()
        if not raw:
            return None
        enabled = not raw.startswith('#')
        body = raw[1:].strip() if not enabled else raw
        comment = ''
        if '#' in body:
            body, _, comment = body.partition('#')
            body = body.strip()
            comment = comment.strip()
        parts = body.split()
        ip = parts[0] if parts and _IP_RE.fullmatch(parts[0]) else ''
        domain = parts[1] if (ip and len(parts) > 1) else ''
        if not ip and not domain:
            return None  # 纯注释行，没有映射内容
        return {'ip': ip, 'domain': domain, 'comment': comment,
                'enabled': enabled, 'raw': raw}

    def read_hosts(self) -> dict:
        """
        读取并解析 hosts 文件【只读，绝不写入】。
        返回 {"success":bool, "path":..., "entries":[{"ip","domain","comment","enabled"}],
              "raw_lines":int, "message":str}
        以 # 开头或被注释的行标记 enabled=False；解析异常时 success=False 并给出原因。
        """
        result = {"success": False, "path": _HOSTS_PATH, "entries": [],
                  "raw_lines": 0, "message": ""}
        if not os.path.exists(_HOSTS_PATH):
            result['message'] = f'hosts 文件不存在：{_HOSTS_PATH}'
            return result
        try:
            with open(_HOSTS_PATH, 'rb') as f:
                raw = f.read()
        except PermissionError:
            result['message'] = f'读取 hosts 文件被拒绝（权限不足）：{_HOSTS_PATH}'
            return result
        except Exception as exc:
            result['message'] = f'读取 hosts 文件失败：{type(exc).__name__}: {exc}'
            return result

        binary_anomaly = b'\x00' in raw
        text = _smart_decode(raw)
        lines = text.replace('\x00', '').splitlines()
        entries = []
        for line in lines:
            try:
                entry = self._parse_hosts_line(line)
            except Exception:
                entry = None
            if entry:
                entries.append(entry)
        active = [e for e in entries if e['enabled']]
        result.update({
            'success': True,
            'entries': entries,
            'raw_lines': len(lines),
        })
        msg = f'hosts 文件共 {len(lines)} 行，解析出 {len(entries)} 条记录，其中生效 {len(active)} 条'
        if binary_anomaly:
            msg += '；检测到文件包含异常二进制空字符（可能被篡改或损坏）'
        result['message'] = msg
        return result

    def backup_hosts(self, backup_dir=None):
        """
        备份 hosts 到「用户目录\\CleanMasterBackup\\hosts\\hosts_<时间戳>.bak」。
        返回 (是否成功, 备份文件路径或失败原因)。
        """
        if not os.path.exists(_HOSTS_PATH):
            return False, f'hosts 文件不存在：{_HOSTS_PATH}'
        try:
            if not backup_dir:
                backup_dir = os.path.join(os.environ.get('USERPROFILE', ''),
                                          'CleanMasterBackup', 'hosts')
            os.makedirs(backup_dir, exist_ok=True)
            target = os.path.join(backup_dir,
                                  f'hosts_{datetime.now().strftime("%Y%m%d_%H%M%S")}.bak')
            shutil.copy2(_HOSTS_PATH, target)
            return True, target
        except Exception as exc:
            return False, f'备份 hosts 失败：{type(exc).__name__}: {exc}'

    def restore_hosts(self, backup_file):
        """
        从备份文件还原 hosts（写入系统目录需要管理员权限）。
        返回 (是否成功, 可读中文信息)。
        """
        if not backup_file or not os.path.exists(backup_file):
            return False, f'备份文件不存在：{backup_file}'
        if not _is_admin():
            return False, _admin_required_msg()
        try:
            # 还原前先备份当前 hosts，避免误操作后无法回退
            self.backup_hosts()
            shutil.copy2(backup_file, _HOSTS_PATH)
            return True, f'已从备份还原 hosts：{backup_file}（如浏览器仍异常，可刷新 DNS 缓存）'
        except PermissionError:
            return False, _admin_required_msg()
        except Exception as exc:
            return False, f'还原 hosts 失败：{type(exc).__name__}: {exc}'

    def reset_hosts(self):
        """
        把 hosts 重置为 Windows 默认内容（只有注释头，无任何映射）。
        需要管理员权限，且执行前会自动先备份（备份失败则中止操作）。
        返回 (是否成功, 可读中文信息)。
        """
        if not _is_admin():
            return False, _admin_required_msg()
        ok, info = self.backup_hosts()
        if not ok:
            return False, f'重置前备份 hosts 失败，已中止操作：{info}'
        try:
            with open(_HOSTS_PATH, 'w', encoding='ascii', errors='ignore', newline='\r\n') as f:
                f.write(_DEFAULT_HOSTS_CONTENT)
            return True, f'hosts 已重置为 Windows 默认内容，原文件已备份至：{info}'
        except PermissionError:
            return False, _admin_required_msg()
        except Exception as exc:
            return False, f'重置 hosts 失败：{type(exc).__name__}: {exc}'

    # ==================== 连通性诊断 ====================

    @staticmethod
    def _resolve_with_timeout(host, timeout=8):
        """
        在子线程中调用系统解析器解析域名，避免 DNS 故障时长时间阻塞。
        返回 (是否成功, 地址列表, 耗时毫秒或 None, 错误信息)。
        """
        holder = {}

        def worker():
            t0 = time.perf_counter()
            try:
                infos = socket.getaddrinfo(host, 80, socket.AF_UNSPEC, socket.SOCK_STREAM)
                addrs = []
                for info in infos:
                    try:
                        addr = info[4][0]
                    except Exception:
                        continue
                    if addr not in addrs:
                        addrs.append(addr)
                holder['result'] = (True, addrs, (time.perf_counter() - t0) * 1000, '')
            except Exception as exc:
                holder['result'] = (False, [], (time.perf_counter() - t0) * 1000,
                                    f'{type(exc).__name__}: {exc}')

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if 'result' not in holder:
            return False, [], None, f'解析超时（>{timeout}s）'
        return holder['result']

    def diagnose(self) -> list:
        """
        逐项执行真实连通性诊断，返回：
        [{"step": 步骤名, "result": "正常"/"异常", "detail": 说明}]
        依次检查：本机 IP → 默认网关 → DNS 解析 → 外网可达 → 目标网站可达。
        """
        results = []
        self._emit(10, '正在读取本机网络信息...')
        info = self.get_network_info()

        # 1) 本机是否获取到 IP
        if info.get('ip'):
            results.append({
                'step': '检查本机IP', 'result': '正常',
                'detail': f'网卡「{info.get("adapter", "未知")}」IP {info["ip"]}，'
                          f'网关 {info.get("gateway") or "无"}，'
                          f'链路速率 {info.get("link_speed", "未知")}',
            })
        else:
            results.append({
                'step': '检查本机IP', 'result': '异常',
                'detail': '未获取到有效 IPv4 地址，请检查网线/Wi-Fi 连接或网卡驱动',
            })

        # 2) 默认网关是否可达
        gateway = info.get('gateway') or ''
        self._emit(30, f'正在检测默认网关 {gateway or "（未知）"}...')
        if not gateway:
            results.append({'step': '检查默认网关', 'result': '异常',
                            'detail': '未获取到默认网关地址，无法与局域网通信'})
        else:
            ping = self.test_ping(gateway, count=2)
            results.append({
                'step': '检查默认网关', 'result': '正常' if ping['success'] else '异常',
                'detail': (f'网关 {gateway} 可达，平均 {ping["avg_ms"]:.0f}ms，'
                           f'丢包 {ping["loss_percent"] if ping["loss_percent"] is not None else "未知"}%'
                           if ping['success'] else f'网关 {gateway} 不可达：{ping["message"]}'),
            })

        # 3) DNS 是否可解析
        self._emit(50, '正在测试 DNS 域名解析...')
        ok, addrs, cost_ms, error = self._resolve_with_timeout('www.baidu.com', timeout=8)
        if ok and addrs:
            results.append({
                'step': '检查DNS解析', 'result': '正常',
                'detail': f'www.baidu.com 解析成功，耗时 {cost_ms:.0f}ms，'
                          f'返回 {len(addrs)} 个地址（{addrs[0]}）',
            })
        else:
            results.append({
                'step': '检查DNS解析', 'result': '异常',
                'detail': f'域名解析失败：{error or "未返回任何地址"}，可尝试使用 DNS 优选或刷新 DNS 缓存',
            })

        # 4) 外网是否可达
        self._emit(70, '正在测试外网连通性...')
        ping = self.test_ping('223.5.5.5', count=3)
        results.append({
            'step': '检查外网连通性', 'result': '正常' if ping['success'] else '异常',
            'detail': (f'223.5.5.5 可达，平均 {ping["avg_ms"]:.0f}ms，'
                       f'丢包 {ping["loss_percent"] if ping["loss_percent"] is not None else "未知"}%'
                       if ping['success'] else f'外网不可达：{ping["message"]}'),
        })

        # 5) 目标网站是否可达
        self._emit(90, '正在测试目标网站连通性...')
        ping = self.test_ping('baidu.com', count=3)
        results.append({
            'step': '检查目标网站', 'result': '正常' if ping['success'] else '异常',
            'detail': (f'baidu.com 可达，平均 {ping["avg_ms"]:.0f}ms，'
                       f'丢包 {ping["loss_percent"] if ping["loss_percent"] is not None else "未知"}%'
                       if ping['success'] else f'目标网站不可达：{ping["message"]}'),
        })
        self._emit(100, '网络诊断完成')
        return results

    # ==================== 汇总为 ScanResult ====================

    def scan_network_issues(self) -> list:
        """
        把网络层面的"可优化"问题转成 ScanResult 列表（全部基于真实读到的数据，
        size 取 get_size(path)；不产出任何伪造的数据量）。
        包含：
          1. hosts 映射异常（被屏蔽/指向内网地址）或映射过多、文件异常
          2. LSP 条目异常偏多
          3. DNS 未优选（当前 DNS 明显慢于候选公共 DNS）
        注意：DNS 解析缓存位于内存中、没有对应文件，因此不产出该类"可清理"条目。
        """
        results = []

        # ---- 1) hosts 检查（只读） ----
        self._emit(10, '正在检查 hosts 映射...')
        try:
            hosts = self.read_hosts()
        except Exception:
            hosts = {'success': False, 'entries': [], 'message': ''}
        if hosts.get('success'):
            suspicious = []
            enabled = 0
            for entry in hosts.get('entries', []):
                if not entry.get('enabled'):
                    continue
                ip = entry.get('ip', '')
                domain = entry.get('domain', '')
                if not _IP_RE.fullmatch(ip) or not domain:
                    continue
                if domain.lower() in ('localhost', 'localhost.localdomain', 'local'):
                    continue  # Windows 标准写法，不算异常
                enabled += 1
                if ip in ('0.0.0.0', '127.0.0.1', '::1'):
                    suspicious.append(f'{domain} → {ip}(屏蔽/本机地址)')
                elif _is_private_ip(ip):
                    suspicious.append(f'{domain} → {ip}(内网地址)')
            if suspicious:
                results.append(ScanResult(
                    category="网络优化",
                    source="hosts映射异常",
                    path=_HOSTS_PATH,
                    size=get_size(_HOSTS_PATH),
                    description=("hosts 中存在非标准映射（可能由广告/破解类软件写入或被篡改，"
                                 "也可能是用户自建的广告屏蔽规则），"
                                 f"共 {len(suspicious)} 条：{'；'.join(suspicious[:8])}"
                                 + ("…" if len(suspicious) > 8 else "")
                                 + "。建议先备份再重置 hosts。"),
                ))
            elif enabled > 30:
                results.append(ScanResult(
                    category="网络优化",
                    source="hosts映射过多",
                    path=_HOSTS_PATH,
                    size=get_size(_HOSTS_PATH),
                    description=(f"hosts 中生效的自定义映射共 {enabled} 条，数量偏多，"
                                 "可能由第三方软件批量写入，建议确认后清理以恢复标准域名解析。"),
                ))
            if '二进制空字符' in (hosts.get('message') or ''):
                results.append(ScanResult(
                    category="网络优化",
                    source="hosts文件异常",
                    path=_HOSTS_PATH,
                    size=get_size(_HOSTS_PATH),
                    description="hosts 文件包含异常二进制空字符，文件可能被损坏或被程序异常写入，"
                                "可能导致域名解析异常，建议先备份再重置为默认内容。",
                ))
        else:
            self._emit(20, f'hosts 检查跳过：{hosts.get("message", "读取失败")}')

        # ---- 2) LSP 检查 ----
        self._emit(40, '正在检查 Winsock LSP 协议目录...')
        try:
            lsp = self.check_lsp()
        except Exception:
            lsp = {'success': False, 'suspicious': False, 'detail': '', 'message': ''}
        if lsp.get('success') and lsp.get('suspicious'):
            results.append(ScanResult(
                category="网络优化",
                source="LSP协议链异常",
                path="Winsock 目录(LSP)",
                size=get_size("Winsock 目录(LSP)"),
                description=(f"Winsock 协议目录可能存在第三方注入：{lsp.get('detail', '')}。"
                             "重置 LSP 可清理异常协议链（需管理员权限并重启电脑，"
                             "部分安全/加速软件可能需要重装）。"),
            ))

        # ---- 3) DNS 优选 ----
        self._emit(60, '正在测量当前 DNS 与候选公共 DNS 的解析延迟...')
        try:
            info = self.get_network_info()
            current_dns = list(info.get('dns_servers') or [])
            candidates = [{'name': f'当前DNS {ip}', 'ip': ip} for ip in current_dns[:2]]
            for ip in ('223.5.5.5', '119.29.29.29', '114.114.114.114'):
                if ip not in current_dns:
                    candidates.append({'name': '候选公共DNS', 'ip': ip})
            measured = self.measure_dns_servers(candidates) if candidates else []
            ok_items = [m for m in measured if m.get('success')]
            current_ok = [m for m in ok_items if str(m.get('name', '')).startswith('当前DNS')]
            best_ok = next((m for m in ok_items if not str(m.get('name', '')).startswith('当前DNS')), None)
            if current_ok and best_ok:
                cur_avg = current_ok[0]['avg_ms']
                best_avg = best_ok['avg_ms']
                # 只有候选 DNS 明显更快（至少快 20%）才提示，
                # 否则可能出现"建议切换到更慢的 DNS"的误导结论
                if best_avg < cur_avg * 0.8:
                    gain = (1 - best_avg / cur_avg) * 100 if cur_avg > 0 else 0
                    results.append(ScanResult(
                        category="网络优化",
                        source="DNS配置可优化",
                        path=f"网络适配器: {info.get('adapter', '未知')}",
                        size=get_size(f"网络适配器: {info.get('adapter', '未知')}"),
                        description=(f"当前 DNS（{current_ok[0]['ip']}）平均解析 {cur_avg:.0f}ms，"
                                     f"而 {best_ok['ip']} 仅 {best_avg:.0f}ms（约快 {gain:.0f}%），"
                                     "建议改用更快的公共 DNS（DNS 优选）以提升网页打开速度。"),
                    ))
            elif not current_ok and best_ok:
                results.append(ScanResult(
                    category="网络优化",
                    source="DNS配置异常",
                    path=f"网络适配器: {info.get('adapter', '未知')}",
                    size=get_size(f"网络适配器: {info.get('adapter', '未知')}"),
                    description=("当前配置的 DNS 服务器均未能正常响应解析请求，"
                                 "建议切换到可用的公共 DNS（如 223.5.5.5）后刷新缓存。"),
                ))
        except Exception as exc:
            self._emit(80, f'DNS 优选检查失败：{type(exc).__name__}: {exc}')

        self._emit(100, f'网络优化检查完成，发现 {len(results)} 项可优化问题')
        return results

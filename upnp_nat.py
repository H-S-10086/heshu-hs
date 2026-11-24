# -*- coding: utf-8 -*-
r"""
UPnP 自动端口映射

作用
----
「跨网络访问」需要路由器把外网端口转发到本机。手动改路由器门槛高，
这里通过 UPnP IGD 协议自动完成：SSDP 发现网关 → 读取设备描述 →
SOAP 调用 AddPortMapping / GetExternalIPAddress。

约束
----
  · 仅标准库，不引入第三方依赖
  · 本地设备 HTTP 必须绕过系统代理（否则会被 Clash 之类的代理截走）
  · 路由器未开启 UPnP、或运营商大内网（CGNAT）时会如实报错，
    由界面继续引导用户用手动映射 / IPv6 / 组网工具

对外接口
--------
  map_port(port)    → {"ok":True,"public_ip":...,"local_ip":...}
                      失败抛 UpnpError（消息为中文，可直接展示）
  unmap_port(port)  → True / False（失败静默，不抛异常）
"""

import re
import time
import socket
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET


SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
WAN_IP_PREFIX = "urn:schemas-upnp-org:service:WANIPConnection:"
WAN_PPP_PREFIX = "urn:schemas-upnp-org:service:WANPPPConnection:"
MAP_DESC = "HeShu-HS-Transfer"


class UpnpError(Exception):
    """UPnP 操作失败（消息可直接展示给用户）"""


def _local_ip_towards(host):
    """取本机通往 host 的那块网卡 IP（UDP connect 不会真的发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host or "192.168.1.1", 9))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        try:
            s.close()
        except Exception:
            pass


def _ssdp_locations(timeout=3.0):
    """SSDP 搜索网关，返回设备描述 URL 列表（去重、保序）"""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: %s:%d\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: %s\r\n\r\n" % (SSDP_ADDR, SSDP_PORT, SSDP_ST)
    ).encode("ascii")

    locations = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.6)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        deadline = time.time() + max(1.0, float(timeout))
        rounds = 0
        while time.time() < deadline and rounds < 3:
            try:
                s.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            except Exception:
                pass
            rounds += 1
            while time.time() < deadline:
                try:
                    data, _addr = s.recvfrom(65535)
                except socket.timeout:
                    break
                except Exception:
                    break
                text = data.decode("utf-8", "ignore")
                for line in text.split("\r\n"):
                    head = line.lower()
                    if head.startswith("location:"):
                        loc = line.split(":", 1)[1].strip()
                        if loc and loc not in locations:
                            locations.append(loc)
    finally:
        try:
            s.close()
        except Exception:
            pass
    return locations


def _opener():
    """绕过系统代理的 urlopen（局域网设备不能被代理接管）"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_xml(url, timeout=4.0):
    with _opener().open(url, timeout=timeout) as resp:
        return resp.read(512 * 1024).decode("utf-8", "ignore")


def _find_wan_service(xml_text, location):
    """
    在设备描述里找 WAN 连接服务的控制地址。
    返回 (service_type, control_url)，找不到返回 None。
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None

    services = []
    for node in root.iter():
        if node.tag.split("}")[-1] != "service":
            continue
        stype, control = "", ""
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "serviceType":
                stype = (child.text or "").strip()
            elif tag == "controlURL":
                control = (child.text or "").strip()
        if stype and control:
            services.append((stype, control))

    # 优先 WANIPConnection（版本不限），其次 WANPPPConnection（PPPoE 拨号）
    for prefix in (WAN_IP_PREFIX, WAN_PPP_PREFIX):
        for stype, control in services:
            if stype.startswith(prefix):
                return stype, urllib.parse.urljoin(location, control)
    return None


def discover_gateway(timeout=3.0):
    """返回 {"service","control","host"}；没有可用网关时抛 UpnpError"""
    locations = _ssdp_locations(timeout)
    if not locations:
        raise UpnpError(
            "没有发现支持 UPnP 的路由器（可能未开启 UPnP，或被防火墙拦截）")
    for loc in locations:
        try:
            text = _fetch_xml(loc)
        except Exception:
            continue
        found = _find_wan_service(text, loc)
        if found:
            stype, control = found
            host = urllib.parse.urlparse(loc).hostname or ""
            return {"service": stype, "control": control, "host": host}
    raise UpnpError("发现了路由器，但它没有提供 WAN 端口映射服务")


def _xml_escape(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _soap(control, service, action, args, timeout=6.0):
    """调用一次 UPnP SOAP 动作，失败抛 UpnpError（尽量带网关返回的原因）"""
    body_parts = []
    for key, val in args:
        body_parts.append("<%s>%s</%s>" % (key, _xml_escape(val), key))
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><u:%s xmlns:u=\"%s\">%s</u:%s></s:Body></s:Envelope>"
    ) % (action, service, "".join(body_parts), action)

    req = urllib.request.Request(control, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", 'text/xml; charset="utf-8"')
    req.add_header("SOAPAction", '"%s#%s"' % (service, action))
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        m = re.search(r"<errorDescription>(.*?)</errorDescription>", detail, re.S)
        reason = m.group(1).strip() if m else "网关返回 HTTP %s" % e.code
        raise UpnpError(reason)
    except Exception as e:
        raise UpnpError("网关无响应：%s" % e)


def _add_mapping(gw, port, local_ip):
    return _soap(gw["control"], gw["service"], "AddPortMapping", [
        ("NewRemoteHost", ""),
        ("NewExternalPort", port),
        ("NewProtocol", "TCP"),
        ("NewInternalPort", port),
        ("NewInternalClient", local_ip),
        ("NewEnabled", 1),
        ("NewPortMappingDescription", MAP_DESC),
        ("NewLeaseDuration", 0),
    ])


def map_port(port, local_ip=None, timeout=3.0):
    """
    把外网 TCP port 映射到本机同名端口。

    成功返回 {"ok":True,"public_ip":...,"local_ip":...,"control":...}
    失败抛 UpnpError，消息可直接展示。
    """
    port = int(port)
    gw = discover_gateway(timeout=timeout)
    local_ip = local_ip or _local_ip_towards(gw.get("host"))
    if not local_ip:
        raise UpnpError("未能确定本机在局域网中的 IP")

    try:
        _add_mapping(gw, port, local_ip)
    except UpnpError as e:
        # 718 = ConflictInMappingEntry：端口上已有旧映射，删掉重试一次
        if "718" not in str(e):
            raise
        _soap(gw["control"], gw["service"], "DeletePortMapping", [
            ("NewRemoteHost", ""),
            ("NewExternalPort", port),
            ("NewProtocol", "TCP"),
        ])
        _add_mapping(gw, port, local_ip)

    public_ip = ""
    try:
        xml = _soap(gw["control"], gw["service"], "GetExternalIPAddress", [])
        m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", xml, re.S)
        if m:
            public_ip = m.group(1).strip()
    except Exception:
        pass

    return {"ok": True, "public_ip": public_ip, "local_ip": local_ip,
            "control": gw["control"]}


def unmap_port(port, timeout=3.0):
    """删除之前添加的映射；失败静默返回 False"""
    try:
        gw = discover_gateway(timeout=timeout)
        _soap(gw["control"], gw["service"], "DeletePortMapping", [
            ("NewRemoteHost", ""),
            ("NewExternalPort", int(port)),
            ("NewProtocol", "TCP"),
        ])
        return True
    except Exception:
        return False

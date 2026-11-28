# -*- coding: utf-8 -*-
r"""
PC ↔ 手机 文件与文字传输引擎

实现方式
--------
在 PC 上启动一个局域网 HTTP 服务，手机用浏览器访问即可，**手机端不需要装任何 App**。
界面上显示地址（附带二维码），手机扫码直达。

支持的能力：
  · 手机 → PC：上传文件（支持多选、大文件流式落盘）
  · PC → 手机：把共享目录里的文件列出来供下载
  · 手机 → PC：发送文字（自动进入接收列表，可一键复制）
  · PC → 手机：桌面端发文字，手机端在网页上读取

安全设计
--------
  1. 只允许**局域网私有地址**访问（10./172.16-31./192.168./127./fe80::），
     外网来源一律拒绝
  2. 可选 PIN 码保护：设置后必须在网页输入正确 PIN 才能操作
  3. 上传大小上限可配置，防止塞满磁盘
  4. 文件名做安全清洗，杜绝 ../ 之类的路径穿越
  5. 服务默认只监听局域网，停止时立即关闭 socket

技术说明
--------
仅用标准库（http.server / socketserver / json / mimetypes 等）。
上传采用**流式 multipart 解析**，边收边写磁盘，不会把整个文件读进内存。
"""

import os
import io
import re
import json
import time
import socket
import threading
import mimetypes
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ws_server import is_websocket_request, perform_handshake


# ==================== 配置常量 ====================

DEFAULT_PORT = 8080
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024      # 单次上传上限 4GB
CHUNK = 64 * 1024
MAX_TEXT_LEN = 200 * 1024                      # 单条文字上限 200KB


def is_private_ip(ip):
    """
    判断是否为局域网/本机地址。只允许这些来源访问传输服务。
    """
    if not ip:
        return False
    ip = ip.split("%")[0]  # 去掉 IPv6 的 zone id
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    if ip.startswith("fe80:"):          # IPv6 链路本地
        return True
    if ip.startswith("192.168."):
        return True
    if ip.startswith("10."):
        return True
    if ip.startswith("169.254."):       # 链路本地
        return True
    m = re.match(r"^172\.(\d{1,3})\.", ip)
    if m:
        try:
            second = int(m.group(1))
            return 16 <= second <= 31
        except ValueError:
            return False
    return False


def port_in_use(port):
    """
    探测端口是否已有服务在监听。

    Windows 上 SO_REUSEADDR 允许两个进程同时绑定同一端口（静默"抢占"），
    仅靠 bind 失败无法发现冲突：先 bind 成功、请求却可能被旧进程接走。
    因此这里改用"能否连通"来判断，可靠区分"有人在听"和"残留 TIME_WAIT"。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def safe_filename(name):
    r"""
    清洗文件名，防止路径穿越。
    - 去掉目录部分（../、..\、绝对路径）
    - 去掉 Windows 非法字符
    - 空名回退为 unknown
    """
    if not name:
        return "unknown"
    # 统一分隔符后只取最后一段
    name = str(name).replace("\\", "/")
    name = name.split("/")[-1]
    name = name.replace("\x00", "")
    # 去掉 Windows 不允许的字符
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name or name in (".", ".."):
        return "unknown"
    # 长度限制（保留扩展名）
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[:170] + ext[:10]
    return name


def get_local_ips():
    """
    获取本机的局域网 IPv4 地址列表（优先返回看起来"最像主网卡"的那个）。
    做法：向一个外部地址做 UDP connect（不会真的发包），从而拿到默认出口网卡 IP。
    """
    ips = []
    # 主出口 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            if primary and primary != "0.0.0.0":
                ips.append(primary)
        finally:
            s.close()
    except Exception:
        pass
    # 补充：解析主机名
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and is_private_ip(ip) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


# ==================== 流式 multipart 解析 ====================

class _MultipartStream:
    """
    流式 multipart/form-data 解析器。

    只负责"边读边写"，绝不把整个请求体读进内存，因此能处理大文件。
    使用方式：
        mp = _MultipartStream(rfile, boundary, content_length)
        while True:
            headers = mp.next_part()
            if headers is None:
                break
            filename = ...
            mp.stream_to(fileobj)   # 把当前 part 的内容写入文件
    """

    def __init__(self, rfile, boundary, content_length):
        self.rfile = rfile
        self.delim = b"\r\n--" + boundary.encode("latin-1")
        self.remaining = int(content_length)
        self.buf = b""
        self._started = False

    def _fill(self, at_least=1):
        """从底层读一些数据到缓冲区"""
        if self.remaining <= 0:
            return False
        want = min(CHUNK, self.remaining)
        try:
            chunk = self.rfile.read(want)
        except Exception:
            return False
        if not chunk:
            self.remaining = 0
            return False
        self.remaining -= len(chunk)
        self.buf += chunk
        return True

    def _read_headers(self):
        """
        读取一个 part 的头部（到空行为止），返回解析后的 dict。
        返回 None 表示流结束。
        """
        # 确保缓冲区里有 \r\n\r\n，或者流已耗尽
        while b"\r\n\r\n" not in self.buf:
            if not self._fill():
                break
        idx = self.buf.find(b"\r\n\r\n")
        if idx < 0:
            return None
        raw = self.buf[:idx]
        self.buf = self.buf[idx + 4:]
        headers = {}
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            text = ""
        for line in text.split("\r\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        return headers

    def next_part(self):
        """
        前进到下一个 part，返回其头部 dict；没有更多 part 时返回 None。
        """
        if not self._started:
            # 第一段以 "--boundary" 开头（没有前置 CRLF）。
            # 注意：self.delim = b"\r\n--" + boundary，
            # 所以 delim[2:] 正好就是 b"--" + boundary，不能再补一根 "--"。
            head_delim = self.delim[2:]
            while len(self.buf) < len(head_delim):
                if not self._fill():
                    return None
            if self.buf.startswith(head_delim):
                self.buf = self.buf[len(head_delim):]
            self._started = True
            # 结束后可能是 \r\n（还有 part）或 --（结束）
            while len(self.buf) < 2:
                if not self._fill():
                    return None
            if self.buf.startswith(b"--"):
                return None
            if self.buf.startswith(b"\r\n"):
                self.buf = self.buf[2:]
            return self._read_headers()

        # 后续 part：此时 buf 应该以 "--" 或 "\r\n" 开头（已在上次 stream 后定位）
        while len(self.buf) < 2:
            if not self._fill():
                return None
        if self.buf.startswith(b"--"):
            return None
        if self.buf.startswith(b"\r\n"):
            self.buf = self.buf[2:]
        return self._read_headers()

    def stream_to(self, out_file, progress_cb=None):
        """
        把当前 part 的内容写入 out_file，直到遇到下一个 boundary。
        返回写入的字节数。
        """
        written = 0
        tail_len = len(self.delim)
        while True:
            idx = self.buf.find(self.delim)
            if idx >= 0:
                # boundary 之前的内容就是本 part 的数据
                data = self.buf[:idx]
                if data:
                    out_file.write(data)
                    written += len(data)
                # 定位到 boundary 之后，交给 next_part 处理
                self.buf = self.buf[idx + tail_len:]
                return written
            # 没找到 boundary：把安全的部分写出去，保留可能跨块的尾部
            if len(self.buf) > tail_len:
                data = self.buf[:-tail_len]
                out_file.write(data)
                written += len(data)
                self.buf = self.buf[-tail_len:]
            if not self._fill():
                # 流结束仍未遇到 boundary，把剩余全写出
                if self.buf:
                    out_file.write(self.buf)
                    written += len(self.buf)
                    self.buf = b""
                return written
            if progress_cb:
                progress_cb(written)


def parse_content_disposition(value):
    """
    从 Content-Disposition 里解析 filename 和 name。
    兼容 filename*=UTF-8''xxx 的写法。
    """
    filename = ""
    field = ""
    if not value:
        return field, filename
    m = re.search(r'name="([^"]*)"', value)
    if m:
        field = m.group(1)
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        try:
            filename = urllib.parse.unquote(m.group(1))
        except Exception:
            filename = m.group(1)
    if not filename:
        m = re.search(r'filename="([^"]*)"', value)
        if m:
            filename = m.group(1)
    return field, filename


# ==================== 手机端网页 ====================

MOBILE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>传输助手</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     background:#f0f4f8;color:#262626;padding:14px 12px 40px}
h1{font-size:19px;margin:4px 0 14px;text-align:center;color:#1890ff}
.tabs{display:flex;background:#fff;border-radius:10px;overflow:hidden;margin-bottom:14px;
      box-shadow:0 1px 4px rgba(0,0,0,.06)}
.tab{flex:1;text-align:center;padding:12px 0;font-size:14px;color:#8c8c8c;cursor:pointer;
     transition:.2s}
.tab.on{color:#1890ff;font-weight:600;background:#e6f7ff;border-bottom:2px solid #1890ff}
.panel{display:none}
.panel.on{display:block}
.card{background:#fff;border-radius:10px;padding:14px;margin-bottom:12px;
      box-shadow:0 1px 4px rgba(0,0,0,.06)}
textarea{width:100%;height:130px;border:1px solid #e8e8e8;border-radius:8px;padding:10px;
         font-size:15px;font-family:inherit;resize:vertical;outline:none}
textarea:focus{border-color:#1890ff}
button{width:100%;padding:13px;border:0;border-radius:8px;background:#1890ff;color:#fff;
       font-size:15px;font-weight:600;margin-top:10px;cursor:pointer}
button:active{background:#096dd9}
button.gray{background:#f5f5f5;color:#595959}
button:disabled{background:#d9d9d9;color:#fff}
.filebox{border:2px dashed #d9d9d9;border-radius:10px;padding:26px 14px;text-align:center;
         color:#8c8c8c;font-size:14px;position:relative}
.filebox input{position:absolute;inset:0;opacity:0;width:100%;height:100%;cursor:pointer}
.item{display:flex;align-items:center;justify-content:space-between;padding:11px 0;
      border-bottom:1px solid #f0f0f0;font-size:14px}
.item:last-child{border-bottom:0}
.item .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:10px}
.item .sz{color:#8c8c8c;font-size:12px;margin-right:10px;flex-shrink:0}
.item a{color:#1890ff;text-decoration:none;font-size:13px;flex-shrink:0}
.txtitem{background:#fafafa;border-radius:8px;padding:10px;margin-bottom:8px;font-size:14px;
         word-break:break-all;white-space:pre-wrap}
.txtitem .t{color:#bfbfbf;font-size:11px;margin-bottom:4px}
.empty{text-align:center;color:#bfbfbf;font-size:13px;padding:20px 0}
.bar{height:6px;background:#f0f0f0;border-radius:3px;overflow:hidden;margin-top:10px;display:none}
.bar .in{height:100%;width:0;background:#1890ff;transition:width .2s}
.toast{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);background:rgba(0,0,0,.8);
       color:#fff;padding:11px 20px;border-radius:8px;font-size:14px;opacity:0;
       transition:.3s;pointer-events:none;z-index:99}
.toast.on{opacity:1}
.pin{margin-bottom:12px}
.pin input{width:100%;padding:12px;border:1px solid #e8e8e8;border-radius:8px;font-size:16px;
           text-align:center;letter-spacing:4px;outline:none}
.search{width:100%;padding:11px 12px;border:1px solid #e8e8e8;border-radius:8px;font-size:14px;
        outline:none;margin-bottom:10px;background:#fff}
.chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:8px;margin-bottom:8px}
.chip{flex:0 0 auto;padding:7px 14px;border-radius:16px;background:#fff;color:#595959;
      font-size:13px;box-shadow:0 1px 3px rgba(0,0,0,.06);cursor:pointer;white-space:nowrap}
.chip.on{background:#1890ff;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.fcard{background:#fff;border-radius:10px;padding:12px;box-shadow:0 1px 4px rgba(0,0,0,.06);
       cursor:pointer}
.fcard:active{background:#fafafa}
.fcard .ic{font-size:26px;line-height:1;margin-bottom:8px}
.fcard .fn{font-size:13px;line-height:1.35;word-break:break-all;display:-webkit-box;
           -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:35px}
.fcard .meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px;
             font-size:11px;color:#bfbfbf}
.fcard .dl{background:#e6f7ff;color:#1890ff;border-radius:6px;padding:4px 9px;font-size:12px;
           text-decoration:none;flex-shrink:0}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:200;display:none;
    flex-direction:column}
.ov.on{display:flex}
.ov .hd{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;
        color:#fff;font-size:14px;gap:10px}
.ov .hd .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov .hd .x{font-size:24px;line-height:1;padding:0 6px;cursor:pointer}
.ov .bd{flex:1;overflow:auto;display:flex;align-items:center;justify-content:center;padding:8px}
.ov .bd img,.ov .bd video{max-width:100%;max-height:100%;object-fit:contain}
.ov .bd audio{width:92%}
.ov .bd pre{color:#e6e6e6;font-size:12px;line-height:1.6;white-space:pre-wrap;
            word-break:break-all;margin:0;padding:12px;align-self:flex-start;width:100%}
.ov .bd iframe{width:100%;height:100%;border:0;background:#fff}
.ov .ft{padding:12px 16px 22px;display:flex;gap:10px;align-items:center}
.ov .ft a{flex:1;text-align:center;padding:13px;border-radius:8px;background:#1890ff;color:#fff;
          text-decoration:none;font-size:15px;font-weight:600}
.ov .ft .info{flex:0 0 auto;color:#8c8c8c;font-size:12px}
</style>
</head>
<body>
<h1>📱 ⇄ 💻 传输助手</h1>

<div id="pinbox" class="card pin" style="display:none">
  <div style="font-size:13px;color:#8c8c8c;margin-bottom:8px;text-align:center">
    请输入电脑上显示的访问码
  </div>
  <input id="pin" type="tel" inputmode="numeric" maxlength="8" placeholder="访问码">
  <button onclick="savePin()">确认</button>
</div>

<div class="tabs">
  <div class="tab on" data-t="down">电脑文件</div>
  <div class="tab" data-t="up">上传文件</div>
  <div class="tab" data-t="text">发送文字</div>
  <div class="tab" data-t="mic">麦克风</div>
</div>

<div class="panel on" id="p-down">
  <input class="search" id="q" placeholder="搜索文件名…" oninput="renderFiles()">
  <div class="chips" id="chips">
    <div class="chip on" data-k="all">全部</div>
    <div class="chip" data-k="image">图片</div>
    <div class="chip" data-k="video">视频</div>
    <div class="chip" data-k="doc">文档</div>
    <div class="chip" data-k="audio">音频</div>
    <div class="chip" data-k="other">其他</div>
  </div>
  <div class="grid" id="files"></div>
  <div id="downmsg" style="text-align:center;color:#bfbfbf;font-size:13px;padding:16px 0">加载中...</div>
  <button class="gray" onclick="loadFiles()">刷新列表</button>
</div>

<div class="panel" id="p-up">
  <div class="card">
    <div class="filebox">
      <div style="font-size:30px;margin-bottom:8px">📄</div>
      <div>点击选择文件（可多选）</div>
      <input type="file" id="file" multiple onchange="doUpload()">
    </div>
    <div class="bar" id="upbar"><div class="in" id="upin"></div></div>
    <div id="upmsg" style="font-size:13px;color:#8c8c8c;margin-top:10px;text-align:center"></div>
  </div>
</div>

<div class="panel" id="p-text">
  <div class="card">
    <textarea id="txt" placeholder="在这里输入或粘贴文字，点下方按钮发送到电脑..."></textarea>
    <button onclick="sendText()">发送到电脑</button>
    <button class="gray" onclick="loadTexts()">刷新电脑发来的文字</button>
  </div>
  <div class="card">
    <div style="font-size:13px;color:#8c8c8c;margin-bottom:8px">文字记录</div>
    <div id="texts"><div class="empty">暂无</div></div>
  </div>
</div>

<div class="panel" id="p-mic">
  <div class="card" id="micpair">
    <div style="font-size:13px;color:#8c8c8c;margin-bottom:8px;text-align:center">
      首次使用需与电脑配对：输入电脑上显示的 6 位访问码
    </div>
    <input class="search" id="linkcode" type="tel" inputmode="numeric" maxlength="6"
           placeholder="6 位访问码" style="text-align:center;letter-spacing:4px">
    <button onclick="pairLink()">配对</button>
  </div>
  <div class="card" id="micpanel" style="display:none">
    <div style="text-align:center;padding:8px 0 2px">
      <div style="font-size:40px">🎤</div>
      <div id="mictime" style="font-size:30px;font-weight:600;color:#1890ff;margin:6px 0">00:00</div>
      <div id="micst" style="font-size:13px;color:#8c8c8c">未开始</div>
      <div class="bar" id="micbar" style="display:block;margin-top:12px">
        <div class="in" id="micin" style="width:0%"></div>
      </div>
    </div>
    <button id="micbtn" onclick="toggleMic()">开始录音</button>
    <div id="michint" style="font-size:12px;color:#bfbfbf;margin-top:10px;line-height:1.7;
         text-align:left">
      声音只做实时传输，不会保存成文件。<br>
      若在电脑端选择了输出设备（如 VB-Cable 虚拟声卡），其它软件就能把手机当麦克风。
    </div>
  </div>
</div>

<div class="ov" id="ov">
  <div class="hd"><span class="nm" id="ovname"></span>
    <span class="x" onclick="closeOv()">×</span></div>
  <div class="bd" id="ovbody"></div>
  <div class="ft" id="ovfoot"></div>
</div>

<div class="toast" id="toast"></div>

<script>
var PIN = localStorage.getItem('cm_pin') || '';

function toast(m){
  var t=document.getElementById('toast'); t.textContent=m; t.classList.add('on');
  clearTimeout(window._tt); window._tt=setTimeout(function(){t.classList.remove('on')},1900);
}
function savePin(){
  PIN=document.getElementById('pin').value.trim();
  localStorage.setItem('cm_pin',PIN);
  location.reload();
}
function q(url){ return PIN ? (url + (url.indexOf('?')>=0?'&':'?') + 'pin=' + encodeURIComponent(PIN)) : url; }

document.querySelectorAll('.tab').forEach(function(el){
  el.onclick=function(){
    document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('on')});
    document.querySelectorAll('.panel').forEach(function(x){x.classList.remove('on')});
    el.classList.add('on');
    document.getElementById('p-'+el.dataset.t).classList.add('on');
    if(el.dataset.t==='down') loadFiles();
    if(el.dataset.t==='text') loadTexts();
    if(el.dataset.t==='mic') refreshMicUi();
  };
});

function doUpload(){
  var f=document.getElementById('file').files;
  if(!f.length) return;
  var fd=new FormData();
  for(var i=0;i<f.length;i++) fd.append('file', f[i]);
  var bar=document.getElementById('upbar'), inn=document.getElementById('upin');
  var msg=document.getElementById('upmsg');
  bar.style.display='block'; inn.style.width='0%'; msg.textContent='上传中...';
  var xhr=new XMLHttpRequest();
  xhr.open('POST', q('/api/upload'));
  xhr.upload.onprogress=function(e){
    if(e.lengthComputable){ inn.style.width=Math.round(e.loaded/e.total*100)+'%'; }
  };
  xhr.onload=function(){
    if(xhr.status===200){
      inn.style.width='100%';
      try{ var r=JSON.parse(xhr.responseText);
        msg.textContent='已上传 '+r.count+' 个文件'; toast('上传成功');
      }catch(e){ msg.textContent='上传成功'; toast('上传成功'); }
      document.getElementById('file').value='';
      setTimeout(function(){ bar.style.display='none'; },1200);
    } else if(xhr.status===401){
      showPin(); toast('需要访问码');
    } else {
      msg.textContent='上传失败（'+xhr.status+'）'; toast('上传失败');
    }
  };
  xhr.onerror=function(){ msg.textContent='网络错误'; toast('网络错误'); };
  xhr.send(fd);
}

function sendText(){
  var t=document.getElementById('txt').value;
  if(!t.trim()){ toast('内容为空'); return; }
  fetch(q('/api/text'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:t})})
  .then(function(r){
    if(r.status===401){ showPin(); toast('需要访问码'); return null; }
    return r.json();
  })
  .then(function(d){
    if(d){ document.getElementById('txt').value=''; toast('已发送到电脑'); }
  }).catch(function(){ toast('发送失败'); });
}

function loadTexts(){
  fetch(q('/api/texts')).then(function(r){
    if(r.status===401){ showPin(); return null; }
    return r.json();
  }).then(function(d){
    if(!d) return;
    var box=document.getElementById('texts');
    if(!d.items || !d.items.length){ box.innerHTML='<div class="empty">暂无</div>'; return; }
    box.innerHTML=d.items.map(function(it){
      var who = it.from==='pc' ? '💻 电脑' : '📱 手机';
      return '<div class="txtitem"><div class="t">'+who+' · '+it.time+'</div>'+
             esc(it.text)+'</div>';
    }).join('');
  }).catch(function(){});
}

/* ==================== 文件网格 / 筛选 / 预览 ==================== */

var FILES=[], KIND='all';

function iconOf(t){
  var m={image:'🖼️',video:'🎬',audio:'🎵',text:'📄',pdf:'📕'};
  return m[t]||'📦';
}
function kindOf(t){ return (t==='text'||t==='pdf') ? 'doc' : t; }

function loadFiles(){
  fetch(q('/api/files')).then(function(r){
    if(r.status===401){ showPin(); return null; }
    return r.json();
  }).then(function(d){
    if(!d) return;
    FILES=d.items||[];
    renderFiles();
  }).catch(function(){
    document.getElementById('files').innerHTML='';
    document.getElementById('downmsg').textContent='加载失败，请点下方刷新';
  });
}

document.querySelectorAll('#chips .chip').forEach(function(c){
  c.onclick=function(){
    document.querySelectorAll('#chips .chip').forEach(function(x){x.classList.remove('on')});
    c.classList.add('on');
    KIND=c.dataset.k;
    renderFiles();
  };
});

function renderFiles(){
  var box=document.getElementById('files');
  var msg=document.getElementById('downmsg');
  var key=(document.getElementById('q').value||'').trim().toLowerCase();
  var list=FILES.filter(function(it){
    if(KIND!=='all' && kindOf(it.type)!==KIND) return false;
    if(key && String(it.name).toLowerCase().indexOf(key)<0) return false;
    return true;
  });
  if(!list.length){
    box.innerHTML='';
    msg.textContent = FILES.length ? '没有匹配的文件' : '电脑共享目录里没有文件';
    return;
  }
  msg.textContent='共 '+list.length+' 个文件，点卡片可预览';
  box.innerHTML=list.map(function(it){
    var i=FILES.indexOf(it);
    return '<div class="fcard" onclick="openOv('+i+')">'+
      '<div class="ic">'+iconOf(it.type)+'</div>'+
      '<div class="fn">'+esc(it.name)+'</div>'+
      '<div class="meta"><span>'+it.size_text+'</span>'+
      '<a class="dl" href="'+q('/download/'+encodeURIComponent(it.name))+'" '+
      'download onclick="event.stopPropagation()">下载</a></div></div>';
  }).join('');
}

function openOv(i){
  var it=FILES[i]; if(!it) return;
  var enc=encodeURIComponent(it.name);
  var inlineUrl=q('/download/'+enc+'?inline=1');
  var dlUrl=q('/download/'+enc);
  var body=document.getElementById('ovbody');
  var foot=document.getElementById('ovfoot');
  document.getElementById('ovname').textContent=it.name;
  body.innerHTML='';
  foot.innerHTML='';

  if(it.type==='image'){
    body.innerHTML='<img src="'+inlineUrl+'" alt="预览">';
  } else if(it.type==='video'){
    body.innerHTML='<video src="'+inlineUrl+'" controls playsinline '+
                   'preload="metadata" style="max-width:100%;max-height:100%"></video>';
  } else if(it.type==='audio'){
    body.innerHTML='<audio src="'+inlineUrl+'" controls preload="metadata"></audio>';
  } else if(it.type==='pdf'){
    body.innerHTML='<iframe src="'+inlineUrl+'"></iframe>';
  } else if(it.type==='text'){
    body.innerHTML='<pre>加载中…</pre>';
    fetch(q('/api/preview?name='+enc)).then(function(r){return r.json();})
    .then(function(d){
      if(d && d.content!==undefined && d.content!==null){
        body.innerHTML='<pre>'+esc(d.content)+
          (d.truncated?'\\n\\n…… 仅显示前 8KB，完整内容请下载':'')+'</pre>';
      } else {
        body.innerHTML='<pre>无法预览该文件</pre>';
      }
    }).catch(function(){ body.innerHTML='<pre>加载失败</pre>'; });
  } else {
    body.innerHTML='<div style="color:#8c8c8c;font-size:14px;text-align:center">'+
      '<div style="font-size:44px;margin-bottom:12px">'+iconOf(it.type)+'</div>'+
      '该类型不支持在线预览<br>可下载到手机后查看</div>';
  }

  foot.innerHTML='<span class="info">'+it.size_text+'</span>'+
    '<a href="'+dlUrl+'" download>下载到手机</a>';
  document.getElementById('ov').classList.add('on');
}

function closeOv(){
  document.getElementById('ov').classList.remove('on');
  document.getElementById('ovbody').innerHTML='';
  document.getElementById('ovfoot').innerHTML='';
}

/* ==================== 手机当麦克风（浏览器录音 → 电脑） ==================== */

var TOKEN = localStorage.getItem('cm_token') || '';
var MIC = {ws:null, ctx:null, stream:null, node:null, on:false, t0:0, timer:null};

function refreshMicUi(){
  document.getElementById('micpair').style.display = TOKEN ? 'none' : 'block';
  document.getElementById('micpanel').style.display = TOKEN ? 'block' : 'none';
  // 浏览器只在安全上下文（https / localhost）里才允许录音，
  // 局域网 http 访问时直接说清楚，避免用户点了没反应以为是坏了。
  if(!window.isSecureContext){
    var h=document.getElementById('michint'), b=document.getElementById('micbtn');
    if(h){ h.innerHTML='<span style="color:#fa8c16">⚠️ 当前是 HTTP 访问，'+
      '手机浏览器出于安全限制不允许录音。<br>请改用手机 App（已支持麦克风录音）。</span>'; }
    if(b){ b.disabled=true; b.textContent='浏览器不支持记录（请用 App）'; }
  }
}

function pairLink(){
  var code=(document.getElementById('linkcode').value||'').trim();
  if(!code){ toast('请输入访问码'); return; }
  fetch(q('/api/pair'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:code,device:'手机浏览器'})})
  .then(function(r){ return r.json().then(function(d){ return {s:r.status,d:d}; }); })
  .then(function(x){
    if(x.s===200 && x.d.token){
      TOKEN=x.d.token; localStorage.setItem('cm_token',TOKEN);
      toast('配对成功'); refreshMicUi();
    } else {
      toast(x.d.error || '配对失败');
    }
  }).catch(function(){ toast('配对失败'); });
}

function toggleMic(){ if(MIC.on) stopMic(); else startMic(); }

function startMic(){
  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    toast('该浏览器不支持录音（需要 https 或 localhost）'); return;
  }
  navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,
      noiseSuppression:true,autoGainControl:true}})
  .then(function(stream){
    MIC.stream=stream;
    var AC = window.AudioContext || window.webkitAudioContext;
    var ctx = new AC();
    MIC.ctx=ctx;
    var rate = Math.round(ctx.sampleRate) || 16000;
    var src = ctx.createMediaStreamSource(stream);
    var node = ctx.createScriptProcessor(4096,1,1);
    MIC.node = node;

    var wsUrl=(location.protocol==='https:'?'wss://':'ws://')+location.host+
              '/ws?token='+encodeURIComponent(TOKEN);
    var ws=new WebSocket(wsUrl);
    MIC.ws=ws;

    ws.onopen=function(){
      ws.send(JSON.stringify({type:'mic_start',rate:rate,channels:1,bits:16}));
      MIC.on=true; MIC.t0=Date.now();
      document.getElementById('micbtn').textContent='停止录音';
      document.getElementById('micst').textContent='录音中 · '+rate+' Hz';
      MIC.timer=setInterval(function(){
        var s=Math.floor((Date.now()-MIC.t0)/1000);
        document.getElementById('mictime').textContent=
          ('0'+Math.floor(s/60)).slice(-2)+':'+('0'+(s%60)).slice(-2);
      },500);
    };
    ws.onmessage=function(ev){
      try{
        var m=JSON.parse(ev.data);
        if(m.type==='mic_ack'){
          if(!m.ok){ toast(m.message||'电脑端无法开始录音'); stopMic(); }
          else if(m.playing){ document.getElementById('micst').textContent=
            '录音中 · 已同时输出到电脑声卡'; }
        }
      }catch(e){}
    };
    ws.onerror=function(){ toast('麦克风连接失败，请检查配对'); };
    ws.onclose=function(){ if(MIC.on){ toast('连接已断开'); stopMic(); } };

    var pcm=new Int16Array(4096);
    node.onaudioprocess=function(e){
      var inp=e.inputBuffer.getChannelData(0), n=inp.length, peak=0, i, v;
      for(i=0;i<n;i++){
        v=inp[i];
        if(v>1)v=1; else if(v<-1)v=-1;
        if(v<0 ? -v>peak : v>peak) peak = v<0?-v:v;
        pcm[i] = v<0 ? v*32768 : v*32767;
      }
      document.getElementById('micin').style.width=
        Math.min(100, Math.round(peak*140))+'%';
      if(MIC.on && ws.readyState===1 && ws.bufferedAmount<262144){
        ws.send(pcm.buffer);
      }
    };

    var mute=ctx.createGain();
    mute.gain.value=0;            // 静音接到 output，仅用于驱动 ScriptProcessor
    src.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);
    if(ctx.state==='suspended'){ ctx.resume(); }
  }).catch(function(err){
    toast('无法打开麦克风：'+((err&&err.message)||err));
  });
}

function stopMic(){
  MIC.on=false;
  if(MIC.timer){ clearInterval(MIC.timer); MIC.timer=null; }
  try{ if(MIC.ws && MIC.ws.readyState===1){
        MIC.ws.send(JSON.stringify({type:'mic_stop'})); } }catch(e){}
  try{ if(MIC.ws) MIC.ws.close(); }catch(e){}
  try{ if(MIC.node) MIC.node.disconnect(); }catch(e){}
  try{ if(MIC.stream){ MIC.stream.getTracks().forEach(function(t){t.stop();}); } }catch(e){}
  try{ if(MIC.ctx) MIC.ctx.close(); }catch(e){}
  MIC.ws=null; MIC.node=null; MIC.stream=null; MIC.ctx=null;
  var b=document.getElementById('micbtn');
  if(b) b.textContent='开始录音';
  var s=document.getElementById('micst');
  if(s) s.textContent='已停止';
  var i=document.getElementById('micin');
  if(i) i.style.width='0%';
}

function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function showPin(){ document.getElementById('pinbox').style.display='block'; }

if(window.__NEED_PIN__){ showPin(); }
loadFiles();
refreshMicUi();
setInterval(function(){
  if(document.getElementById('p-text').classList.contains('on')) loadTexts();
},5000);
</script>
</body>
</html>
"""


# ==================== 传输引擎 ====================

class TransferEngine:
    """局域网文件与文字传输服务"""

    def __init__(self, save_dir=None, port=DEFAULT_PORT, pin="",
                 progress_callback=None, event_callback=None):
        """
        save_dir       : 接收文件的保存目录
        port           : 监听端口，0 表示自动选择空闲端口
        pin            : 访问码，空字符串表示不需要
        progress_callback : 进度回调 (percent, message)
        event_callback : 事件回调，收到文件/文字时触发，用于桌面端刷新
        """
        self.save_dir = save_dir or self._load_saved_dir() or self._default_save_dir()
        self.port = port
        self.pin = (pin or "").strip()
        self.progress_callback = progress_callback
        self.event_callback = event_callback

        self._server = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

        self.received_files = []     # [{"name","size","time","path"}]
        self.received_texts = []     # [{"text","time"}]
        self.outgoing_texts = []     # 桌面端发给手机的文字
        self.sent_files = []         # 桌面端主动发给手机的文件记录
        self.allow_wan = False       # 是否允许公网来源访问（需配合访问码）
        self.link = None             # 由 LinkService 注入（配对 + WebSocket）

    # ---------- 基础 ----------

    @staticmethod
    def _default_save_dir():
        """默认保存到 用户目录\\Downloads\\C盘清理大师接收"""
        base = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                            "Downloads", "C盘清理大师接收")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = os.environ.get("TEMP", ".")
        return base

    @staticmethod
    def _settings_path():
        """接收目录等设置存放位置（与配对数据同级，避免写进接收目录）"""
        base = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                            "CleanMasterBackup")
        return os.path.join(base, "transfer_settings.json")

    def _load_saved_dir(self):
        """读取上次选择的接收目录；目录不存在时回退默认目录"""
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = (data.get("save_dir") or "").strip()
            if saved and os.path.isdir(saved):
                return saved
        except Exception:
            pass
        return ""

    def _save_saved_dir(self):
        try:
            path = self._settings_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"save_dir": self.save_dir}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _emit(self, percent, message):
        if self.progress_callback:
            try:
                self.progress_callback(percent, message)
            except Exception:
                pass

    def _notify(self):
        if self.event_callback:
            try:
                self.event_callback()
            except Exception:
                pass

    def set_save_dir(self, path):
        """修改保存目录，并持久化到本机设置，下次启动/切页不再重置"""
        if not path:
            return False, "目录不能为空"
        path = os.path.abspath(os.path.expanduser(str(path).strip()))
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return False, f"无法创建目录：{e}"
        self.save_dir = path
        self._save_saved_dir()
        return True, path

    # ---------- 服务控制 ----------

    def get_url(self):
        """返回供手机访问的地址（取第一个局域网 IP）"""
        ips = get_local_ips()
        if not ips:
            return ""
        port = self._server.server_address[1] if self._server else self.port
        return f"http://{ips[0]}:{port}"

    def start(self):
        """启动服务，返回 (成功, 提示信息)"""
        if self._running:
            return False, "服务已在运行"

        engine = self

        class Handler(_TransferHandler):
            pass

        Handler.engine = engine

        # 端口选择：port=0 交给系统分配；否则从指定端口往后找空闲端口。
        # 必须跳过"已被监听"的端口 —— Windows 的 SO_REUSEADDR 会让 bind 静默成功，
        # 结果请求被旧进程接走、新服务形同虚设。
        if self.port:
            candidates = [self.port] + [p for p in range(DEFAULT_PORT, DEFAULT_PORT + 20)
                                        if p != self.port]
        else:
            candidates = [0] + list(range(DEFAULT_PORT, DEFAULT_PORT + 20))

        last_err = ""
        for try_port in candidates:
            if try_port and port_in_use(try_port):
                last_err = f"端口 {try_port} 已被占用"
                continue
            try:
                httpd = ThreadingHTTPServer(("0.0.0.0", try_port), Handler)
                httpd.daemon_threads = True
                self._server = httpd
                break
            except OSError as e:
                last_err = str(e)
                continue

        if self._server is None:
            return False, f"端口被占用，启动失败：{last_err}"

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

        url = self.get_url()
        pin_hint = f"，访问码 {self.pin}" if self.pin else ""
        return True, f"服务已启动：{url}{pin_hint}"

    def _serve(self):
        try:
            self._server.serve_forever(poll_interval=0.5)
        except Exception:
            pass
        finally:
            self._running = False

    def stop(self):
        """停止服务"""
        if not self._server:
            return False, "服务未运行"
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._running = False
        return True, "服务已停止"

    def is_running(self):
        return self._running

    # ---------- 文字 ----------

    def add_outgoing_text(self, text):
        """桌面端发文字给手机"""
        text = (text or "").strip()
        if not text:
            return False, "文字不能为空"
        if len(text.encode("utf-8")) > MAX_TEXT_LEN:
            return False, "文字过长"
        self.outgoing_texts.append({
            "text": text,
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "ts": time.time(),
            "from": "pc",
        })
        if len(self.outgoing_texts) > 200:
            self.outgoing_texts = self.outgoing_texts[-200:]
        # 实时推给已连接的手机（App 立刻能看到，不必等轮询）
        link = self.link
        if link is not None:
            try:
                link.push_text(text)
            except Exception:
                pass
        return True, "已发送"

    def record_sent_file(self, name, size, path=""):
        """电脑主动发给手机的文件：只做记录，文件本身已在共享目录"""
        self.sent_files.append({
            "name": name,
            "size": int(size or 0),
            "path": path,
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "ts": time.time(),
        })
        if len(self.sent_files) > 300:
            self.sent_files = self.sent_files[-300:]

    def get_messages(self, limit=200):
        """
        统一的会话流：文字和文件按时间合并在一条时间线上。
        dir: "pc->phone" 表示自己发出的，"phone->pc" 表示手机发来的。
        """
        msgs = []
        for t in self.received_texts:
            msgs.append({"kind": "text", "dir": "phone->pc",
                         "device": t.get("device") or "手机",
                         "text": t.get("text", ""), "time": t.get("time", ""),
                         "ts": t.get("ts", 0)})
        for t in self.outgoing_texts:
            msgs.append({"kind": "text", "dir": "pc->phone", "device": "电脑",
                         "text": t.get("text", ""), "time": t.get("time", ""),
                         "ts": t.get("ts", 0)})
        for f in self.received_files:
            msgs.append({"kind": "file", "dir": "phone->pc",
                         "device": f.get("device") or "手机",
                         "name": f.get("name", ""), "size": f.get("size", 0),
                         "size_text": self._human_size(f.get("size", 0)),
                         "path": f.get("path", ""), "time": f.get("time", ""),
                         "ts": f.get("ts", 0)})
        for f in self.sent_files:
            msgs.append({"kind": "file", "dir": "pc->phone", "device": "电脑",
                         "name": f.get("name", ""), "size": f.get("size", 0),
                         "size_text": self._human_size(f.get("size", 0)),
                         "path": f.get("path", ""), "time": f.get("time", ""),
                         "ts": f.get("ts", 0)})
        msgs.sort(key=lambda x: x.get("ts") or 0)
        return msgs[-limit:]

    @staticmethod
    def _human_size(n):
        try:
            n = float(n)
        except Exception:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
            n /= 1024.0
        return "%.1f TB" % n

    def get_all_texts(self, limit=50):
        """合并手机发来与电脑发出的文字，按时间倒序"""
        merged = []
        for t in self.received_texts:
            merged.append({"text": t["text"], "time": t["time"], "from": "phone"})
        for t in self.outgoing_texts:
            merged.append({"text": t["text"], "time": t["time"], "from": "pc"})
        merged.sort(key=lambda x: x["time"], reverse=True)
        return merged[:limit]

    # ---------- 文件列表 ----------

    def list_shared_files(self):
        """列出接收目录里的文件，供手机下载"""
        items = []
        try:
            with os.scandir(self.save_dir) as it:
                for e in it:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    items.append({
                        "name": e.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
        except (OSError, PermissionError):
            return []
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items


# ==================== HTTP 处理器 ====================

class _TransferHandler(BaseHTTPRequestHandler):
    """处理手机端的 HTTP 请求"""

    engine = None            # 由 TransferEngine.start 注入
    server_version = "CleanMasterTransfer/1.0"

    # 必须声明 HTTP/1.1：
    #   1) WebSocket 升级要求 HTTP/1.1，HTTP/1.0 的 101 响应会被 OkHttp 等
    #      严格客户端直接拒绝，导致手机端连不上实时通道
    #   2) 配合 Content-Length 正确设置，keep-alive 才能正常工作
    protocol_version = "HTTP/1.1"

    # 关闭默认的日志输出，避免刷屏
    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------

    def _client_ok(self):
        """只允许局域网访问；开了跨网络模式才放行公网来源（仍需访问码/token）"""
        try:
            ip = self.client_address[0]
        except Exception:
            return False
        if is_private_ip(ip):
            return True
        return bool(getattr(self.engine, "allow_wan", False))

    def _is_app_client(self):
        """识别手机 App（浏览器不带这个标识）"""
        if (self.headers.get("X-Client", "") or "").strip().lower() == "app":
            return True
        ua = self.headers.get("User-Agent", "") or ""
        return "CleanMasterLink" in ua

    def _pin_ok(self, query):
        """校验访问码 / App token"""
        # App 已配对时优先用 token 认证：这样即使用户给浏览器设了 PIN，
        # 也不会把已配对的 App 挡在门外（否则上传/列表/下载全 401）。
        link = getattr(self.engine, "link", None)
        tok = ""
        if link is not None:
            tok = (query.get("token") or [""])[0]
            if not tok:
                tok = self.headers.get("X-Transfer-Token", "") or ""
            if tok:
                if link.pairing.check_token(tok):
                    return True
                # 带了 token 但已被吊销 / 失效：直接拒绝。
                # 这正是"断开连接后旧 App 还继续轮询、发消息"要被堵住的入口。
                return False

        # App 必须持有有效 token：断开连接时电脑会吊销 token，
        # 这样"断开后还能轮询消息 / 继续发消息"的路子就被堵住了。
        if self._is_app_client():
            return False

        pin = getattr(self.engine, "pin", "") or ""
        if not pin:
            return True
        # 从 query 或 header 里取
        given = ""
        if "pin" in query:
            given = (query.get("pin") or [""])[0]
        if not given:
            given = self.headers.get("X-Transfer-Pin", "")
        if not given:
            # 从 cookie 里取
            cookie = self.headers.get("Cookie", "") or ""
            m = re.search(r"cm_pin=([^;]+)", cookie)
            if m:
                given = urllib.parse.unquote(m.group(1))
        return given == pin

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_body(self, limit):
        """读取请求体，超过上限返回 None"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > limit:
            return None
        return length

    @staticmethod
    def _size_text(n):
        """人类可读的体积"""
        try:
            n = float(n)
        except Exception:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} TB"

    # ---------- 路由 ----------

    def do_GET(self):
        if not self._client_ok():
            self._send_json({"error": "仅允许局域网访问"}, 403)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # WebSocket 实时通道（剪贴板 / 通知）
        if path == "/ws" and is_websocket_request(self.headers):
            self._handle_ws(query)
            return

        # 首页不需要 PIN（先让页面打开，再由页面提示输入）
        if path in ("/", "/index.html"):
            self._send_html(MOBILE_HTML)
            return

        if not self._pin_ok(query):
            self._send_json({"error": "需要访问码", "need_pin": True}, 401)
            return

        if path == "/api/files":
            items = self.engine.list_shared_files()
            out = []
            for i in items:
                out.append({
                    "name": i["name"],
                    "size": i["size"],
                    "size_text": self._size_text(i["size"]),
                    "type": self._file_kind(i["name"]),
                    "mime": (mimetypes.guess_type(i["name"])[0]
                             or "application/octet-stream"),
                    "mtime": int(i.get("mtime", 0)),
                })
            self._send_json({"items": out})
            return

        if path == "/api/preview":
            name = (query.get("name") or [""])[0]
            self._send_preview(name)
            return

        if path == "/api/texts":
            items = self.engine.get_all_texts()
            self._send_json({"items": items})
            return

        if path == "/api/messages":
            # 统一会话流：文字 + 文件，按时间正序（客户端直接顺序渲染）
            self._send_json({"items": self.engine.get_messages(limit=300)})
            return

        if path.startswith("/download/"):
            name = urllib.parse.unquote(path[len("/download/"):])
            inline = (query.get("inline") or ["0"])[0] in ("1", "true", "yes")
            self._serve_file(name, inline=inline)
            return

        if path == "/api/status":
            self._send_json({
                "running": self.engine.is_running(),
                "files": len(self.engine.received_files),
                "texts": len(self.engine.received_texts),
            })
            return

        self._send_json({"error": "未找到"}, 404)

    @staticmethod
    def _file_kind(name):
        """
        按扩展名归类，供手机端决定用什么方式预览。
        手机端拿到 type 后：image 用图片查看器、video 用播放器、
        text 拉 /api/preview 显示前几 KB、audio 用播放器、other 才提示下载。
        """
        ext = os.path.splitext(name)[1].lower()
        image = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif")
        video = (".mp4", ".mkv", ".avi", ".mov", ".3gp", ".webm", ".m4v")
        audio = (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".amr")
        text = (".txt", ".md", ".log", ".json", ".xml", ".csv", ".ini",
                ".py", ".js", ".java", ".c", ".cpp", ".h", ".html", ".css", ".bat")
        if ext in image:
            return "image"
        if ext in video:
            return "video"
        if ext in audio:
            return "audio"
        if ext in text:
            return "text"
        if ext == ".pdf":
            return "pdf"
        return "other"

    def _send_preview(self, name):
        """返回文本文件的前几 KB，供手机端直接预览（不必先下载整份）"""
        safe = safe_filename(name)
        full = os.path.join(self.engine.save_dir, safe)
        if not os.path.isfile(full):
            self._send_json({"error": "文件不存在"}, 404)
            return
        kind = self._file_kind(safe)
        if kind != "text":
            self._send_json({"error": "该类型不支持文本预览", "type": kind}, 400)
            return
        try:
            size = os.path.getsize(full)
            with open(full, "rb") as f:
                raw = f.read(8192)
        except OSError as e:
            self._send_json({"error": str(e)}, 500)
            return

        content = self._decode_text(raw)
        self._send_json({
            "name": safe,
            "type": kind,
            "size": size,
            "truncated": size > 8192,
            "content": content,
        })

    @staticmethod
    def _decode_text(raw):
        """
        猜测编码并解码。

        注意：预览只读前 8KB，末尾很可能把一个多字节字符切成两半，
        严格解码会整体失败并退化成 latin-1（中文全乱）。
        所以针对 utf-8 / gbk 各试 0~3 字节的尾部截断。
        """
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw[3:].decode("utf-8", errors="replace")
        for enc in ("utf-8", "gbk"):
            for cut in range(0, 4):
                try:
                    return raw[:len(raw) - cut].decode(enc)
                except Exception:
                    continue
        return raw.decode("latin-1")

    def _serve_file(self, name, inline=False):
        """
        把共享目录里的文件发给手机（分块发送）。

        支持 Range 断点：手机播放视频、拖动进度条、iOS Safari 内联播放
        都要求服务端返回 206 Partial Content，否则只能"点击就下载"。
        inline=True 时用 inline 让浏览器直接渲染，否则按附件下载。
        """
        safe = safe_filename(name)
        full = os.path.join(self.engine.save_dir, safe)
        if not os.path.isfile(full):
            self._send_json({"error": "文件不存在"}, 404)
            return
        try:
            size = os.path.getsize(full)
        except OSError:
            self._send_json({"error": "无法读取"}, 500)
            return

        ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"

        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "") or ""
        if size > 0 and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.split(",")[0].strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:                       # bytes=-N：最后 N 字节
                    start = max(0, size - int(b))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
        length = max(0, end - start + 1)

        quoted = urllib.parse.quote(safe)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment")
                         + "; filename*=UTF-8''" + quoted)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(full, "rb") as f:
                if start:
                    f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass

    def _handle_ws(self, query):
        """
        处理 WebSocket 升级请求。

        App 必须带配对 token（?token=xxx）。没配对的连接直接拒绝，
        避免局域网内任何设备都能同步剪贴板——那等于把剪贴板公开了。
        """
        link = getattr(self.engine, "link", None)
        if link is None:
            self._send_json({"error": "协同服务未启用"}, 503)
            return

        token = (query.get("token") or [""])[0]
        if not token:
            # 兼容把 token 放在请求头里的客户端（App 早期版本/未来其它端）
            token = self.headers.get("X-Transfer-Token", "") or ""
        if not link.pairing.check_token(token):
            self._send_json({"error": "未配对或 token 无效，请先在电脑上配对",
                             "needPair": True}, 403)
            return

        session = perform_handshake(self)
        if session is None:
            return
        self.close_connection = True
        link.attach_session(session, token)

    def do_POST(self):
        if not self._client_ok():
            self._send_json({"error": "仅允许局域网访问"}, 403)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 配对接口是"用 6 位访问码换 token"的入口，本身不能要求 token；
        # 保护手段是仅局域网 / 跨网开关 + 访问码 + 成功后立即换码。
        if path == "/api/pair":
            self._handle_pair()
            return

        if not self._pin_ok(query):
            self._send_json({"error": "需要访问码", "need_pin": True}, 401)
            return

        if path == "/api/text":
            self._handle_text()
            return
        if path == "/api/upload":
            self._handle_upload()
            return

        self._send_json({"error": "未找到"}, 404)

    def _handle_pair(self):
        """
        配对接口：手机提交电脑上显示的 6 位访问码，换取长期 token。
        这个接口不走 PIN 校验（此时用户还没有 token），
        但受限于"仅局域网访问"+ 6 位码 + 配对成功后立即换码。
        """
        link = getattr(self.engine, "link", None)
        if link is None:
            self._send_json({"ok": False, "error": "协同服务未启用"}, 503)
            return

        length = self._read_body(4096)
        if length is None:
            self._send_json({"ok": False, "error": "请求为空"}, 400)
            return
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        except Exception:
            self._send_json({"ok": False, "error": "格式错误"}, 400)
            return

        code = str(data.get("code", "")).strip()
        device = str(data.get("device", "未知设备"))[:60]
        token = link.pair(code, device)
        if not token:
            self._send_json({"ok": False, "error": "访问码错误"}, 403)
            return
        self._send_json({"ok": True, "token": token})

    def _handle_text(self):
        """接收手机发来的文字"""
        length = self._read_body(MAX_TEXT_LEN)
        if length is None:
            self._send_json({"error": "内容为空或过大"}, 400)
            return
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        except Exception:
            self._send_json({"error": "格式错误"}, 400)
            return

        text = str(data.get("text", "") or "").strip()
        if not text:
            self._send_json({"error": "内容为空"}, 400)
            return

        device, token = self._sender_identity()
        with self.engine._lock:
            self.engine.received_texts.append({
                "text": text,
                "time": datetime.now().strftime("%m-%d %H:%M:%S"),
                "ts": time.time(),
                "device": device,
            })
            if len(self.engine.received_texts) > 200:
                self.engine.received_texts = self.engine.received_texts[-200:]
        # 让其它手机也能实时看到（手机之间互发）
        link = getattr(self.engine, "link", None)
        if link is not None:
            try:
                link.push_peer_text(text, device, exclude_token=token)
            except Exception:
                pass
        self.engine._notify()
        self._send_json({"ok": True})

    def _sender_identity(self):
        """从 URL / 请求头里的 token 反查设备名，拿不到就当网页访客"""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = (query.get("token") or [""])[0] or self.headers.get("X-Transfer-Token", "") or ""
        device = ""
        link = getattr(self.engine, "link", None)
        if link is not None and token:
            try:
                device = link.pairing.device_of(token) or ""
            except Exception:
                device = ""
        return (device or "网页设备"), token

    def _handle_upload(self):
        """接收手机上传的文件（流式落盘）"""
        ctype = self.headers.get("Content-Type", "") or ""
        if "multipart/form-data" not in ctype.lower():
            self._send_json({"error": "需要 multipart/form-data"}, 400)
            return

        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            self._send_json({"error": "缺少 boundary"}, 400)
            return
        boundary = m.group(1)

        length = self._read_body(MAX_UPLOAD_BYTES)
        if length is None:
            self._send_json({"error": "文件过大或请求体为空"}, 413)
            return

        try:
            os.makedirs(self.engine.save_dir, exist_ok=True)
        except Exception as e:
            self._send_json({"error": f"无法创建保存目录：{e}"}, 500)
            return

        mp = _MultipartStream(self.rfile, boundary, length)
        device, token = self._sender_identity()
        saved = []
        while True:
            headers = mp.next_part()
            if headers is None:
                break
            disp = headers.get("content-disposition", "")
            field, filename = parse_content_disposition(disp)
            if not filename:
                # 非文件字段，丢弃内容
                try:
                    mp.stream_to(io.BytesIO())
                except Exception:
                    break
                continue

            safe = safe_filename(filename)
            # 重名时自动加序号
            target = os.path.join(self.engine.save_dir, safe)
            if os.path.exists(target):
                stem, ext = os.path.splitext(safe)
                stamp = datetime.now().strftime("%H%M%S")
                safe = f"{stem}_{stamp}{ext}"
                target = os.path.join(self.engine.save_dir, safe)

            written = 0
            try:
                with open(target, "wb") as f:
                    written = mp.stream_to(f)
            except Exception as e:
                self._send_json({"error": f"写入失败：{e}"}, 500)
                return

            if written <= 0:
                # 空文件也保留（有些场景确实传空文件），但记录一下
                pass

            with self.engine._lock:
                self.engine.received_files.append({
                    "name": safe,
                    "size": written,
                    "time": datetime.now().strftime("%m-%d %H:%M:%S"),
                    "ts": time.time(),
                    "device": device,
                    "path": target,
                })
            # 其它手机也能实时看到这个文件（手机之间互发）
            link = getattr(self.engine, "link", None)
            if link is not None:
                try:
                    link.push_file(safe, written, from_device=device,
                                   exclude_token=token)
                except Exception:
                    pass
            saved.append({"name": safe, "size": written})

        self.engine._notify()
        self._send_json({"ok": True, "count": len(saved), "files": saved})

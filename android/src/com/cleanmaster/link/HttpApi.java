package com.cleanmaster.link;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.provider.OpenableColumns;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.List;

/**
 * PC 端 HTTP 接口的封装：配对、列文件、上传、下载。
 * 所有方法都是阻塞的，必须在子线程调用。
 */
public final class HttpApi {

    private static final int CONNECT_TIMEOUT = 6000;
    private static final int READ_TIMEOUT = 15000;

    /** 电脑端凭这两个头识别「手机 App」而不是浏览器 */
    private static final String USER_AGENT = "CleanMasterLink/1.8";

    private HttpApi() {
    }

    /** 所有请求统一加 App 标识（没 token 时电脑端会回 401） */
    private static void applyHeaders(HttpURLConnection conn) {
        conn.setRequestProperty("X-Client", "app");
        conn.setRequestProperty("User-Agent", USER_AGENT);
    }

    public interface Progress {
        void onProgress(int percent, String message);
    }

    public static class PairResult {
        public boolean ok;
        public String token = "";
        public String error = "";
    }

    public static class FileItem {
        public String name = "";
        public long size;
        public String sizeText = "";
        public String type = "other";      // image / video / audio / text / pdf / other
        public String mime = "";
        public long mtime;
    }

    /** /api/preview 的返回结果（文本预览，最多前 8KB） */
    public static class PreviewResult {
        public String name = "";
        public String type = "text";
        public long size;
        public boolean truncated;
        public String content = "";
    }

    /**
     * /api/messages 里的一条会话消息。
     * kind = text / file；dir = pc->phone（电脑发的）/ phone->pc（手机发的）；
     * device = 发送方设备名（电脑发的可能为「电脑」或空）。
     */
    public static class MsgItem {
        public String kind = "";
        public String dir = "";
        public String device = "";
        public String text = "";
        public String name = "";
        public long size;
        public String sizeText = "";
        public String path = "";
        public String time = "";
        public double ts;
    }

    // ==================== 配对 ====================

    /**
     * POST /api/pair  {"code":"123456","device":"Xiaomi 13"}
     * 成功返回 token；失败返回 error 文案。
     */
    public static PairResult pair(String ip, int port, String code, String device) throws IOException {
        PairResult res = new PairResult();
        HttpURLConnection conn = (HttpURLConnection) new URL(
                "http://" + ip + ":" + port + "/api/pair").openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestMethod("POST");
            applyHeaders(conn);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");

            JSONObject body = new JSONObject();
            try {
                body.put("code", code == null ? "" : code.trim());
                body.put("device", device == null ? "" : device);
            } catch (Exception e) {
                throw new IOException("构造请求失败");
            }
            byte[] data = body.toString().getBytes("UTF-8");
            conn.setFixedLengthStreamingMode(data.length);

            OutputStream os = conn.getOutputStream();
            os.write(data);
            os.flush();
            os.close();

            int httpCode = conn.getResponseCode();
            InputStream is = httpCode >= 400 ? conn.getErrorStream() : conn.getInputStream();
            String text = readAll(is);
            try {
                JSONObject o = new JSONObject(text);
                res.ok = o.optBoolean("ok", false);
                res.token = o.optString("token", "");
                res.error = o.optString("error", "");
            } catch (Exception e) {
                res.ok = false;
                res.error = "响应格式错误(HTTP " + httpCode + ")";
            }
            if (res.ok && res.token.isEmpty()) {
                res.ok = false;
                res.error = "服务端未返回 token";
            }
            if (!res.ok && res.error.isEmpty()) {
                res.error = "配对失败(HTTP " + httpCode + ")";
            }
            return res;
        } finally {
            conn.disconnect();
        }
    }

    // ==================== 文件列表 ====================

    /**
     * 当前配对 token。
     * 电脑端如果设置了浏览器访问码（PIN），未带 token 的请求会全部 401；
     * 而 App 早已配对过，带上 token 就能免 PIN。
     */
    private static volatile String sToken = "";

    public static void setToken(String token) {
        sToken = token == null ? "" : token;
    }

    private static String withToken(String url) {
        String t = sToken;
        if (t == null || t.isEmpty()) {
            return url;
        }
        try {
            String sep = url.indexOf('?') >= 0 ? "&" : "?";
            return url + sep + "token=" + URLEncoder.encode(t, "UTF-8");
        } catch (Exception e) {
            return url;
        }
    }

    public static List<FileItem> listFiles(String ip, int port) throws IOException {
        List<FileItem> out = new ArrayList<>();
        HttpURLConnection conn = (HttpURLConnection) new URL(
                withToken("http://" + ip + ":" + port + "/api/files")).openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestMethod("GET");
            applyHeaders(conn);
            int code = conn.getResponseCode();
            if (code != 200) {
                throw new IOException("HTTP " + code + " " + readAll(conn.getErrorStream()));
            }
            String text = readAll(conn.getInputStream());
            JSONObject o = new JSONObject(text);
            JSONArray arr = o.optJSONArray("items");
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject it = arr.optJSONObject(i);
                    if (it == null) {
                        continue;
                    }
                    FileItem f = new FileItem();
                    f.name = it.optString("name", "");
                    f.size = it.optLong("size", 0);
                    f.sizeText = it.optString("size_text", "");
                    f.type = it.optString("type", "");
                    if (f.type.isEmpty()) {
                        f.type = "other";
                    }
                    f.mime = it.optString("mime", "");
                    f.mtime = it.optLong("mtime", 0);
                    if (!f.name.isEmpty()) {
                        out.add(f);
                    }
                }
            }
            return out;
        } catch (org.json.JSONException e) {
            throw new IOException("文件列表格式错误");
        } finally {
            conn.disconnect();
        }
    }

    // ==================== 统一会话流 ====================

    /**
     * GET /api/messages
     * 电脑端把「文字」和「文件」按时间合并成一条会话流，按 ts 正序返回（旧→新，最多 300 条）。
     */
    public static List<MsgItem> listMessages(String ip, int port) throws IOException {
        List<MsgItem> out = new ArrayList<>();
        HttpURLConnection conn = (HttpURLConnection) new URL(
                withToken("http://" + ip + ":" + port + "/api/messages")).openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestMethod("GET");
            applyHeaders(conn);
            int code = conn.getResponseCode();
            if (code != 200) {
                throw new IOException("HTTP " + code + " " + readAll(conn.getErrorStream()));
            }
            JSONObject o = new JSONObject(readAll(conn.getInputStream()));
            JSONArray arr = o.optJSONArray("items");
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject it = arr.optJSONObject(i);
                    if (it == null) {
                        continue;
                    }
                    MsgItem m = new MsgItem();
                    m.kind = it.optString("kind", "");
                    m.dir = it.optString("dir", "");
                    m.device = it.optString("device", "");
                    m.text = it.optString("text", "");
                    m.name = it.optString("name", "");
                    m.size = it.optLong("size", 0);
                    m.sizeText = it.optString("size_text", "");
                    m.path = it.optString("path", "");
                    m.time = it.optString("time", "");
                    m.ts = it.optDouble("ts", 0);
                    if ("text".equals(m.kind) && m.text.isEmpty()) {
                        continue;
                    }
                    if ("file".equals(m.kind) && m.name.isEmpty()) {
                        continue;
                    }
                    if (!"text".equals(m.kind) && !"file".equals(m.kind)) {
                        continue;
                    }
                    out.add(m);
                }
            }
            return out;
        } catch (org.json.JSONException e) {
            throw new IOException("会话列表格式错误");
        } finally {
            conn.disconnect();
        }
    }

    // ==================== 文字互传 ====================


    /**
     * POST /api/text  {"text":"..."}
     * WebSocket 不可用时的 HTTP 回退通道。
     *
     * @return 电脑端是否确认接收
     */
    public static boolean sendText(String ip, int port, String token, String text)
            throws IOException {
        String url = "http://" + ip + ":" + port + "/api/text" + tokenQuery(token);
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestMethod("POST");
            applyHeaders(conn);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");

            JSONObject body = new JSONObject();
            try {
                body.put("text", text == null ? "" : text);
            } catch (Exception e) {
                throw new IOException("构造请求失败");
            }
            byte[] data = body.toString().getBytes("UTF-8");
            conn.setFixedLengthStreamingMode(data.length);

            OutputStream os = conn.getOutputStream();
            os.write(data);
            os.flush();
            os.close();

            int code = conn.getResponseCode();
            String resp = readAll(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code != 200) {
                throw new IOException("HTTP " + code + " " + resp);
            }
            try {
                return new JSONObject(resp).optBoolean("ok", true);
            } catch (Exception e) {
                return true;
            }
        } finally {
            conn.disconnect();
        }
    }

    /** 根据 token 拼接查询串（配对过的电脑用 token 免 PIN） */
    private static String tokenQuery(String token) throws IOException {
        if (token == null || token.isEmpty()) {
            return "";
        }
        return "?token=" + URLEncoder.encode(token, "UTF-8");
    }

    // ==================== 文本预览 ====================

    /**
     * GET /api/preview?name=&lt;urlencode 后的文件名&gt;
     * 返回前 8KB 文本内容（是否截断由 truncated 标记）。
     */
    public static PreviewResult preview(String ip, int port, String name) throws IOException {
        PreviewResult res = new PreviewResult();
        String url = withToken("http://" + ip + ":" + port + "/api/preview?name="
                + URLEncoder.encode(name == null ? "" : name, "UTF-8"));
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestMethod("GET");
            applyHeaders(conn);
            int code = conn.getResponseCode();
            if (code != 200) {
                throw new IOException("HTTP " + code + " " + readAll(conn.getErrorStream()));
            }
            JSONObject o = new JSONObject(readAll(conn.getInputStream()));
            res.name = o.optString("name", name);
            res.type = o.optString("type", "");
            res.size = o.optLong("size", 0);
            res.truncated = o.optBoolean("truncated", false);
            res.content = o.optString("content", "");
            return res;
        } catch (org.json.JSONException e) {
            throw new IOException("预览内容格式错误");
        } finally {
            conn.disconnect();
        }
    }

    // ==================== 预览用缓存下载 ====================

    /**
     * 把远端文件下载到应用缓存目录（不落用户下载目录），供图片 / 音视频预览使用。
     * 缓存文件在系统空间紧张时会被自动清理。
     */
    public static File downloadToCache(Context context, String ip, int port, String name)
            throws IOException {
        String url = withToken("http://" + ip + ":" + port + "/download/" + encodePath(name));
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(60000);
            conn.setRequestMethod("GET");
            applyHeaders(conn);
            int code = conn.getResponseCode();
            if (code != 200) {
                throw new IOException("HTTP " + code);
            }
            InputStream in = conn.getInputStream();
            if (in == null) {
                throw new IOException("响应为空");
            }
            File dir = new File(context.getCacheDir(), "preview");
            if (!dir.exists() && !dir.mkdirs()) {
                throw new IOException("无法创建缓存目录");
            }
            File out = new File(dir, sanitizeName(name));
            long written = copy(in, new FileOutputStream(out));
            if (written <= 0) {
                throw new IOException("文件内容为空");
            }
            return out;
        } finally {
            conn.disconnect();
        }
    }

    // ==================== 上传 ====================

    /**
     * POST /api/upload，multipart/form-data，字段名固定为 file，可多文件。
     *
     * @return 服务端确认收到并落盘的文件个数
     */
    public static int upload(Context context, String ip, int port,
                             List<Uri> uris, Progress progress) throws IOException {
        return upload(context, ip, port, uris, progress, null);
    }

    /**
     * @param savedNames 可选；非空时把电脑端实际落盘的文件名回填进去
     *                   （重名时电脑会自动加时间戳，所以以服务端返回为准）
     */
    public static int upload(Context context, String ip, int port,
                             List<Uri> uris, Progress progress,
                             List<String> savedNames) throws IOException {
        if (uris == null || uris.isEmpty()) {
            return 0;
        }
        ContentResolver cr = context.getContentResolver();
        List<Part> parts = new ArrayList<>();
        List<File> temps = new ArrayList<>();

        try {
            for (Uri uri : uris) {
                if (uri == null) {
                    continue;
                }
                String name = queryName(cr, uri);
                String type = cr.getType(uri);
                if (type == null || type.isEmpty()) {
                    type = "application/octet-stream";
                }
                long size = querySize(cr, uri);

                if (size < 0) {
                    // 拿不到大小（部分内容提供者如此），先落到缓存文件再传，才能算出 Content-Length
                    File tmp = new File(context.getCacheDir(), "cmlink_up_" + System.nanoTime());
                    InputStream in = cr.openInputStream(uri);
                    if (in == null) {
                        continue;
                    }
                    long written = copy(in, new FileOutputStream(tmp));
                    parts.add(new Part(name, type, written, tmp, null));
                    temps.add(tmp);
                } else {
                    parts.add(new Part(name, type, size, null, uri));
                }
            }
            if (parts.isEmpty()) {
                return 0;
            }

            String boundary = "----CMLinkBoundary" + System.currentTimeMillis();
            byte[] tail = ("--" + boundary + "--\r\n").getBytes("UTF-8");

            long total = tail.length;
            for (Part p : parts) {
                p.header = ("--" + boundary + "\r\n"
                        + "Content-Disposition: form-data; name=\"file\"; filename=\""
                        + escapeFilename(p.name) + "\"\r\n"
                        + "Content-Type: " + p.type + "\r\n\r\n").getBytes("UTF-8");
                total += p.header.length + p.size + 2;
            }

            HttpURLConnection conn = (HttpURLConnection) new URL(
                    withToken("http://" + ip + ":" + port + "/api/upload")).openConnection();
            try {
                conn.setConnectTimeout(CONNECT_TIMEOUT);
                conn.setReadTimeout(60000);
                conn.setRequestMethod("POST");
                applyHeaders(conn);
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type",
                        "multipart/form-data; boundary=" + boundary);
                conn.setFixedLengthStreamingMode(total);

                OutputStream raw = conn.getOutputStream();
                BufferedOutputStream out = new BufferedOutputStream(raw, 64 * 1024);
                long sent = 0;
                int lastPercent = -1;
                byte[] buf = new byte[64 * 1024];

                for (Part p : parts) {
                    out.write(p.header);
                    sent += p.header.length;

                    InputStream src = p.file != null
                            ? new java.io.FileInputStream(p.file)
                            : cr.openInputStream(p.uri);
                    if (src == null) {
                        throw new IOException("无法读取文件: " + p.name);
                    }
                    InputStream in = new BufferedInputStream(src, 64 * 1024);
                    try {
                        int n;
                        while ((n = in.read(buf)) > 0) {
                            out.write(buf, 0, n);
                            sent += n;
                            if (progress != null && total > 0) {
                                int percent = (int) (sent * 100 / total);
                                if (percent != lastPercent) {
                                    lastPercent = percent;
                                    progress.onProgress(percent, p.name);
                                }
                            }
                        }
                    } finally {
                        try {
                            in.close();
                        } catch (Exception ignored) {
                        }
                    }
                    out.write(new byte[]{'\r', '\n'});
                    sent += 2;
                }
                out.write(tail);
                out.flush();
                out.close();

                int code = conn.getResponseCode();
                String text = readAll(code >= 400
                        ? conn.getErrorStream() : conn.getInputStream());
                if (code != 200) {
                    throw new IOException("HTTP " + code + " " + text);
                }
                try {
                    JSONObject o = new JSONObject(text);
                    if (savedNames != null) {
                        JSONArray files = o.optJSONArray("files");
                        if (files != null) {
                            for (int i = 0; i < files.length(); i++) {
                                JSONObject it = files.optJSONObject(i);
                                String n = it == null ? "" : it.optString("name", "");
                                if (!n.isEmpty()) {
                                    savedNames.add(n);
                                }
                            }
                        }
                    }
                    return o.optInt("count", parts.size());
                } catch (Exception e) {
                    return parts.size();
                }
            } finally {
                conn.disconnect();
            }
        } finally {
            for (File t : temps) {
                try {
                    t.delete();
                } catch (Exception ignored) {
                }
            }
        }
    }

    // ==================== 下载 ====================

    /**
     * GET /download/&lt;urlencode 后的文件名&gt;
     *
     * Android 10+ 用 MediaStore 写入「下载/手机互传」，
     * 更低版本写到应用专属外部目录（无需存储权限）。
     *
     * @return 保存位置的描述文案
     */
    public static String download(Context context, String ip, int port, String name)
            throws IOException {
        String url = withToken("http://" + ip + ":" + port + "/download/" + encodePath(name));
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(60000);
            conn.setRequestMethod("GET");
            applyHeaders(conn);

            int code = conn.getResponseCode();
            if (code != 200) {
                throw new IOException("HTTP " + code);
            }
            InputStream in = conn.getInputStream();
            if (in == null) {
                throw new IOException("响应为空");
            }

            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                cv.put(MediaStore.MediaColumns.MIME_TYPE, "application/octet-stream");
                cv.put(MediaStore.MediaColumns.RELATIVE_PATH,
                        Environment.DIRECTORY_DOWNLOADS + "/手机互传");
                Uri target = context.getContentResolver()
                        .insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                if (target == null) {
                    throw new IOException("无法创建下载文件");
                }
                OutputStream os = context.getContentResolver().openOutputStream(target);
                if (os == null) {
                    throw new IOException("无法写入下载文件");
                }
                copy(in, os);
                return "下载/手机互传/" + name;
            } else {
                File dir = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                if (dir == null) {
                    dir = new File(context.getFilesDir(), "downloads");
                }
                if (!dir.exists() && !dir.mkdirs()) {
                    throw new IOException("无法创建目录");
                }
                File out = new File(dir, name);
                copy(in, new FileOutputStream(out));
                return out.getAbsolutePath();
            }
        } finally {
            conn.disconnect();
        }
    }

    // ==================== 工具 ====================

    private static class Part {
        final String name;
        final String type;
        final long size;
        final File file;
        final Uri uri;
        byte[] header;

        Part(String name, String type, long size, File file, Uri uri) {
            this.name = name;
            this.type = type;
            this.size = size;
            this.file = file;
            this.uri = uri;
        }
    }

    /** 取用户选中文件的显示名（上传前记录，用于避免把自己传的文件当成「电脑传来」） */
    public static String displayName(Context context, Uri uri) {
        if (context == null || uri == null) {
            return "";
        }
        return queryName(context.getContentResolver(), uri);
    }

    private static String queryName(ContentResolver cr, Uri uri) {
        Cursor c = null;
        try {
            c = cr.query(uri, null, null, null, null);
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) {
                    String n = c.getString(idx);
                    if (n != null && !n.isEmpty()) {
                        return n;
                    }
                }
            }
        } catch (Exception ignored) {
        } finally {
            if (c != null) {
                try {
                    c.close();
                } catch (Exception ignored) {
                }
            }
        }
        String last = uri.getLastPathSegment();
        return last == null || last.isEmpty() ? "file" : last;
    }

    private static long querySize(ContentResolver cr, Uri uri) {
        Cursor c = null;
        try {
            c = cr.query(uri, null, null, null, null);
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(OpenableColumns.SIZE);
                if (idx >= 0 && !c.isNull(idx)) {
                    return c.getLong(idx);
                }
            }
        } catch (Exception ignored) {
        } finally {
            if (c != null) {
                try {
                    c.close();
                } catch (Exception ignored) {
                }
            }
        }
        return -1;
    }

    private static String escapeFilename(String name) {
        if (name == null || name.isEmpty()) {
            return "file";
        }
        return name.replace("\"", "_").replace("\\", "_")
                .replace("\r", "_").replace("\n", "_");
    }

    /** 文件名放进 URL path，空格要编码成 %20 而不是 + */
    private static String encodePath(String name) throws IOException {
        return URLEncoder.encode(name, "UTF-8").replace("+", "%20");
    }

    /** 远端文件名转成本地缓存文件名，去掉文件系统不允许的字符 */
    private static String sanitizeName(String name) {
        String safe = name == null ? "" : name.replaceAll("[\\\\/:*?\"<>|]", "_").trim();
        if (safe.isEmpty()) {
            safe = "preview_file";
        }
        if (safe.length() > 100) {
            String ext = "";
            int dot = safe.lastIndexOf('.');
            if (dot > 0 && safe.length() - dot <= 10) {
                ext = safe.substring(dot);
            }
            safe = safe.substring(0, 90) + ext;
        }
        return safe;
    }

    private static String readAll(InputStream in) throws IOException {
        if (in == null) {
            return "";
        }
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
                if (bos.size() > 1024 * 1024) {
                    break;      // 防御性上限，正常响应远小于 1MB
                }
            }
            return new String(bos.toByteArray(), "UTF-8");
        } finally {
            try {
                in.close();
            } catch (Exception ignored) {
            }
        }
    }

    private static long copy(InputStream in, OutputStream out) throws IOException {
        try {
            byte[] buf = new byte[64 * 1024];
            long total = 0;
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                total += n;
            }
            return total;
        } finally {
            try {
                in.close();
            } catch (Exception ignored) {
            }
            try {
                out.close();
            } catch (Exception ignored) {
            }
        }
    }
}

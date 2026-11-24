# -*- coding: utf-8 -*-
r"""
Android APK 手工构建脚本（不依赖 Gradle / Android Studio）

为什么手工构建
--------------
本项目只需要一个不依赖 AndroidX 的轻量 App。Gradle 方案要下载 130MB Gradle
加数百 MB 的 AGP 依赖，在这台机器上实测网络限速严重、反复失败。
而 aapt2 / javac / d8 / apksigner 这几个工具在 Android SDK + JDK 里已经具备，
直接串起来用，构建过程完全可控、可复现。

构建流程
--------
  1. aapt2 compile   编译资源为 .flat
  2. aapt2 link      链接成基础 APK 并生成 R.java
  3. javac           编译 Java 源码 + R.java
  4. d8              把 .class 转成 classes.dex
  5. 合并            classes.dex 塞进基础 APK
  6. zipalign        4 字节对齐
  7. apksigner       签名（首次自动生成 keystore）
  8. apksigner verify 校验签名

用法
----
    python build_apk.py            # 完整构建
    python build_apk.py --clean    # 清理后构建
"""

import os
import sys
import glob
import shutil
import zipfile
import subprocess
import argparse

# ==================== 路径配置 ====================

# 开源环境优先读标准环境变量，找不到再回退到作者机器的默认路径
JDK_HOME = (os.environ.get("JAVA_HOME")
            or r"C:\Program Files\Microsoft\jdk-17.0.21.102-hotspot")
SDK_ROOT = (os.environ.get("ANDROID_SDK_ROOT")
            or os.environ.get("ANDROID_HOME")
            or r"E:\Android\Sdk")
BUILD_TOOLS = os.path.join(SDK_ROOT, "build-tools", "34.0.0")
ANDROID_JAR = os.path.join(SDK_ROOT, "platforms", "android-34", "android.jar")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(PROJECT_DIR, "AndroidManifest.xml")
RES_DIR = os.path.join(PROJECT_DIR, "res")
SRC_DIR = os.path.join(PROJECT_DIR, "src")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")
OUT_APK = os.path.join(PROJECT_DIR, "CleanMasterLink.apk")

# 签名密钥不入库；首次构建会自动生成。公开发布前请换成自己的口令/别名。
KEYSTORE = os.path.join(PROJECT_DIR, "cleanmaster.keystore")
KS_PASS = os.environ.get("HS_KS_PASS", "cleanmaster")
KS_ALIAS = os.environ.get("HS_KS_ALIAS", "cleanmaster")
KS_CN = "CleanMaster Link"

MIN_SDK = "24"
TARGET_SDK = "34"

AAPT2 = os.path.join(BUILD_TOOLS, "aapt2.exe")
D8 = os.path.join(BUILD_TOOLS, "d8.bat")
APKSIGNER = os.path.join(BUILD_TOOLS, "apksigner.bat")
ZIPALIGN = os.path.join(BUILD_TOOLS, "zipalign.exe")
JAVAC = os.path.join(JDK_HOME, "bin", "javac.exe")
KEYTOOL = os.path.join(JDK_HOME, "bin", "keytool.exe")


def log(msg):
    print(msg, flush=True)


def setup_env():
    """
    构建工具（d8.bat / apksigner.bat 等）是批处理脚本，内部依赖 JAVA_HOME。
    当前进程若没有该变量会直接失败，所以这里显式注入。
    """
    os.environ["JAVA_HOME"] = JDK_HOME
    java_bin = os.path.join(JDK_HOME, "bin")
    path = os.environ.get("PATH", "")
    if java_bin.lower() not in path.lower():
        os.environ["PATH"] = java_bin + os.pathsep + path
    os.environ["ANDROID_HOME"] = SDK_ROOT
    os.environ["ANDROID_SDK_ROOT"] = SDK_ROOT


def run(cmd, cwd=None, allow_fail=False):
    """执行命令，失败时打印完整输出"""
    log("  > " + " ".join(os.path.basename(c) if i == 0 else c
                         for i, c in enumerate(cmd)))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True)

    def _dec(raw):
        """JDK 工具在中文系统上输出 GBK，需做编码兜底"""
        if not raw:
            return ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, Exception):
                continue
        return raw.decode("utf-8", errors="ignore")

    out = _dec(proc.stdout)
    err = _dec(proc.stderr)
    if proc.returncode != 0 and not allow_fail:
        log("  [失败] 退出码 %d" % proc.returncode)
        if out.strip():
            log("  --- stdout ---\n" + out[-3000:])
        if err.strip():
            log("  --- stderr ---\n" + err[-3000:])
        raise SystemExit(1)
    if out.strip() and proc.returncode == 0 and len(out) < 800:
        log("    " + out.strip()[:800])
    return out, err, proc.returncode


def check_tools():
    """检查构建所需工具是否齐全"""
    required = {
        "aapt2": AAPT2, "d8": D8, "apksigner": APKSIGNER,
        "zipalign": ZIPALIGN, "javac": JAVAC, "keytool": KEYTOOL,
        "android.jar": ANDROID_JAR,
    }
    missing = [k for k, v in required.items() if not os.path.exists(v)]
    if missing:
        log("缺少构建工具: " + ", ".join(missing))
        SystemExit(1)
    log("构建工具检查通过")


def clean():
    for d in (BUILD_DIR,):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    for f in (OUT_APK,):
        if os.path.exists(f):
            os.remove(f)
    log("已清理 build 目录与旧 APK")


def ensure_keystore():
    """首次构建时生成签名密钥"""
    if os.path.exists(KEYSTORE):
        return
    log("[0/8] 生成签名密钥 ...")
    run([KEYTOOL, "-genkeypair", "-v",
         "-keystore", KEYSTORE,
         "-alias", KS_ALIAS,
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
         "-storepass", KS_PASS, "-keypass", KS_PASS,
         "-dname", "CN=%s, OU=Dev, O=CleanMaster, L=CN, ST=CN, C=CN" % KS_CN])


def build():
    if not os.path.isdir(RES_DIR):
        log("找不到 res 目录")
        raise SystemExit(1)

    os.makedirs(BUILD_DIR, exist_ok=True)
    res_zip = os.path.join(BUILD_DIR, "resources.zip")
    base_apk = os.path.join(BUILD_DIR, "base.apk")
    gen_dir = os.path.join(BUILD_DIR, "gen")
    classes_dir = os.path.join(BUILD_DIR, "classes")
    dex_dir = os.path.join(BUILD_DIR, "dex")
    unaligned = os.path.join(BUILD_DIR, "unaligned.apk")
    aligned = os.path.join(BUILD_DIR, "aligned.apk")
    for d in (gen_dir, classes_dir, dex_dir):
        os.makedirs(d, exist_ok=True)

    # ---- 1. 编译资源 ----
    log("[1/8] aapt2 compile ...")
    run([AAPT2, "compile", "--dir", RES_DIR, "-o", res_zip])

    # ---- 2. 链接资源 + 生成 R.java ----
    log("[2/8] aapt2 link ...")
    run([AAPT2, "link",
         "-o", base_apk,
         "-I", ANDROID_JAR,
         "--manifest", MANIFEST,
         "-R", res_zip,
         "--java", gen_dir,
         "--min-sdk-version", MIN_SDK,
         "--target-sdk-version", TARGET_SDK,
         "--auto-add-overlay"])

    # ---- 3. 编译 Java ----
    log("[3/8] javac 编译 ...")
    sources = glob.glob(os.path.join(gen_dir, "**", "*.java"), recursive=True)
    sources += glob.glob(os.path.join(SRC_DIR, "**", "*.java"), recursive=True)
    if not sources:
        log("没有找到 Java 源码")
        raise SystemExit(1)
    log("    源文件 %d 个" % len(sources))
    arg_file = os.path.join(BUILD_DIR, "sources.txt")
    with open(arg_file, "w", encoding="utf-8") as f:
        for s in sources:
            f.write('"%s"\n' % s.replace("\\", "\\\\"))
    # 注意：JDK 17 在 -source/-target >= 9 时不允许再用 -boot-class-path，
    # 而 Android 必须把 android.jar 当作引导类路径。因此这里用 8（JDK 17 仍支持），
    # 这也是 AGP 传统构建方式的等价做法。
    run([JAVAC, "-encoding", "UTF-8", "-source", "8", "-target", "8",
         "-bootclasspath", ANDROID_JAR,
         "-classpath", ANDROID_JAR,
         "-nowarn",
         "-d", classes_dir,
         "@" + arg_file])

    # ---- 4. d8 生成 dex ----
    log("[4/8] d8 生成 classes.dex ...")
    class_files = glob.glob(os.path.join(classes_dir, "**", "*.class"), recursive=True)
    if not class_files:
        log("没有编译出 class 文件")
        raise SystemExit(1)
    log("    class 文件 %d 个" % len(class_files))
    d8_cmd = [D8, "--lib", ANDROID_JAR, "--min-api", MIN_SDK,
              "--output", dex_dir] + class_files
    run(d8_cmd)

    dex_file = os.path.join(dex_dir, "classes.dex")
    if not os.path.exists(dex_file):
        log("d8 未生成 classes.dex")
        raise SystemExit(1)

    # ---- 5. 合并 dex 到 APK ----
    log("[5/8] 合并 classes.dex 到 APK ...")
    shutil.copyfile(base_apk, unaligned)
    with zipfile.ZipFile(unaligned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex_file, "classes.dex")
    log("    classes.dex %.1f KB" % (os.path.getsize(dex_file) / 1024.0))

    # ---- 6. 对齐 ----
    log("[6/8] zipalign ...")
    run([ZIPALIGN, "-f", "4", unaligned, aligned])

    # ---- 7. 签名 ----
    log("[7/8] apksigner 签名 ...")
    run([APKSIGNER, "sign",
         "--ks", KEYSTORE,
         "--ks-pass", "pass:" + KS_PASS,
         "--key-pass", "pass:" + KS_PASS,
         "--ks-key-alias", KS_ALIAS,
         "--min-sdk-version", MIN_SDK,
         # 同时打 v1(JAR) 与 v2/v3：部分 ROM 的安装器只认 v1，
         # 只有 v2/v3 时会报「解析软件包时出现问题 / packageInfo is null」。
         "--v1-signing-enabled", "true",
         "--v2-signing-enabled", "true",
         "--v3-signing-enabled", "true",
         "--out", OUT_APK,
         aligned])

    # ---- 8. 校验 ----
    log("[8/8] 校验签名 ...")
    out, err, rc = run([APKSIGNER, "verify", "--verbose", OUT_APK], allow_fail=True)
    if rc != 0:
        log("  签名校验失败:\n" + (err or out)[-1500:])
        raise SystemExit(1)
    for line in (out or "").splitlines():
        if any(k in line for k in ("Verifies", "v1", "v2", "v3", "Number of signers")):
            log("    " + line.strip())

    size_mb = os.path.getsize(OUT_APK) / (1024.0 * 1024.0)
    log("")
    log("=" * 56)
    log("构建成功: %s" % OUT_APK)
    log("APK 大小: %.2f MB" % size_mb)
    log("=" * 56)


def main():
    ap = argparse.ArgumentParser(description="手工构建 Android APK")
    ap.add_argument("--clean", action="store_true", help="构建前清理")
    args = ap.parse_args()

    log("=" * 56)
    log("CleanMaster Link APK 构建")
    log("=" * 56)
    setup_env()
    check_tools()
    if args.clean:
        clean()
    ensure_keystore()
    build()


if __name__ == "__main__":
    main()

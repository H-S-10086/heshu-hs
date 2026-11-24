# 贡献指南

感谢你愿意为「合枢 HS」做贡献。无论是报 Bug、提需求、改文档还是提交代码，都很有价值。

## 提交 Issue

- 先搜索是否已有相同问题，避免重复。
- Bug 反馈请使用 [Bug 模板](.github/ISSUE_TEMPLATE/bug_report.yml)，尽量附上系统版本、复现步骤和日志。
- 需求建议请使用 [功能模板](.github/ISSUE_TEMPLATE/feature_request.yml)，说明使用场景而不是只给结论。
- 安全问题不要公开提 Issue，请按 [SECURITY.md](SECURITY.md) 的流程私下报告。

## 提交 Pull Request

1. Fork 本仓库并从 `main` 拉分支，分支名建议 `feat/xxx`、`fix/xxx`、`docs/xxx`。
2. 一个 PR 只做一件事，避免夹带无关格式化或重构。
3. 提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/)：
   - `feat: 新增设备列表双向确认`
   - `fix: 修复断开后仍能轮询消息`
   - `docs: 补充 Android 构建说明`
   - `refactor: 拆分 UDP 发送线程`
4. 提交前自测，并在 PR 描述里写清楚验证方式。

## 开发环境

### 电脑端

- Windows 10/11 x64
- Python 3.10+
- 仅标准库，不引入第三方运行时依赖
- 打包：PyInstaller；安装包：Inno Setup 6

```bash
python main.py
python -m py_compile *.py
```

### 手机端

- JDK 17（`JAVA_HOME`）
- Android SDK Platform 34、Build-Tools 34.0.0（`ANDROID_SDK_ROOT` 或 `ANDROID_HOME`）
- 无 Gradle，使用 `aapt2 → javac → d8 → zipalign → apksigner`

```bash
python android/build_apk.py --clean
```

## 代码约定

- 电脑端保持标准库实现，新增依赖需要先讨论。
- 界面文案使用中文；代码注释使用中文，解释「为什么」而不是复述代码。
- 耗时操作必须放到后台线程，禁止阻塞 tkinter 主线程。
- Android 端避免 lambda（d8 命令行有长度上限），网络与 UDP 发送必须在子线程执行。
- 涉及清理、删除、注册表修改的逻辑，必须标注来源并遵守「宁可漏清理，不可误删」的原则。

## 不要提交的内容

- `android/cleanmaster.keystore`、`*.jks` 等签名密钥
- `build/`、`dist/`、`installer_output/`、`android/build/` 等产物
- 安装包、APK、日志、个人路径、token 或访问码

## 行为准则

参与本项目即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

# Windows 打包与发布（第五阶段）

当前版本：**v0.4.0**

产物是 **onedir 免安装目录**，不是单文件 exe，也不是 MSI 安装器：

```text
dist/AIMovieFactory/
  AIMovieFactory.exe
  run.cmd              # 设置 AIMOVIE_DATA_DIR 后启动
  README.md
  _internal/           # Python、PySide6、Whisper/OpenCV/FFmpeg 绑定等
```

## 1. 构建

前置：

- 已运行 `scripts\setup_windows.cmd`
- `ffmpeg` / `ffprobe` 在 PATH（或安装后在设置中填路径）
- Windows 10/11 x64

```bat
scripts\build_windows.ps1
```

脚本会：

1. 检查 `.venv` 和 PyInstaller；缺 PyInstaller 时安装。
2. 执行 `python -m PyInstaller --noconfirm --clean aimovie.spec`。
3. 在 `dist/AIMovieFactory/` 生成 `AIMovieFactory.exe`、`_internal/`。
4. 写入 `run.cmd`（设置 `AIMOVIE_DATA_DIR=%~dp0data`）并复制 README。

构建耗时约 1 分钟；本次产物约 **487 MB**。

## 2. 为什么是 onedir

- PySide6、OpenCV、faster-whisper、ctranslate2、FFmpeg 绑定体积大；单文件每次启动都要解压，体验差。
- onedir 便于替换/升级 `_internal`，也便于排查缺失 DLL。
- 用户数据与程序分离：`run.cmd` 把数据目录设为程序目录下 `data/`，不写入 `_internal`。

## 3. 依赖与不打包项

| 项目 | 是否打包 | 说明 |
|---|---|---|
| Python / PySide6 | ✅ | `_internal` |
| faster-whisper / ctranslate2 / av | ✅ | 本地 ASR |
| OpenCV / PySceneDetect | ✅ | 镜头切分/关键帧 |
| edge-tts | ✅ | 配音客户端；仍需网络 |
| piper-tts | ✅（如已安装） | 本地语音；不附带模型 |
| FFmpeg / ffprobe | ❌ | 需系统安装；体积/许可原因 |
| Whisper 模型 | ❌ | 首次识别时下载到 `data/models/` |
| Piper 中文模型 | ❌ | 用户自行准备并确认许可 |
| DeepSeek Key | ❌ | 只从环境变量读取 |

所以目标机器必须：

1. 安装 FFmpeg/ffprobe 并加入 PATH，或在设置里指定路径。
2. 首次 ASR 时联网下载 Whisper 模型。
3. 使用 DeepSeek 时设置 `DEEPSEEK_API_KEY`。
4. 首次 Edge TTS 时联网。

## 4. 启动与自检

正常启动：

```bat
dist\AIMovieFactory\run.cmd
```

打包/依赖自检（不开主窗口，退出码 0 表示通过）：

```bat
set AIMOVIE_DATA_DIR=%CD%\data
set QT_QPA_PLATFORM=offscreen
dist\AIMovieFactory\AIMovieFactory.exe --smoke-test
echo %errorlevel%
```

自检会创建 QApplication/MainWindow，并导入 `faster_whisper`、`cv2`、`scenedetect`、`av`、`ctranslate2`、`edge_tts`；通过时日志写入 `data/logs/app.log`。

本次实测：

```text
EXIT=0
INFO aimovie.app: 打包/启动自检通过（GUI + Whisper/OpenCV/SceneDetect/AV/EdgeTTS）
```

## 5. 数据目录

`run.cmd` 设置：

```bat
set AIMOVIE_DATA_DIR=%~dp0data
```

因此程序目录下会生成：

```text
data/
  config.json
  app.db
  logs/
  models/whisper/
  projects/
```

直接双击 exe（不经过 run.cmd）时，数据目录回退到 `%LOCALAPPDATA%\AIMovieFactory`。

## 6. 发布前检查清单

- [ ] 在干净的 Windows 10/11 x64 机器/虚拟机启动
- [ ] `--smoke-test` 退出码 0
- [ ] FFmpeg/ffprobe 路径可用
- [ ] 首次 Whisper 下载成功
- [ ] 短项目跑通：分析 → 文案 → 配音 → 匹配 → 导出
- [ ] 打开成片、字幕、配音目录
- [ ] 不包含 Key、测试影片、用户数据、日志
- [ ] 版本号与 README 一致
- [ ] 分发前确认 FFmpeg/模型/Edge TTS/Piper 模型的许可与使用条款

## 7. 已知限制

- 没有 MSI/安装器/自动更新；只有免安装目录。
- 没有代码签名；Windows SmartScreen 可能提示未知发布者。
- 没有打包 FFmpeg 和 Whisper/Piper 模型；首次使用需要准备/下载。
- 包体积约 487 MB（含 Qt/OpenCV/ctranslate2/piper）。
- 没有在 Windows 10 实机验证；Windows 11 26200 验证通过。
- 视频导出依赖 libx264，目标机 FFmpeg 必须带该编码器。

## 8. 相关文件

- [`aimovie.spec`](../aimovie.spec)
- [`scripts/build_windows.ps1`](../scripts/build_windows.ps1)
- [`scripts/launcher.py`](../scripts/launcher.py)

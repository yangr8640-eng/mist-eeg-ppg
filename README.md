# MIST 脑电＋PPG 一体化实验

Windows 中文桌面程序：在同一界面连接 QX-EEG-4 脑环和 Arduino RED PPG 指夹，执行六阶段 MIST，按被试、阶段、尝试分别保存数据。

**[下载 Windows 64 位免安装运行包（v0.1.0，约 62 MB）](https://github.com/yangr8640-eng/mist-eeg-ppg/releases/download/v0.1.0/MIST-EEG-PPG-Windows-x64.zip)** · [查看 Release 与校验文件](https://github.com/yangr8640-eng/mist-eeg-ppg/releases/tag/v0.1.0)

在其他 Windows 电脑上下载 ZIP，完整解压后双击 `MIST-EEG-PPG.exe` 即可启动，不需要安装 Python，也不依赖开发电脑的文件路径。请下载上述运行包；GitHub 自动提供的 `Source code` 是开发源码。

**当前为软件验证版本，真实设备尚未验收。** 模拟设备、协议解析和流程测试不等同于硬件兼容性或同步精度验证。开发时设备暂在实验室，真实采集能力将在实际接入后确认。

## 使用

下载 Release 中的 Windows ZIP，完整解压，双击 `MIST-EEG-PPG.exe`。同目录的 `_internal` 文件夹必须保留。无需安装 Python。

已在 Windows 11 x64 验证。连接真实设备时，电脑需要可用的 BLE 蓝牙适配器和指夹 USB 串口驱动；暂时没有设备时可双击 `Start-Simulation.cmd` 检查程序流程。

1. 开启脑环，插入指夹 USB；关闭厂商采集软件和 Arduino 串口监视器。
2. 在右侧扫描脑环、选择串口并连接。两路连续有效数据达到 3 秒后填写被试信息。
3. 设置保存位置和阶段时长，创建会话；按阶段说明点击开始。
4. 阶段到时自动保存，在评分页选择压力分数，继续下一阶段。闭眼阶段结束有声音提示，请保持系统声音开启。
5. 全部完成后打开数据文件夹查看。断线时当前阶段标为未完成，重新连接后可重做，旧记录不覆盖。

界面提供明确标记的模拟模式；模拟数据只能用于流程检查，自动保存到 `SIMULATED` 子文件夹。

详见 [中文操作手册](docs/USER_GUIDE.md)、[协议与时间同步](docs/PROTOCOL.md) 和 [真机验收步骤](docs/HARDWARE_ACCEPTANCE.md)。

## 实验设置

默认顺序：睁眼静息、闭眼静息、算术练习、对照任务、压力任务、恢复，各 180 秒；每阶段后填写 0–100 压力评分。每阶段可在会话开始前改为 1–3600 秒，实验开始后锁定。说明和评分期间只预览，不记录生理数据。

题目及压力规则参考 [Pixxrick/eeg_mist](https://github.com/Pixxrick/eeg_mist/tree/d6be12e52dcf1b2185e0f2fb728ec77b973fe0b1) 的固定版本，使用独立实现。压力阶段的同伴成绩和目标是实验情境的一部分，完成页提供解释。界面不能精确报告屏幕发光时间，呈现标记仅为软件绘制完成的代理。

## 数据与同步

默认保存到桌面 `MIST_data/被试编号_时间/`。被试信息、配置、行为和评分保存在会话目录；六阶段分别保存 EEG CSV、PPG CSV、原始接收记录和质量报告。

- EEG：协议值 500 Hz，4 通道，每 100 字节通知含 8 个样本。保留原始计数和厂商比例换算的 µV。
- PPG：57600 波特率、红光整数流，固件名义输出上限约 125 Hz，显示并保存实测速率。
- 共同时间基准：`time.perf_counter_ns()`，配有 UTC 锚点。按**主机接收时间**裁切阶段，EEG 另存 500 Hz 假设下的包内估计时间。
- 两设备均没有设备时间戳和计数器。接收抖动不是硬件同步误差，也不能用来证明精确丢样数；不承诺毫秒级硬件同步。
- 脑环电量字段只显示未校准原值；通道位置暂标 CH1–CH4；PPG 当前协议不提供 IR、心率或血氧值。

真实被试数据不会上传到 GitHub。不要把采集文件加入公开仓库。

## 开发与验证

使用 64 位 Python 3.11–3.14，开发验证环境为 Windows 11 / Python 3.13。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m mist_app --simulate
.\.venv\Scripts\python.exe -m pytest -q
```

打包程序的独立流程自检（只使用模拟数据，约 11 秒，输出报告和截图）：

```powershell
.\MIST-EEG-PPG.exe --self-test "$env:TEMP\mist-exe-check" --self-test-hidden
```

可用 `--output D:\MIST_data` 指定保存根目录。关闭模拟模式后使用真实设备。

```powershell
.\.venv\Scripts\python.exe scripts\build_windows.py
```

复现本次 Windows 构建时，可先安装 `requirements.txt` 与 `requirements-build.txt` 中锁定的依赖版本。

生成 `dist/MIST-EEG-PPG/` 和 `dist/MIST-EEG-PPG-Windows-x64.zip`。构建采用目录打包，保留 Qt 动态库和第三方许可文件。依赖说明见 [THIRD_PARTY_NOTICES](docs/THIRD_PARTY_NOTICES.md)。

实现分为设备驱动、单调时钟、实验状态机、后台文件写入、数学任务和 Qt 界面。测试涵盖协议、仿真流程、阶段边界、失败重做和异常保存；实物验收状态单独记录。

# MIST 脑电＋PPG＋温度一体化实验

Windows 中文桌面程序：在同一界面连接 QX-EEG-4 脑环、Arduino RED PPG 指夹和 GT-M601 柔性温度传感器蓝牙套件，执行六阶段 MIST，按被试、阶段、尝试分别保存数据。温度记录的是传感器接触部位的表面温度，不等同于核心体温。

**[下载 Windows 64 位免安装运行包（v0.2.0，含温度功能）](https://github.com/yangr8640-eng/mist-eeg-ppg/releases/download/v0.2.0/MIST-EEG-PPG-Windows-x64.zip)** · [查看 Release 与校验文件](https://github.com/yangr8640-eng/mist-eeg-ppg/releases/tag/v0.2.0)

下载 ZIP 后完整解压，双击 `MIST-EEG-PPG.exe`；保留同目录的 `_internal` 文件夹，无需安装 Python。开发电脑也可使用本项目 `dist/MIST-EEG-PPG/MIST-EEG-PPG.exe` 或指向该程序的现有快捷方式。旧 v0.1.0 不含温度功能；GitHub 自动提供的 `Source code` 是开发源码。

**v0.2.0 以预发布（Pre-release）提供，真实设备尚未验收。** 模拟设备、协议解析和流程测试不等同于硬件兼容性或同步精度验证。开发时设备暂在实验室，真实采集能力将在实际接入后确认。

## 使用

完整解压上述 v0.2.0 运行包后启动 `MIST-EEG-PPG.exe`。同目录的 `_internal` 文件夹必须保留。

连接真实设备时，电脑需要可用的 BLE 蓝牙适配器、指夹 USB 串口驱动，以及温度套件的配套 USB 蓝牙接收器/CH340 驱动。温度蓝牙数据由该接收器转成 Windows COM 端口，不通过脑环的 BLE 扫描连接。暂时没有设备时可双击 `Start-Simulation.cmd` 检查程序流程。

1. 开启脑环和温度模块，插入指夹 USB 与温度配套 USB 蓝牙接收器；关闭厂商采集软件和 Arduino 串口监视器。
2. 在右侧扫描脑环，分别选择指夹和温度的不同 COM 端口并连接。温度默认启用；本次不测温时可在会话前关闭，恢复两路实验。
3. 所有启用设备连续有效数据达到 3 秒后填写被试信息；启用温度时填写测量部位。设置保存位置和阶段时长，创建会话；按阶段说明点击开始。
4. 阶段到时自动保存，在评分页选择压力分数，继续下一阶段。闭眼阶段结束有声音提示，请保持系统声音开启。
5. 全部完成后打开数据文件夹查看。断线时当前阶段标为未完成，重新连接后可重做，旧记录不覆盖。

界面提供明确标记的模拟模式；模拟数据只能用于流程检查，自动保存到 `SIMULATED` 子文件夹。

温度面板显示摄氏温度、绝对温度趋势、有效接收率、当前阶段已存样本数及距上次有效更新的时间；过期值有提示。温度准备门槛要求连续 3 秒且有效更新间隔小于 1.5 秒，这是软件的稳定性策略，厂家手册没有承诺固定上报频率。任一启用设备连续 3 秒无有效数据，当前阶段按未完成保存，恢复后可重做。

详见 [中文操作手册](docs/USER_GUIDE.md)、[协议与时间同步](docs/PROTOCOL.md) 和 [真机验收步骤](docs/HARDWARE_ACCEPTANCE.md)。

## 实验设置

默认顺序：睁眼静息、闭眼静息、算术练习、对照任务、压力任务、恢复，各 180 秒；每阶段后填写 0–100 压力评分。每阶段可在会话开始前改为 1–3600 秒，实验开始后锁定。说明和评分期间只预览，不记录生理数据。

题目及压力规则参考 [Pixxrick/eeg_mist](https://github.com/Pixxrick/eeg_mist/tree/d6be12e52dcf1b2185e0f2fb728ec77b973fe0b1) 的固定版本，使用独立实现。压力阶段的同伴成绩和目标是实验情境的一部分，完成页提供解释。界面不能精确报告屏幕发光时间，呈现标记仅为软件绘制完成的代理。

## 数据与同步

默认保存到桌面 `MIST_data/被试编号_时间/`。被试信息、启用设备及连接配置、行为和评分保存在会话目录；六阶段分别保存 EEG CSV、PPG CSV、温度 CSV、原始接收记录和质量报告。禁用温度时不生成温度数据文件。

- EEG：协议值 500 Hz，4 通道，每 100 字节通知含 8 个样本。保留原始计数和厂商比例换算的 µV。
- PPG：57600 波特率、红光整数流，固件名义输出上限约 125 Hz，显示并保存实测速率。
- 温度：配套 USB 蓝牙接收器的 COM 端口，115200 波特率、8N1，严格解析 `A+XX.XB\r\n`（正负号均可）；保存 `temperature_c` 与 `timing_basis=host_receive`。测量部位和连接设置写入会话清单；不把芯片分辨率当成串口输出精度。
- 共同时间基准：`time.perf_counter_ns()`，配有 UTC 锚点。按**主机接收时间**裁切阶段，EEG 另存 500 Hz 假设下的包内估计时间。
- 三设备均没有可用的设备时间戳和计数器。接收抖动不是硬件同步误差，也不能用来证明精确丢样数；不承诺毫秒级硬件同步。温度质量报告采用 3 秒接收间隔阈值，正常约 1 秒更新不计作长间隔或丢包。
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

打包程序的独立流程自检（只使用模拟数据，六个 2 秒阶段，约 20–25 秒，输出报告和截图）：

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

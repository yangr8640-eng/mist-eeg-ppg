# 第三方依赖与参考资料

本项目源码采用 MIT 许可，第三方库仍适用各自许可。Windows 发行包使用可替换的动态 Qt 库；对应许可证随包附在 `third_party_licenses`，完整依赖版本在 `dependencies.txt`。

| 依赖 | 用途 | 项目与源码 |
| --- | --- | --- |
| PySide6 / Shiboken6 / Qt | 界面与动态 Qt 库，LGPL/GPL/商业许可组合，具体组件见附带许可 | https://code.qt.io/cgit/pyside/pyside-setup.git/ 及 https://download.qt.io/official_releases/qt/ |
| Bleak | BLE，MIT | https://github.com/hbldh/bleak |
| pyserial | 串口，BSD | https://github.com/pyserial/pyserial |
| pyqtgraph | 波形绘制，MIT | https://github.com/pyqtgraph/pyqtgraph |
| NumPy | 波形数组，BSD 及附带组件许可 | https://github.com/numpy/numpy |
| PyWinRT | Windows 蓝牙接口，MIT | https://github.com/pywinrt/pywinrt |
| Python | 运行时，PSF | https://www.python.org/downloads/source/ |
| PyInstaller | 构建工具，GPL 加发行例外 | https://github.com/pyinstaller/pyinstaller |

MIST 行为设计参考 `Pixxrick/eeg_mist` 的 `d6be12e52dcf1b2185e0f2fb728ec77b973fe0b1` 版本。参考仓库未附 LICENSE；本项目不复制其源文件，实验规则在新架构中独立实现。

QX-EEG-4 协议根据用户本机随设备提供的软件进行静态核对。厂商 EXE、DLL、APK 不属于本项目，也不随源码或运行包分发。

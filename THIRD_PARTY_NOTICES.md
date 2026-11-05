# 第三方组件

项目代码的 MIT 许可证不替代第三方组件的许可证。

## 外部转换组件

FFmpeg 与 x265 通过外部命令行程序调用，Windows 轻量发布包不包含它们。安装和再分发这些组件时，适用其具体构建的许可证。

- FFmpeg：[项目](https://ffmpeg.org/)、[许可证说明](https://ffmpeg.org/legal.html)。启用 GPL 组件的构建适用相应 GPL 条款。
- x265：[项目](https://www.videolan.org/developers/x265.html)、[编码文档](https://x265.readthedocs.io/)。x265 提供 GPL 及商业许可方式。

## Windows 程序包

单文件程序通过 PyInstaller 打包，包含 Python 运行环境与 Tcl/Tk 界面组件。

- Python：[许可证](third_party/Python-LICENSE.txt)。
- Tcl：[许可证](third_party/Tcl-license.terms)。
- Tk：[许可证](third_party/Tk-license.terms)。
- PyInstaller：[许可与 bootloader 例外](third_party/PyInstaller-COPYING.txt)。

运行环境涉及的 OpenSSL 使用随包保留的 [Apache 2.0 许可文本](third_party/OpenSSL-LICENSE.txt)。

发布包保留以上许可文件。编译环境中的依赖版本和实际打包内容，以发布包信息为准。

# BiliOpsToolbox 打包报告

生成时间: 2026-08-19 20:07:06

## 1. 打包结果

❌ **打包失败**

- **错误信息**: Unknown error
- **开始时间**: 2026-08-19 20:04:14
- **结束时间**: 2026-08-19 20:07:06
- **打包耗时**: 172.20 秒
- **返回码**: 1

### 错误输出

```
160 INFO: PyInstaller: 6.21.0, contrib hooks: 2026.6
160 INFO: Python: 3.13.14
200 INFO: Platform: Windows-10-10.0.19045-SP0
201 INFO: Python environment: C:\Users\27418\.workbuddy\binaries\python\versions\3.13.12
208 INFO: Removing temporary files and cleaning cache in C:\Users\27418\AppData\Local\pyinstaller
643 INFO: Module search paths (PYTHONPATH):
['D:\\tasks\\cola\\bili_ops_toolbox',
 'D:\\WorkBuddy\\resources\\app.asar.unpacked\\cli\\vendor\\shim',
 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python313.zip',
 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\DLLs',
 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\Lib',
 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12',
 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\Lib\\site-packages',
 'D:\\tasks\\cola\\bili_ops_toolbox']
1092 INFO: Appending 'datas' from .spec
1094 INFO: checking Analysis
1094 INFO: Building Analysis because Analysis-00.toc is non existent
1094 INFO: Looking for Python shared library...
1094 INFO: Using Python shared library: C:\Users\27418\.workbuddy\binaries\python\versions\3.13.12\python313.dll
1094 INFO: Running Analysis Analysis-00.toc
1094 INFO: Target bytecode optimization level: 0
1094 INFO: Initializing module dependency graph...
1095 INFO: Initializing module graph hook caches...
1107 INFO: Analyzing modules for base_library.zip ...
2486 INFO: Processing standard module hook 'hook-encodings.py' from 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\Lib\\site-packages\\PyInstaller\\hooks'
2922 INFO: Processing standard module hook 'hook-math.py' from 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\Lib\\site-packages\\PyInstaller\\hooks'
3823 INFO: Processing standard module hook 'hook-pickle.py' from 'C:\\Users\\27418\\.workbuddy\\binaries\\python\\versions\\3.13.12\\Lib\\site-packages\\PyInstaller\\hooks'
4479 INFO: Processing standard modul
```

## 2. 冒烟测试

### 2.1 EXE 启动测试

❌ **测试失败**

- **错误信息**: Skipped due to packaging failure
- **返回码**: N/A

### 2.2 Web 模式测试

❌ **测试失败**

- **错误信息**: Skipped due to packaging failure

## 3. 总结

❌ **存在失败项，需要修复**

请根据上述错误信息进行排查和修复。


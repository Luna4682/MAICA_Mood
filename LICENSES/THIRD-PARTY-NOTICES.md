# 第三方组件与许可 / Third-Party Notices

本插件（MAICA_Mood）建立在下列第三方组件之上。完整许可全文都在本目录里，
组件、版本、许可与文件位置的对应关系见下表。

| 组件 | 版本 | 许可证 | 许可全文 | 在插件里的位置 |
|---|---|---|---|---|
| MAICA Illuminator（后端） | 1.3.004.post4（PyPI `mi-maica` 1.3.4.post4） | MIT | `LICENSE-MAICA-Illuminator.txt` | `backend\` |
| llama.cpp | 0.6.0-dev（build 11557，commit 5c8854fd8） | MIT | `LICENSE-llama.cpp.txt` | `extractor\bin-vulkan\`、`extractor\bin-cpu\` |
| Qwen2.5-1.5B-Instruct（基座模型） | 1.5B | Apache-2.0 | `LICENSE-Apache-2.0.txt` | `extractor\fact-r8-q6_k.gguf`（见下方"修改说明"） |
| libomp（LLVM OpenMP 运行时） | 随 llama.cpp 构建 | Apache-2.0 WITH LLVM-exception | `LICENSE-LLVM-OpenMP.txt` | `extractor\bin-*\libomp.dll` |
| BAAI bge-base-zh-v1.5（向量模型，int8 ONNX） | v1.5 | MIT | `LICENSE-BAAI-FlagEmbedding.txt` | `embedding\models\bge-base-zh-v1.5-int8\` |
| Python | 3.12.10 | PSF License | `LICENSE-Python-3.12.txt` | `runtime\`（内嵌解释器 + 标准库） |
| 其它 Python 依赖 | quart 0.20.0、SQLAlchemy 2.1.3、openai 3.24.0、numpy 2.5.3、onnxruntime 1.30.0、hypercorn 0.17.3、pydantic、Pillow、tokenizers、aiosqlite 等 | MIT / BSD / Apache-2.0 / PSF 等 | 多数随包自带：`runtime\Lib\site-packages\<包>.dist-info\LICENSE*` | `runtime\Lib\site-packages\` |

## 修改说明（Apache-2.0 §4(b)）

`extractor\fact-r8-q6_k.gguf` 是 **Qwen2.5-1.5B-Instruct** 的衍生模型：
在本项目中用 LoRA(r32) 微调为「从玩家一句话里抽取结构化事实」的用途，
再用 llama.cpp 量化为 Q6_K。它**不是** Qwen 官方发布的文件，**我们对其做了修改**；
基座模型仍适用 Apache-2.0。gguf 元数据里保留了 `general.license = apache-2.0`
和指向基座模型许可的链接。

`backend\` 是 **MAICA Illuminator** 的**修改版**（新增/调整了事实记忆、问卷导入、
本地统计接口等），不是原样分发。原项目：https://github.com/Mon1-innovation/MAICA （MIT）。

## 本插件没有分发的组件

- **MAICA ChatSubmod**（前端，MIT）：由玩家自行安装，本插件不含它的任何文件。
- **MAICA_Server_Submod**：上游仓库**没有声明开源许可**，本插件既不包含、也不分发（也不依赖）它的
  任何文件，玩家若已安装它，那是玩家自己的安装。
- 官方后端服务与官方模型：本插件不包含、不代理。

## 本插件自己的代码

`MAICA_Mood` 的代码（`MAICA_Mood.rpy`、`maica_mood_lib.py`、`launcher.py`、
`watchdog.py`、`webui_server.py`、`updater.py`、`lib\monika_mood_engine.py`、
`embedding\embed_server.py` 等）以 **MIT** 发布，见 `LICENSE-MAICA_Mood.txt`。

## 商标

MIT 与 Apache-2.0 都**不授予商标权**。本插件是**非官方**项目，与 MAICA 项目作者
没有隶属关系；插件名与描述中的 "MAICA" 仅用于说明它基于该后端。

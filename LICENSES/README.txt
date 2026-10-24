第三方许可文件说明

  LICENSE-MAICA-Illuminator.txt   MAICA 后端（MIT）。原文照抄自上游仓库：
                                  https://github.com/Mon1-innovation/MAICA
                                 
  LICENSE-llama.cpp.txt           llama.cpp（MIT）。extractor\bin-vulkan / bin-cpu 里的
                                  llama-server 等程序来自该项目。

  LICENSE-Apache-2.0.txt          Apache License 2.0 全文。用于 extractor\fact-r8-q6_k.gguf
                                  的基座模型 Qwen2.5-1.5B-Instruct。我对它做过微调与量化，
                                  修改说明见 THIRD-PARTY-NOTICES.md。

  LICENSE-LLVM-OpenMP.txt         libomp 的 Apache-2.0 WITH LLVM-exception。
                                  与 extractor\bin-*\LICENSE-LLVM-OpenMP 是同一份。

  LICENSE-BAAI-FlagEmbedding.txt  BAAI bge 系列 / FlagEmbedding（MIT）。
                                  用于 embedding\models\bge-base-zh-v1.5-int8。

  LICENSE-Python-3.12.txt         Python 3.12 的 PSF License。用于 runtime\ 里自带的解释器
                                  和标准库。

  LICENSE-MAICA_Mood.txt          本插件自己的代码（MIT）。

  THIRD-PARTY-NOTICES.md          组件 / 版本 / 许可证的汇总表，以及 Apache-2.0 要求的
                                  "修改说明"（r8 模型、MAICA 后端）。

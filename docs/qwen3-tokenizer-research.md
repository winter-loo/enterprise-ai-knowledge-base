# Qwen3 Embedding 与 `o200k_base` tokenizer 对照

## 结论

Qwen3 Embedding 的 tokenizer 与 OpenAI `tiktoken` 的 `o200k_base` **并不相同**。

两者都属于 byte-level BPE 一类的 tokenizer，因此不少常见英文或短中文样本会得到相同的 token 数；但它们的词表、合并规则、预分词规则和特殊 token 均不同，不能把 `o200k_base` 描述为“与 Qwen3 完全相同”。

## 官方文件证据

Qwen 官方模型仓库声明 `Qwen3-Embedding-0.6B` 使用 `Qwen2Tokenizer`，特殊 token 从 ID `151643` 开始，模型最大长度为 `131072`：

- [Qwen tokenizer_config.json](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/main/tokenizer_config.json)
- [Qwen tokenizer.json](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/main/tokenizer.json)

OpenAI 官方 `tiktoken` 源码则让 `o200k_base` 加载独立的 `o200k_base.tiktoken` 合并表，使用自己的预分词正则，并把特殊 token 放在 `199999` 和 `200018`：

- [OpenAI tiktoken: openai_public.py](https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py#L101-L140)

## 目标机器实测

实验只在用户指定的目标机器 `ldd@192.168.10.104` 上执行，没有在本机加载模型。

目标机器的 Ollama 服务运行 `qwen3-embedding:0.6b`。`/api/show` 返回的 GGUF tokenizer 元数据为：

| 项目 | Qwen3 Embedding 服务 | `o200k_base` |
|---|---:|---:|
| tokenizer/pre-tokenizer | `gpt2` 模型族，`qwen2` 预分词 | OpenAI `o200k_base` 正则 |
| token/词表项 | 151,669 | 200,019 (`n_vocab`) |
| merges/mergeable ranks | 151,387 | 199,998 |
| 代表性特殊 token ID | 151,643 | 199,999、200,018 |

这里 GGUF 的 `gpt2` 表示 tokenizer 的实现族，不表示它使用 GPT-2 或 OpenAI `o200k_base` 的同一份词表。

Qwen 的 OpenAI 兼容 `/v1/embeddings` 接口会自动加一个 EOS token。空字符串返回 `prompt_tokens=1`，所以下表把服务返回值减 1，得到正文 token 数：

| 文本 | Qwen3 正文 token | `o200k_base` token |
|---|---:|---:|
| `hello world` | 2 | 2 |
| `知识库` | 2 | 2 |
| `中华人民共和国` | 2 | 1 |
| `人工智能知识库` | 3 | 4 |
| `今天天气很好。` | 4 | 5 |
| `The quick brown fox jumps over the lazy dog.` | 10 | 10 |
| `版本 3.14 已发布。` | 10 | 8 |

样本既有相同结果，也有不同结果。这符合“同一算法家族、不同词表与规则”的特征，并直接否定了两者 token 计数完全一致的说法。

## 对当前项目的影响

当前 `app/chunking.py` 使用 `o200k_base` 仍然可以作为一个稳定、项目自有的 **Chunk Tokenizer**，用于控制检索片段粒度；但它不能同时被称为 Qwen3 Embedding 的精确 tokenizer。

如果 `size` 的业务含义是“Qwen3 实际接收的 token 硬上限”，应该改用随 embedding 模型配置的真实 tokenizer，或在提交给 embedding 服务前用该 tokenizer 再做最终校验。如果 `size` 只是稳定的检索分块尺度，则可以继续使用 `o200k_base`，但应明确它是项目选定的计量标准，而不是模型的原生计量标准。

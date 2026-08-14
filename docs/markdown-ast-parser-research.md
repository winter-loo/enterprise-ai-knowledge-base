# 高性能 Markdown AST 解析器选型

## 结论

本项目优先选择 **[`multimark==0.3.2`](https://pypi.org/project/multimark/)**。

它封装了 GitHub 的 C 语言 [`cmark-gfm`](https://github.com/github/cmark-gfm) 解析器，同时提供可遍历的 Python AST。相比只输出 HTML 的高性能解析器，它更适合本项目后续按标题、段落、表格、图片、列表和代码块进行结构化切分。

建议保留 **[`markdown-it-py==4.2.0`](https://pypi.org/project/markdown-it-py/)** 作为成熟、纯 Python 的备选。如果部署平台无法使用 `multimark` 的预编译 wheel，或者不愿引入仍处于 Beta 阶段的库，再采用这个方案。

## 本项目真正需要的能力

Markdown 切块不应只依赖正则表达式。解析器至少需要提供：

- 块级结构：标题、段落、列表、引用、围栏代码块、HTML 块；
- GFM 结构：表格、任务列表、删除线、自动链接；
- 行内结构：图片、链接、强调、行内代码；
- 原文位置：至少能把块级节点映射回原始 Markdown 的行范围；
- 足够高的吞吐量：导入大量文档时，解析不应成为主要瓶颈。

其中“原文位置”很重要：AST 告诉我们一段内容是什么，而位置让我们能够从原始 Markdown 中忠实地取回它，避免重新渲染时改变表格、空白、链接定义或代码格式。

## 推荐方案：multimark

[`multimark`](https://github.com/posit-dev/multimark) 的主要优点：

- 底层使用 `cmark-gfm`，核心解析工作在 C 中完成；
- `parse()` 直接返回节点树，而不是只有 HTML；
- 节点支持 `children`、`parent`、`next`、`previous` 和 `walk()`；
- 能读取标题级别、图片 URL、代码围栏信息等节点属性；
- 支持 `table`、`strikethrough`、`autolink`、`tasklist`、`tagfilter` 等 GFM 扩展；
- 节点暴露 `start_line`、`start_column`、`end_line`、`end_column`，可用于建立 AST 到原文的映射。位置 API 可见其[官方源码](https://github.com/posit-dev/multimark/blob/v0.3.2/src/multimark/_node.py#L293-L315)。

建议初始化方式：

```python
from multimark import parse

root = parse(
    markdown,
    extensions=["table", "strikethrough", "autolink", "tasklist", "tagfilter"],
)
```

### 已验证的性能

在当前开发机上用一份约 102,000 字符、包含标题、图片、GFM 表格和代码块的合成 Markdown，连续解析 5 次，得到以下指示性结果：

| 解析器 | 输出 | 5 次总耗时 |
| --- | --- | ---: |
| `multimark 0.3.2` | AST + 行/列位置 | 16.6 ms |
| `mistune 3.3.4` | AST，无位置 | 147.8 ms |
| `wenmode 0.9.0` | mdast + 完整 offset | 381.5 ms |
| `markdown-it-py 4.2.0` | 语法树 + 块级行范围 | 397.3 ms |

这不是跨平台的通用基准，只说明在本项目当前环境与这类输入上，`multimark` 有明显性能优势。项目官方也发布了与 `cmarkgfm` 和纯 Python `commonmark.py` 的[性能比较](https://github.com/posit-dev/multimark#performance)，但其主要测量的是 Markdown 到 HTML 的路径，不能直接替代本项目的 AST 基准。

### 使用时要接受的限制

- 当前版本在 PyPI 标记为 Beta，虽然底层 `cmark-gfm` 很成熟，但 Python AST 封装本身较新；
- 节点提供行列位置，没有直接提供绝对字符 offset；可以预先建立每行起始位置表来换算；
- 中文、emoji 等多字节字符可能使“列”更接近 UTF-8 字节位置而非 Python 字符索引，必须用真实语料测试；
- `cmark-gfm` 的部分扩展节点存在上游 source-position 边界问题，例如[表格位置问题](https://github.com/github/cmark-gfm/issues/222)。因此应优先按块级行范围切片，不要假设所有行内节点都具有完全精确的边界；
- 官方 wheel 覆盖主流 macOS、Windows x86-64 和 Linux x86-64；其他架构可能需要本地编译，部署前要在目标镜像验证。

## 备选方案比较

### markdown-it-py

[`markdown-it-py`](https://github.com/executablebooks/markdown-it-py) 成熟度更高，CommonMark/GFM 支持完整，也能通过 [`SyntaxTreeNode`](https://markdown-it-py.readthedocs.io/en/latest/api/markdown_it.tree.html) 把 token 流包装成树。其 [`Token.map`](https://markdown-it-py.readthedocs.io/en/latest/api/markdown_it.token.html) 提供块级起止行，但没有通用的字符或字节范围。

适合“稳定性与纯 Python 部署优先”的场景；本项目若只按块级结构切分，它的位置信息也够用，但吞吐量明显低于 `multimark`。

### Mistune

[`Mistune`](https://github.com/lepture/mistune) 可以用 `renderer="ast"` 输出 AST，并通过插件支持表格等语法，官方用法见其[高级文档](https://github.com/lepture/mistune/blob/main/docs/advanced.rst)。它的纯 Python 解析速度不错，但 AST 缺少可靠的原文位置，这会妨碍本项目精确保留原始 Markdown，因此不作为首选。

### Wenmode

[`Wenmode`](https://pypi.org/project/wenmode/) 能输出 mdast 兼容树，并可携带行、列和绝对 offset；功能上很贴合结构化切块。不过它仍处于 Beta，在本地“AST + positions”路径的速度也没有超过 `multimark`。

### Marko

[`Marko`](https://pypi.org/project/marko/) 提供真正的 AST、GFM 扩展和 source span，适合纯 Python 且特别看重字符级位置的场景。不过其性能目标不是领先，因此不符合这次“高性能优先”的要求。

### tree-sitter-markdown

[`tree-sitter-markdown`](https://github.com/tree-sitter-grammars/tree-sitter-markdown) 的节点带字节范围，增量解析也很强，但官方 README 明确说明仍有输出不准确之处，并不推荐用于正确性要求高的场景。它更适合编辑器高亮，而不是作为企业知识库内容结构的事实来源。

## 接入建议

不要让 `app/chunking.py` 直接依赖第三方节点类型。建议先增加一个项目自己的适配层：

```python
@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    text: str
    start_line: int
    end_line: int
    heading_level: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)
```

适配层负责把 `multimark` AST 转成稳定的 `MarkdownBlock` 列表；切块策略只消费这个列表。这样以后如果发现 `multimark` 的位置精度或部署兼容性不合适，可以替换为 `markdown-it-py`，而不需要重写切块算法。

首轮接入测试至少覆盖：

- 中文与 emoji 标题；
- ATX 标题和 Setext 标题；
- 嵌套列表和引用；
- GFM 表格与任务列表；
- 围栏代码块中伪造的 `# 标题`；
- 行内图片、引用式图片与链接；
- HTML 块；
- AST 节点映射回原文后的内容完整性。

## 最终决策

1. 先在项目中用 `multimark==0.3.2` 做一个小型适配器和真实语料测试；
2. 只依赖块级 source position，并自行维护“行号到 Python 字符 offset”的映射；
3. 如果目标部署平台没有可用 wheel，或真实语料暴露不可接受的位置问题，则切换到 `markdown-it-py==4.2.0`；
4. AST 负责识别文档结构，切块器负责在标题边界、token 上限、最小块大小之间作决策；LLM 可用于边界歧义或语义补充，但不应承担每份文档的基础语法解析。

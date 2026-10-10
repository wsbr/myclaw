# 文件工具分页与冲突恢复

`read_file` 的 `offset` 是从文件起点计算的原始字节偏移，不是字符位置。
调用方应使用上一页返回的 `next_offset`；工具不会自动取整或跳过字节。
任意指定的偏移必须落在字符边界：UTF-8 不能从续字节开始，UTF-16 不能从
奇数字节或代理项对的低代理项开始。偏移等于或超过文件大小时返回成功空页。

`limit` 是目标字节数，范围为 1–65536。若页尾落在字符中间，工具最多多读
3 字节以完成该字符。因此每页最多消费 `limit + 3` 字节，`next_offset` 和
evidence 的 `byte_length` 均以实际消费字节计算。在文件未发生变化时，顺序
拼接所有页的 `content` 不会丢失或重复字符。

UTF-16 文件必须有起始 BOM。工具从文件 BOM 判断大小端，后续页沿用此字节序，
不要求每页重复 BOM。起始 BOM 计入字节偏移，但不作为文本输出；极小的首个
分页可以只消费 BOM，并返回空内容和非空 `next_offset`。正文中的 BOM 字符
会保留。无 BOM 的 UTF-16 返回编码错误，避免依赖宿主机的本机字节序。

分页读取及字符补齐的内存和读取量有固定上限。文件 checksum 仍按既有实现
分块扫描整个文件；它不代表只对当前分页计算校验和，也不保证跨多次调用的
文件快照不变。调用方应比较每页 checksum，发现变化时重新读取。

文件错误使用明确且不可自动重试的代码：

| 错误码 | 含义与恢复方式 |
| --- | --- |
| `workspace_file_not_found` | 文件不存在或不是普通文件；先查询可用路径。 |
| `workspace_offset_invalid` | offset 位于字符内部；使用先前的 `next_offset` 或正确边界。 |
| `workspace_encoding_error` | 编码无效、文件尾字符不完整，或 UTF-16 缺少 BOM；检查编码与文件。 |

`write_file` 的 `workspace_target_exists`、`workspace_target_missing` 和
`workspace_checksum_conflict` 保留 `conflict` 分类，设置 `retryable=false`。
它们要求重新选择 create/replace、路径或读取最新 checksum，不能通过重复执行
同一组参数恢复。replace 仍然要求 `expected_checksum`，校验不通过时不写入文件；
读取最新 checksum 后的新调用可以正常恢复。
